"""Per-call causal DeepFilterNet3 with a delay-matched, latched raw bypass."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import struct
import sys
import time

import numpy as np
import soxr
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.frames.frames import FilterEnableFrame

from dots_phone.deepfilter_assets import (
    ALIGNMENT_MS,
    HOP_SAMPLES,
    MODEL_SHA256,
    SAMPLE_RATE,
    TOTAL_DELAY_MS,
    verify_model,
)

logger = logging.getLogger(__name__)


class NativeWorker:
    def __init__(self, model, library, timeout_ms):
        self.model, self.library = model, library
        self.timeout = timeout_ms / 1000
        self.process = None

    async def start(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "SYSTEMROOT"}
        }
        env.update(PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="1")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "dots_phone.deepfilter_worker",
            "--model",
            str(self.model),
            "--library",
            str(self.library),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        async with asyncio.timeout(10):
            ready = json.loads(await self.process.stdout.readline())
        if ready != {"ready": True, "sample_rate": SAMPLE_RATE, "hop": HOP_SAMPLES}:
            raise RuntimeError("native_start_failed")

    async def process_samples(self, samples):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("native_exited")
        # Bound each IPC transaction to 500 ms of audio; normal carrier packets
        # contain 20 ms. Backpressure and deadlines bound backlog growth.
        output = []
        for offset in range(0, len(samples), HOP_SAMPLES * 50):
            payload = samples[offset : offset + HOP_SAMPLES * 50].astype("<f4").tobytes()
            async with asyncio.timeout(self.timeout):
                self.process.stdin.write(struct.pack("<I", len(payload)) + payload)
                await self.process.stdin.drain()
                output.append(await self.process.stdout.readexactly(len(payload)))
        return np.frombuffer(b"".join(output), dtype="<f4")

    async def close(self):
        process, self.process = self.process, None
        if not process:
            return
        if process.returncode is None:
            # No post-call audio flush or queued processing after closure.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()


class DeepFilterNetFilter(BaseAudioFilter):
    def __init__(self, *, model, library, timeout_ms=100, worker_factory=NativeWorker):
        self.model, self.library, self.timeout_ms = model, library, timeout_ms
        self.worker_factory = worker_factory
        self.worker = None
        self.lock = asyncio.Lock()
        self.status_callback = None
        self.started = False
        self.bypassed = False
        self.failure_reason = None
        self.received_samples = 0
        self.max_processing_ms = 0.0
        self._input = np.empty(0, dtype=np.float32)
        self._output = bytearray()
        self._raw = bytearray()

    def status(self, state):
        return {
            "state": state,
            "bypassed": self.bypassed,
            "failure_reason": self.failure_reason,
            "model_sha256": MODEL_SHA256,
            "delay_ms": TOTAL_DELAY_MS,
            "received_samples": self.received_samples,
            "max_processing_ms": round(self.max_processing_ms, 3),
        }

    def notify(self, state):
        if self.status_callback:
            try:
                self.status_callback(self.status(state))
            except Exception:
                logger.warning("Could not save input-filter status")

    async def start(self, sample_rate):
        async with self.lock:
            if self.started:
                raise RuntimeError("input filter already started")
            if sample_rate not in (8000, 16000, 24000, 48000):
                raise ValueError("unsupported input sample rate")
            self.sample_rate = sample_rate
            self.started = True
            self._up = soxr.ResampleStream(
                sample_rate, SAMPLE_RATE, 1, dtype="float32", quality="QQ"
            )
            self._down = soxr.ResampleStream(
                SAMPLE_RATE, sample_rate, 1, dtype="float32", quality="QQ"
            )
            self._output = bytearray(sample_rate * ALIGNMENT_MS // 1000 * 2)
            self._raw = bytearray(sample_rate * TOTAL_DELAY_MS // 1000 * 2)
            self.worker = self.worker_factory(self.model, self.library, self.timeout_ms)
            try:
                await self.worker.start()
            except asyncio.CancelledError:
                await self.worker.close()
                self.worker = None
                raise
            except Exception as exc:
                await self.bypass(type(exc).__name__)
            self.notify("bypassed" if self.bypassed else "active")

    async def bypass(self, reason):
        self.bypassed, self.failure_reason = True, reason
        if self.worker:
            await self.worker.close()
            self.worker = None
        self._input = np.empty(0, dtype=np.float32)
        self._output.clear()
        logger.warning("DeepFilterNet bypassed for this call: %s", reason)
        self.notify("bypassed")

    async def process_frame(self, frame):
        if isinstance(frame, FilterEnableFrame) and not frame.enable:
            async with self.lock:
                if self.started and not self.bypassed:
                    await self.bypass("disabled")
        # Bypass is latched for the call. Re-enable on the next call rather than
        # replaying stale native state or shifting the active audio clock.

    async def filter(self, audio):
        async with self.lock:
            if not self.started:
                raise RuntimeError("input filter not started")
            if len(audio) % 2:
                raise ValueError("mono PCM16 input requires complete samples")
            if not audio:
                return audio
            started = time.perf_counter()
            self.received_samples += len(audio) // 2
            self._raw.extend(audio)
            raw = bytes(self._raw[: len(audio)])
            del self._raw[: len(audio)]
            if self.bypassed:
                return raw
            try:
                samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
                self._input = np.concatenate((self._input, self._up.resample_chunk(samples)))
                count = len(self._input) // HOP_SAMPLES * HOP_SAMPLES
                if count:
                    enhanced = await self.worker.process_samples(self._input[:count])
                    self._input = self._input[count:].copy()
                    if len(enhanced) != count or not np.isfinite(enhanced).all():
                        raise RuntimeError("invalid_native_output")
                    result = self._down.resample_chunk(enhanced)
                    self._output.extend((np.clip(result, -1, 1) * 32767).astype("<i2").tobytes())
                if len(self._output) < len(audio):
                    raise RuntimeError("native_output_underflow")
                result = bytes(self._output[: len(audio)])
                del self._output[: len(audio)]
                return result
            except asyncio.CancelledError:
                # A cancelled pipe read must never be consumed by a later frame.
                await self.bypass("cancelled")
                raise
            except Exception as exc:
                await self.bypass(type(exc).__name__)
                return raw
            finally:
                self.max_processing_ms = max(
                    self.max_processing_ms, (time.perf_counter() - started) * 1000
                )

    async def stop(self):
        async with self.lock:
            if self.worker:
                await self.worker.close()
                self.worker = None
            self._input = np.empty(0, dtype=np.float32)
            self._output.clear()
            self._raw.clear()
            if self.started:
                self.notify("stopped")
            self.started = False


def deepfilter_factory(settings):
    # Explicit enabled-but-misinstalled configuration fails before dialing.
    verify_model(settings.deepfilter_model_path)
    if not settings.deepfilter_library_path.is_file():
        raise RuntimeError("DeepFilterNet native runtime missing; use the deepfilter image")

    def create():
        return DeepFilterNetFilter(
            model=settings.deepfilter_model_path,
            library=settings.deepfilter_library_path,
            timeout_ms=settings.deepfilter_frame_timeout_ms,
        )

    return create
