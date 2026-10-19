"""Packet clocks, call-state isolation and failure fallback; no network or weights."""

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

pytest.importorskip("pipecat")
from pipecat.frames.frames import FilterEnableFrame

from dots_phone.config import Settings
from dots_phone.deepfilter_filter import DeepFilterNetFilter, NativeWorker
from dots_phone.input_audio_filter import input_filter_factory


class DelayWorker:
    instances = []

    def __init__(self, *args):
        self.delay = np.zeros(1440, dtype=np.float32)
        self.closed = False
        self.calls = 0
        self.fail_at = None
        self.instances.append(self)

    async def start(self):
        pass

    async def process_samples(self, samples):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("fixture failure")
        data = np.concatenate((self.delay, samples))
        self.delay = data[len(samples) :].copy()
        return data[: len(samples)].copy()

    async def close(self):
        self.closed = True


def make_filter(worker=DelayWorker):
    return DeepFilterNetFilter(
        model=Path("unused-model"),
        library=Path("unused-library"),
        worker_factory=worker,
        timeout_ms=20,
    )


def speech(rate, seconds=1):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 503 * t) * 5000).astype("<i2").tobytes()


def test_off_does_not_import_filter_or_touch_assets(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "dots_phone.deepfilter_filter", None)
    assert input_filter_factory(settings) is None


@pytest.mark.parametrize("old", ["krisp_viva", "voxlock", "rnnoise"])
def test_only_off_and_deepfilter_are_supported(old):
    with pytest.raises(ValidationError):
        Settings(profile="evaluation", pipecat_input_filter=old)


def test_environment_and_missing_install_fail_before_dial(monkeypatch, tmp_path):
    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    monkeypatch.setenv("DOTS_PHONE_VOICE_BACKEND", "pipecat")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_ENGINE", "realtime")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_INPUT_FILTER", "deepfilter")
    monkeypatch.setenv("DOTS_PHONE_DEEPFILTER_MODEL_PATH", str(tmp_path / "missing.tar.gz"))
    monkeypatch.setenv("DOTS_PHONE_DEEPFILTER_FRAME_TIMEOUT_MS", "60")
    settings = Settings.from_env()
    assert settings.deepfilter_frame_timeout_ms == 60
    assert settings.pipecat_noise_reduction == "off"
    assert settings.pipecat_general_vad_threshold == 0.5
    assert not settings.pipecat_external_turns
    with pytest.raises(RuntimeError, match="model missing"):
        input_filter_factory(settings)


async def render(rate, raw, chunks):
    audio_filter = make_filter()
    await audio_filter.start(rate)
    output, offset, index = [], 0, 0
    try:
        while offset < len(raw):
            size = chunks[index % len(chunks)] * 2
            packet = raw[offset : offset + size]
            result = await audio_filter.filter(packet)
            assert len(result) == len(packet)
            output.append(result)
            offset += len(packet)
            index += 1
        assert not audio_filter.bypassed
        assert len(audio_filter._input) < 480
        assert len(audio_filter._raw) == rate * 0.05 * 2
        return b"".join(output)
    finally:
        worker = audio_filter.worker
        await audio_filter.stop()
        assert worker.closed and audio_filter.worker is None


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
async def test_fragmented_packets_preserve_same_causal_output_and_sample_clock(rate):
    raw = speech(rate)
    regular = await render(rate, raw, [rate // 50])
    fragmented = await render(rate, raw, [1, 17, 43, 80, 161, 239, 13])
    assert regular == fragmented
    # Known native delay + packet alignment: 50 ms, without speeding audio up.
    expected = b"\0" * (rate // 20 * 2) + raw[: -(rate // 20 * 2)]
    a, b = np.frombuffer(regular, "<i2"), np.frombuffer(expected, "<i2")
    assert np.corrcoef(a, b)[0, 1] > 0.999


async def test_failure_switches_to_delay_matched_raw_without_replay_or_missing_samples():
    audio_filter = make_filter()
    await audio_filter.start(8000)
    worker = audio_filter.worker
    worker.fail_at = 7
    raw = speech(8000)
    output = []
    bypass_offset = None
    for offset in range(0, len(raw), 320):
        output.append(await audio_filter.filter(raw[offset : offset + 320]))
        if audio_filter.bypassed and bypass_offset is None:
            bypass_offset = offset
    result = b"".join(output)
    expected = b"\0" * 800 + raw[:-800]
    assert len(result) == len(raw)
    assert result[bypass_offset:] == expected[bypass_offset:]
    assert worker.closed and worker.calls == 7 and audio_filter.worker is None
    await audio_filter.stop()


async def test_separate_calls_and_disable_do_not_restart_or_share_native_state():
    one, two = make_filter(), make_filter()
    await one.start(8000)
    await two.start(8000)
    assert one.worker is not two.worker
    worker = one.worker
    await one.filter(speech(8000, 0.02))
    await one.process_frame(FilterEnableFrame(enable=False))
    await one.process_frame(FilterEnableFrame(enable=True))
    assert one.bypassed and one.failure_reason == "disabled" and worker.closed
    assert not two.bypassed
    await one.stop()
    await two.stop()


class StalledWorker(NativeWorker):
    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(60)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        assert await self.process.stdout.readline() == b"ready\n"


async def test_native_pipe_stall_has_a_deadline_and_does_not_end_audio():
    audio_filter = make_filter(StalledWorker)
    await audio_filter.start(8000)
    process = audio_filter.worker.process
    try:
        # First frame may be mostly buffering; this frame necessarily processes.
        async with asyncio.timeout(0.5):
            result = await audio_filter.filter(speech(8000, 0.1))
        assert len(result) == 1600
        assert audio_filter.bypassed and audio_filter.failure_reason == "TimeoutError"
        assert process.returncode is not None
        assert await audio_filter.filter(b"\x01\x02" * 160) == speech(8000, 0.1)[800:1120]
    finally:
        await audio_filter.stop()


async def test_cancelled_pipe_read_kills_worker_and_cannot_contaminate_next_packet():
    audio_filter = make_filter(StalledWorker)
    await audio_filter.start(8000)
    process = audio_filter.worker.process
    pending = asyncio.create_task(audio_filter.filter(speech(8000, 0.1)))
    await asyncio.sleep(0.005)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert audio_filter.bypassed and process.returncode is not None
    await audio_filter.stop()
