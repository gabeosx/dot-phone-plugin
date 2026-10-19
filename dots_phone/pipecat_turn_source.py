"""Experimental local turn evidence for the explicit manual Realtime route.

Silero supplies onsets and quiet-frame evidence; SmartTurn supplies completion
evidence. Neither an input timeout nor EOF completes a turn. This is an actual
detector adapter, not a validated foreground-speaker/intent classifier. In
particular, a high completion score does not distinguish background speech.
The caller must explicitly install it as the per-call external source.

The private inference hooks and bundled models below are pinned to Pipecat 1.12.
No model download, audio logging, transcript storage, or network API is used.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class _InferenceCapacityError(RuntimeError):
    """All bounded local inference slots are still occupied."""


class _InferencePool:
    """Bound admission across calls and event loops, including timed-out jobs.

    asyncio cancellation cannot stop running native ONNX code. Only the actual
    concurrent future finishing releases a permit; closing a source never
    replaces a busy pool or submits more work behind its running jobs.
    """

    def __init__(self, workers, name):
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._permits = threading.BoundedSemaphore(workers)
        self._lock = threading.Lock()
        self._outstanding = 0

    @property
    def outstanding(self):
        with self._lock:
            return self._outstanding

    def submit(self, function):
        if not self._permits.acquire(blocking=False):
            raise _InferenceCapacityError()
        with self._lock:
            self._outstanding += 1
        try:
            future = self._executor.submit(function)
        except BaseException:
            self._release(None)
            raise
        future.add_done_callback(self._release)
        return future

    def _release(self, future):
        with self._lock:
            self._outstanding -= 1
            self._permits.release()


# No threads are started until the first submit. These pools are deliberately
# process-wide: repeated calls with stuck inference cannot create new workers.
_VAD_POOL = _InferencePool(2, "phone-vad")
_ENDPOINT_POOL = _InferencePool(2, "phone-end")


@dataclass(frozen=True)
class LocalTurnConfig:
    confidence: float = 0.5
    start_ms: int = 96
    pause_ms: int = 320
    retry_ms: int = 160
    settle_ms: int = 224
    context_ms: int = 8000
    pre_speech_ms: int = 500
    max_backlog_ms: int = 1000
    inference_timeout_ms: int = 2000

    def __post_init__(self):
        if not math.isfinite(self.confidence) or not 0 < self.confidence < 1:
            raise ValueError("Invalid speech confidence")
        if any(value <= 0 or value % 32 for value in (self.start_ms, self.pause_ms, self.retry_ms)):
            raise ValueError("Start, pause and retry durations require positive 32ms multiples")
        if self.settle_ms < 0 or self.settle_ms % 32:
            raise ValueError("Settle duration requires a nonnegative 32ms multiple")
        if not 32 <= self.context_ms <= 8000 or not 0 <= self.pre_speech_ms <= self.context_ms:
            raise ValueError("Invalid retained audio bounds")
        if not 32 <= self.max_backlog_ms <= 5000 or not 1 <= self.inference_timeout_ms <= 10000:
            raise ValueError("Invalid inference bounds")


class BundledTurnModels:
    """Local-only model loading and synchronous inference, run off the event loop."""

    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        import pipecat.audio.turn.smart_turn.local_smart_turn_v3 as smart
        import pipecat.audio.vad.silero as silero

        vad_path = Path(silero.__file__).parent / "data/silero_vad.onnx"
        endpoint_path = Path(smart.__file__).parent / "data/smart-turn-v3.2-cpu.onnx"
        if not vad_path.is_file() or not endpoint_path.is_file():
            raise RuntimeError("Bundled turn models unavailable")
        ort.disable_telemetry_events()
        self.np = np
        self.vad = silero.SileroOnnxModel(str(vad_path), force_onnx_cpu=True)
        self.endpoint = smart.LocalSmartTurnAnalyzerV3(
            smart_turn_model_path=str(endpoint_path), cpu_count=1, sample_rate=8000
        )
        self.endpoint.set_sample_rate(8000)
        # Override a process-wide diagnostic opt-in: private call PCM is never logged.
        self.endpoint._log_data = False

    def samples(self, pcm):
        return self.np.frombuffer(pcm, dtype="<i2").astype(self.np.float32) / 32768.0

    def speech_probability(self, pcm):
        return float(self.vad(self.samples(pcm), 8000).item())

    def completion_probability(self, pcm):
        # The SDK's append_audio() has a silence timeout that forces COMPLETE.
        # Only its bounded-window model prediction is admissible here. Its
        # feature extractor left-pads short context; no future/EOF PCM is added.
        return float(self.endpoint._predict_endpoint(self.samples(pcm))["probability"])


@dataclass(frozen=True)
class _Audio:
    pcm: bytes
    received_at: float


@dataclass(frozen=True)
class _Prediction:
    epoch: int
    turn_id: int
    speech_revision: int
    sample_end: int
    context_start: int
    received_at: float
    scheduled_at: float
    inference_started_at: float
    inference_finished_at: float
    available_at: float
    probability: float


@dataclass(frozen=True)
class _Boundary:
    started: bool
    applied: bool
    epoch: int
    accepted_turn: int
    observations: dict
    deferred: bool = False


class LocalTurnSource(FrameProcessor):
    """Bounded asynchronous Silero/SmartTurn source for ManualRealtimeService.

    Construct one per call, e.g. ``external_turn_source_factory=LocalTurnSource``.
    The pipeline must place ManualInputCapture before this processor. Model
    initialization, VAD, and endpoint inference run in dedicated bounded workers.
    Endpoint results rejoin the audio FIFO behind already-received input, so
    resumed speech invalidates an old completion before it can end the turn.
    """

    def __init__(self, llm, *, config=None, model_factory=BundledTurnModels, clock=time.monotonic):
        super().__init__()
        self.llm = llm
        self.config = config or LocalTurnConfig()
        self.clock = clock
        self.epoch = llm.turn_epoch
        self.model_factory = model_factory
        self.models = None
        self.queue = asyncio.Queue()
        self.worker = None
        self.endpoint_task = None
        self.boundary_task = None
        self._pending_start = None
        self._owned_turn_id = None
        self.vad_executor = _VAD_POOL
        self.endpoint_executor = _ENDPOINT_POOL
        self.closed = False
        self._close_task = None
        self.failed = False
        self.origin = clock()
        self.events = deque(maxlen=128)
        self.dropped_events = 0
        self.input_samples = self.processed_samples = self.backlog_samples = 0
        self.max_backlog_samples = 0
        self.vad_frames = self.endpoint_predictions = self.stale_predictions = 0
        self._partial = b""
        self._context = b""
        self._context_start = 0
        self._voice_ms = self._quiet_ms = self._last_probe_quiet_ms = 0
        self._active = False
        self._speech_revision = 0
        self._turn_start = self._last_speech_end = 0
        self._candidate = None
        self._last_received_at = self.origin

    def _live(self):
        return not (
            self.closed
            or self.failed
            or self.llm.turn_epoch != self.epoch
            or self.llm._external_closed
        )

    def _record(self, kind, **values):
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        self.events.append(
            {"kind": kind, "handled_at_ms": round((self.clock() - self.origin) * 1000, 3), **values}
        )

    def _fail(self, phase):
        if not self.closed and not self.failed:
            self.failed = True
            self._candidate = None
            self._record("failed", phase=phase)
            self.llm.voice_failure("external_source_" + phase, RuntimeError())

    def report(self):
        return {
            "source": "silero_smart_turn_experimental",
            "config": asdict(self.config),
            "active": self._active,
            "closed": self.closed,
            "failed": self.failed,
            "input_samples": self.input_samples,
            "captured_input_samples": self.llm.captured_input_samples,
            "processed_samples": self.processed_samples,
            "unclassified_samples": self.llm.captured_input_samples - self.processed_samples,
            "queued_before_source_samples": self.llm.captured_input_samples - self.input_samples,
            "partial_samples": len(self._partial) // 2,
            "backlog_samples": self.backlog_samples,
            "max_backlog_samples": self.max_backlog_samples,
            "context_samples": len(self._context) // 2,
            "vad_frames": self.vad_frames,
            "endpoint_predictions": self.endpoint_predictions,
            "stale_predictions": self.stale_predictions,
            "events": list(self.events),
            "dropped_events": self.dropped_events,
            "clocks": "source samples at 8kHz; receipt, inference, availability and handling use local monotonic offsets",
        }

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self.worker is None and self._live():
            # Deliberately independent of the frame-processing task: an
            # interruption cancels data-frame tasks, not ongoing turn inference.
            self.worker = asyncio.create_task(self._run())
        elif isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._live():
                if (
                    frame.sample_rate != 8000
                    or frame.num_channels != 1
                    or len(frame.audio) % 2
                    or not frame.metadata.get("manual_input_accepted")
                ):
                    self._fail("input_format")
                else:
                    samples = len(frame.audio) // 2
                    if not samples:
                        await self.push_frame(frame, direction)
                        return
                    self.input_samples += samples
                    if self.backlog_samples + samples > self.config.max_backlog_ms * 8:
                        self._fail("backlog")
                    else:
                        self.backlog_samples += samples
                        self.max_backlog_samples = max(
                            self.max_backlog_samples, self.backlog_samples
                        )
                        self.queue.put_nowait(_Audio(bytes(frame.audio), self.clock()))
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.close()
        # No detector inference or boundary handshake blocks carrier input.
        await self.push_frame(frame, direction)

    async def _inference(self, executor, function, *args):
        def run():
            started = self.clock()
            result = function(*args)
            return result, started, self.clock()

        return await asyncio.wait_for(
            asyncio.wrap_future(executor.submit(run)),
            self.config.inference_timeout_ms / 1000,
        )

    async def _run(self):
        try:
            self.models, _, _ = await self._inference(self.vad_executor, self.model_factory)
            while self._live():
                item = await self.queue.get()
                if isinstance(item, _Prediction):
                    self.endpoint_task = None
                    await self._accept_prediction(item)
                else:
                    await self._audio(item)
                    self.backlog_samples -= len(item.pcm) // 2
        except asyncio.CancelledError:
            raise
        except _InferenceCapacityError:
            self._fail("capacity")
        except Exception:
            self._fail("inference")

    @staticmethod
    def _probability(value):
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= 1:
            raise ValueError("Invalid detector probability")
        return result

    async def _audio(self, packet):
        self._partial += packet.pcm
        self._last_received_at = packet.received_at
        while len(self._partial) >= 512 and self._live():
            pcm, self._partial = self._partial[:512], self._partial[512:]
            token = (self.epoch, self.llm.manual.turn_id)
            probability, started, finished = await self._inference(
                self.vad_executor, self.models.speech_probability, pcm
            )
            available = self.clock()
            if not self._live():
                return
            probability = self._probability(probability)
            self.processed_samples += 256
            self.vad_frames += 1
            self._context = (self._context + pcm)[-self.config.context_ms * 16 :]
            self._context_start = self.processed_samples - len(self._context) // 2
            speech = probability >= self.config.confidence
            if speech:
                self._speech_revision += 1
                self._candidate = None
                self._last_speech_end = self.processed_samples
                self._quiet_ms = self._last_probe_quiet_ms = 0
                self._voice_ms += 32
                if not self._active and self._voice_ms >= self.config.start_ms:
                    self._turn_start = self.processed_samples - self._voice_ms * 8
                    observations = {
                        "source_end_ms": self.processed_samples / 8,
                        "turn_start_source_ms": self._turn_start / 8,
                        "received_at_ms": (packet.received_at - self.origin) * 1000,
                        "inference_started_at_ms": (started - self.origin) * 1000,
                        "inference_finished_at_ms": (finished - self.origin) * 1000,
                        "available_at_ms": (available - self.origin) * 1000,
                    }
                    if not self.llm.recognize_onset(epoch=token[0], turn_id=token[1]):
                        if self._live():
                            self._fail("onset_rejected")
                        return
                    self._record("onset", epoch=token[0], turn_id=token[1], **observations)
                    self._active = True
                    if self.boundary_task is not None:
                        # A prior stop is projecting. Preserve this onset's
                        # identity, then start after its acknowledgement.
                        self._pending_start = (token, observations)
                    else:
                        self._launch_boundary(True, token, observations)
            else:
                self._voice_ms = 0
                if self._active:
                    self._quiet_ms += 32
            await self._consider_endpoint()

    def _launch_boundary(self, started, token, observations, classified_samples=None):
        self._owned_turn_id = None
        self.boundary_task = asyncio.create_task(
            self._propose(started, token, observations, classified_samples)
        )

    async def _propose(self, started, token, observations, classified_samples):
        try:
            applied = await self.llm.propose_turn(
                started=started,
                epoch=token[0],
                turn_id=token[1],
                classified_samples=classified_samples,
            )
            if self._live():
                deferred = (
                    not applied
                    and not started
                    and classified_samples is not None
                    and self.llm.manual.turn_id == token[1]
                    and self.llm.manual.speaking
                    and self.llm.captured_input_samples > classified_samples
                )
                # Projection acknowledgements contain no endpoint evidence.
                # Handle them independently of the audio/VAD FIFO: a recognized
                # pending onset must reach the existing interruption/clear
                # barrier even while the next native VAD job is blocked.
                self.boundary_task = None
                await self._accept_boundary(
                    _Boundary(
                        started, applied, token[0], self.llm.manual.turn_id, observations, deferred
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("boundary")

    async def _accept_boundary(self, boundary):
        self._record(
            "start" if boundary.started else "stop",
            applied=boundary.applied,
            deferred=boundary.deferred,
            **boundary.observations,
        )
        if not self._live():
            return
        if boundary.deferred and boundary.accepted_turn == self.llm.manual.turn_id:
            # The controller never admitted this stop. Speech observed while
            # it waited is continuation of the same open turn, not a new turn.
            self._active = True
            self._owned_turn_id = boundary.accepted_turn
            self._pending_start = None
            self._turn_start = round(boundary.observations["turn_start_source_ms"] * 8)
            self._quiet_ms = (self.processed_samples - self._last_speech_end) // 8
            self._last_probe_quiet_ms = 0
            self._candidate = None
            await self._consider_endpoint()
            return
        if not boundary.applied or boundary.accepted_turn != self.llm.manual.turn_id:
            self._fail("boundary_rejected")
            return
        if boundary.started:
            self._owned_turn_id = boundary.accepted_turn
        elif self._pending_start is not None:
            token, observations = self._pending_start
            self._pending_start = None
            self._launch_boundary(True, token, observations)
        await self._consider_endpoint()

    async def _consider_endpoint(self):
        if (
            not self._active
            or self.boundary_task is not None
            or self._owned_turn_id != self.llm.manual.turn_id
            or self._quiet_ms < self.config.pause_ms
        ):
            return
        if self._last_speech_end <= self._context_start:
            self._candidate = None
            return
        if self._candidate is not None:
            await self._maybe_stop()
        elif self.endpoint_task is None and (
            not self._last_probe_quiet_ms
            or self._quiet_ms - self._last_probe_quiet_ms >= self.config.retry_ms
        ):
            self._last_probe_quiet_ms = self._quiet_ms
            context_start = max(
                self._context_start, self._turn_start - self.config.pre_speech_ms * 8, 0
            )
            context = self._context[(context_start - self._context_start) * 2 :]
            token = (self.epoch, self._owned_turn_id, self._speech_revision)
            self.endpoint_task = asyncio.create_task(
                self._predict(
                    token, self.processed_samples, context_start, self._last_received_at, context
                )
            )

    async def _predict(self, token, sample_end, context_start, received_at, pcm):
        scheduled = self.clock()
        try:
            probability, started, finished = await self._inference(
                self.endpoint_executor, self.models.completion_probability, pcm
            )
            available = self.clock()
            probability = self._probability(probability)
            if self._live():
                # Audio that arrived during inference is already ahead of this
                # result; it must be classified before endpoint admission.
                self.queue.put_nowait(
                    _Prediction(
                        *token,
                        sample_end,
                        context_start,
                        received_at,
                        scheduled,
                        started,
                        finished,
                        available,
                        probability,
                    )
                )
        except asyncio.CancelledError:
            raise
        except _InferenceCapacityError:
            self._fail("capacity")
        except Exception:
            self._fail("endpoint_inference")

    def _current(self, prediction):
        return (
            self._live()
            and self._active
            and self.boundary_task is None
            and self._owned_turn_id == prediction.turn_id
            and prediction.epoch == self.llm.turn_epoch
            and prediction.turn_id == self.llm.manual.turn_id
            and prediction.speech_revision == self._speech_revision
            and self._last_speech_end > self._context_start
        )

    async def _accept_prediction(self, prediction):
        self.endpoint_predictions += 1
        current = self._current(prediction)
        if not current:
            self.stale_predictions += 1
        self._record(
            "prediction",
            current=current,
            probability=prediction.probability,
            source_end_ms=prediction.sample_end / 8,
            context_start_ms=prediction.context_start / 8,
            received_at_ms=(prediction.received_at - self.origin) * 1000,
            scheduled_at_ms=(prediction.scheduled_at - self.origin) * 1000,
            inference_started_at_ms=(prediction.inference_started_at - self.origin) * 1000,
            inference_finished_at_ms=(prediction.inference_finished_at - self.origin) * 1000,
            available_at_ms=(prediction.available_at - self.origin) * 1000,
        )
        if current and prediction.probability > 0.5:
            self._candidate = prediction
            await self._maybe_stop()

    async def _maybe_stop(self):
        prediction = self._candidate
        if prediction is None or not self._current(prediction):
            self._candidate = None
            return
        if self.processed_samples - prediction.sample_end < self.config.settle_ms * 8:
            return
        # A result can enter the FIFO before audio that arrives while earlier
        # audio is still being classified. Never let the result overtake any
        # captured sample, including PCM still in Pipecat's upstream queues or
        # an incomplete Silero frame. Further actual input may finish that
        # frame; EOF does not pad it into quiet evidence.
        if (
            self.processed_samples != self.input_samples
            or self.processed_samples != self.llm.captured_input_samples
        ):
            return
        self._candidate = None
        self._active = False
        self._voice_ms = self._quiet_ms = self._last_probe_quiet_ms = 0
        self._launch_boundary(
            False,
            (prediction.epoch, prediction.turn_id),
            {
                "source_end_ms": self.processed_samples / 8,
                "turn_start_source_ms": self._turn_start / 8,
                "verdict_source_end_ms": prediction.sample_end / 8,
                "probability": prediction.probability,
            },
            classified_samples=self.processed_samples,
        )

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._candidate = None
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        # propose_turn deliberately shields its controller-owned projection.
        # Cancelling our awaiting task alone would leave a late stop admissible.
        # The sole turn source ending revokes and joins that ownership as well.
        try:
            await self.llm.close_external_turns()
        finally:
            tasks = [
                task
                for task in (self.worker, self.endpoint_task, self.boundary_task)
                if task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Running native inference cannot be killed. Its process-wide
            # admission permit stays occupied until the actual future finishes.
            while not self.queue.empty():
                self.queue.get_nowait()
            self._context = self._partial = b""
            self.backlog_samples = 0
            self.models = None

    async def cleanup(self):
        await self.close()
        await super().cleanup()
