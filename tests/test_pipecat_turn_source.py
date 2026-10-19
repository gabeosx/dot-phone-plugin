"""Real local-model controls and asynchronous source/actual-pipeline regressions.

The model controls use bundled ONNX and synthetic speech, never provider VAD or
historical human recordings. Injected models test races, not classifier accuracy.
"""

import asyncio
import audioop
import base64
import hashlib
import json
import threading
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, ProposedUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from dots_phone.pipecat_turn_source import LocalTurnConfig, LocalTurnSource
from tests import test_realtime_manual_integration as integration
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_manual_integration import manual_pipeline as manual_pipeline
from tests.test_realtime_playout import eventually


class Models:
    def __init__(self, completion=0.99):
        self.completion = completion
        self.vad_gate = self.endpoint_gate = None
        self.vad_entered = threading.Event()
        self.endpoint_entered = threading.Event()
        self.inputs = []

    def speech_probability(self, pcm):
        self.inputs.append(pcm)
        self.vad_entered.set()
        if self.vad_gate:
            assert self.vad_gate.wait(3)
        return 0.9 if max(abs(v) for v in array("h", pcm)) > 20 else 0.01

    def completion_probability(self, pcm):
        self.endpoint_entered.set()
        if self.endpoint_gate:
            assert self.endpoint_gate.wait(3)
        return self.completion


class Controller:
    def __init__(self):
        self.turn_epoch = 0
        self._external_closed = False
        self.manual = SimpleNamespace(turn_id=0, speaking=False)
        self.captured_input_samples = 0
        self.ready = asyncio.Event()
        self.ready.set()
        self.boundaries = []
        self.voice_failure = MagicMock()

    def recognize_onset(self, *, epoch, turn_id):
        return (
            epoch == self.turn_epoch
            and turn_id == self.manual.turn_id
            and not self._external_closed
        )

    async def propose_turn(self, *, started, epoch, turn_id, classified_samples=None):
        await self.ready.wait()
        if epoch != self.turn_epoch or turn_id != self.manual.turn_id or self._external_closed:
            return False
        if not started and classified_samples != self.captured_input_samples:
            return False
        self.boundaries.append((started, epoch, turn_id))
        self.manual.speaking = started
        if started:
            self.manual.turn_id += 1
        return True

    async def close_external_turns(self):
        self._external_closed = True
        self.turn_epoch += 1


def make_source(models, **config):
    llm = Controller()
    source = LocalTurnSource(llm, config=LocalTurnConfig(**config), model_factory=lambda: models)
    source.push_frame = AsyncMock()
    source.worker = asyncio.create_task(source._run())
    return source, llm


async def feed(source, milliseconds, *, speech=False, pcm=None):
    payload = pcm if pcm is not None else (b"\x00\x04" if speech else b"\0\0") * (milliseconds * 8)
    frame = InputAudioRawFrame(payload, 8000, 1)
    frame.metadata["manual_input_accepted"] = True
    source.llm.captured_input_samples += len(payload) // 2
    frame.metadata["manual_input_sample_end"] = source.llm.captured_input_samples
    await source.process_frame(frame, FrameDirection.DOWNSTREAM)
    return frame


async def drain(source):
    await eventually(lambda: source.backlog_samples == 0 or source.failed)
    assert not source.failed


async def test_forwarding_and_pcm_snapshot_survive_blocked_vad():
    models = Models()
    models.vad_gate = threading.Event()
    source, llm = make_source(models, max_backlog_ms=320)
    try:
        pcm = bytearray(b"\x00\x04" * 256)
        await feed(source, 32, pcm=pcm)
        pcm[:] = b"\0" * len(pcm)
        await eventually(models.vad_entered.is_set)
        await asyncio.wait_for(feed(source, 96, speech=True), 0.05)
        assert source.push_frame.await_count == 2
        assert models.inputs[0] == b"\x00\x04" * 256
        await feed(source, 224)
        llm.voice_failure.assert_called_once()
        assert source.failed and llm.boundaries == []
    finally:
        models.vad_gate.set()
        await source.close()


async def test_pre_ready_start_keeps_vad_moving_and_end_uses_new_turn():
    source, llm = make_source(Models(), settle_ms=0, max_backlog_ms=320)
    llm.ready.clear()
    try:
        await feed(source, 96, speech=True)
        await drain(source)
        for _ in range(5):
            await feed(source, 128)
            await drain(source)
        assert source.processed_samples == 736 * 8
        assert llm.boundaries == [] and source.endpoint_predictions == 0
        llm.ready.set()
        await eventually(lambda: len(llm.boundaries) == 2)
        assert llm.boundaries == [(True, 0, 0), (False, 0, 1)]
    finally:
        await source.close()


@pytest.mark.parametrize("supersede", ["speech", "turn", "epoch"])
async def test_delayed_completion_cannot_end_resumed_or_replaced_turn(supersede):
    models = Models()
    models.endpoint_gate = threading.Event()
    source, llm = make_source(models, settle_ms=0)
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        await feed(source, 320)
        await eventually(models.endpoint_entered.is_set)
        if supersede == "speech":
            await feed(source, 96, speech=True)
            await drain(source)
        elif supersede == "turn":
            llm.manual.turn_id += 1
        else:
            llm.turn_epoch += 1
        models.endpoint_gate.set()
        await eventually(lambda: source.endpoint_task is None or source.endpoint_task.done())
        assert not any(not started for started, _, _ in llm.boundaries)
        if supersede != "epoch":
            await eventually(lambda: source.stale_predictions == 1)
    finally:
        models.endpoint_gate.set()
        await source.close()


async def test_stabilization_requires_real_quiet_samples_and_eof_cannot_supply_them():
    source, llm = make_source(Models())
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        await feed(source, 320)
        await eventually(lambda: source.endpoint_predictions == 1)
        await asyncio.sleep(0.25)
        assert len(llm.boundaries) == 1
        await feed(source, 192)
        await drain(source)
        assert len(llm.boundaries) == 1
        await source.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert source.closed and len(llm.boundaries) == 1
    finally:
        await source.close()


async def test_positive_endpoint_and_clock_order_use_observed_audio():
    source, llm = make_source(Models())
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        await feed(source, 320)
        await eventually(lambda: source.endpoint_predictions == 1)
        await feed(source, 224)
        await eventually(lambda: len(llm.boundaries) == 2)
        report = source.report()
        prediction = next(row for row in report["events"] if row["kind"] == "prediction")
        assert prediction["source_end_ms"] == 416
        assert prediction["received_at_ms"] <= prediction["scheduled_at_ms"]
        assert prediction["scheduled_at_ms"] <= prediction["inference_started_at_ms"]
        assert prediction["inference_started_at_ms"] <= prediction["inference_finished_at_ms"]
        assert prediction["inference_finished_at_ms"] <= prediction["available_at_ms"]
        assert prediction["available_at_ms"] <= prediction["handled_at_ms"] + 0.001
        assert report["input_samples"] == report["processed_samples"] == 640 * 8
    finally:
        await source.close()


async def test_incomplete_prediction_context_expiry_and_partial_frame_never_force_stop():
    source, llm = make_source(Models(completion=0.01), context_ms=512, pre_speech_ms=128)
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        for _ in range(10):
            await feed(source, 128)
            await drain(source)
        await feed(source, 12)
        await drain(source)
        await asyncio.sleep(0.05)
        assert len(llm.boundaries) == 1
        assert len(source._context) // 2 <= 512 * 8
        assert source.report()["partial_samples"] == 12 * 8
    finally:
        await source.close()


async def test_close_revokes_running_endpoint():
    models = Models()
    models.endpoint_gate = threading.Event()
    source, llm = make_source(models, settle_ms=0)
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        await feed(source, 320)
        await eventually(models.endpoint_entered.is_set)
        await asyncio.wait_for(source.close(), 0.1)
        models.endpoint_gate.set()
        await asyncio.sleep(0.05)
        assert len(llm.boundaries) == 1
        assert source.worker.done() and source.endpoint_task.done()
    finally:
        models.endpoint_gate.set()
        await source.close()


@pytest.fixture
def settings(settings, monkeypatch, request):
    monkeypatch.setattr(
        integration,
        "ScriptedSource",
        LocalTurnSource
        if getattr(request, "param", None) == "bundled"
        else lambda llm: LocalTurnSource(
            llm, config=LocalTurnConfig(settle_ms=0), model_factory=Models
        ),
    )
    return settings


async def test_actual_pipeline_binds_source_commits_once_and_delivers_response(manual_pipeline):
    p = manual_pipeline
    payload = audioop.lin2ulaw(b"\x00\x04" * (96 * 8) + b"\0\0" * (640 * 8), 2)
    for offset in range(0, len(payload), 160):
        p.carrier.feed(
            {
                "event": "media",
                "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
            }
        )
    await eventually(lambda: integration.count_wire(p.model, "response.create") == 1)
    assert integration.count_wire(p.model, "input_audio_buffer.commit") == 1
    assert p.llm.manual.turn_id == 1 and p.carrier.clears == 1
    assert p.source.processed_samples + len(p.source._partial) // 2 == len(payload)
    assert p.source.input_samples == len(payload)
    p.model.response("local_reply", milliseconds=200, frequency=710)
    await eventually(lambda: p.carrier.audio_bytes == 1600)
    integration.assert_waveform(
        p.carrier.audio_after(0), milliseconds=200, frequency=710, absent_frequency=300
    )


def read_fixture(name):
    from tests.audio_assets import audio_asset

    root = Path(__file__).with_name("fixtures")
    manifest = json.loads(
        (
            root
            / ("turn-source-manifest.json" if name.startswith("turn-source") else "manifest.json")
        ).read_text()
    )
    path = audio_asset(name)
    assert manifest["synthetic"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["clips"][name]["sha256"]
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 8000)
        return wav.readframes(wav.getnframes())


def require_local_models():
    pytest.importorskip(
        "onnxruntime", reason="Optional local-turn inference dependencies unavailable"
    )
    pytest.importorskip(
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        reason="Optional bundled SmartTurn inference dependencies unavailable",
    )


@pytest.mark.parametrize(
    "fixture,gain,noise",
    [
        ("turn-source-stop.wav", 1.0, False),
        ("turn-source-stop.wav", 0.125, False),
        ("turn-source-stop.wav", 1.0, True),
        ("synthetic-question.wav", 1.0, False),
        ("synthetic-question.wav", 1.0, True),
        ("synthetic-question.wav", 0.125, True),
    ],
)
async def test_bundled_models_recognize_synthetic_speech_and_endpoint_offline(fixture, gain, noise):
    require_local_models()
    # These are authored non-speech-noise controls, not foreground attribution
    # validation. Tail PCM is explicitly part of this synthetic test input.
    pcm = audioop.mul(read_fixture(fixture), 2, gain)
    pcm += b"\0\0" * (1600 * 8)
    if noise:
        rng = np.random.default_rng(17)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        samples += rng.normal(0, 180, len(samples)).astype(np.int32)
        pcm = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    pcm = audioop.ulaw2lin(audioop.lin2ulaw(pcm, 2), 2)
    llm = Controller()
    source = LocalTurnSource(llm)
    source.push_frame = AsyncMock()
    source.worker = asyncio.create_task(source._run())
    try:
        await eventually(lambda: source.models is not None or source.failed, timeout=5)
        assert not source.failed
        # Pace at the same 20ms cadence as carrier media. This measures actual
        # inference availability instead of accelerated replay timestamps.
        for offset in range(0, len(pcm), 320):
            await feed(source, 0, pcm=pcm[offset : offset + 320])
            await asyncio.sleep(0.02)
        await drain(source)
        await eventually(lambda: len(llm.boundaries) >= 2, timeout=2)
        assert llm.boundaries[0][0] is True and llm.boundaries[-1][0] is False
        assert source.endpoint_predictions > 0 and not source.failed
        assert source.input_samples == len(pcm) // 2
        assert source.processed_samples + len(source._partial) // 2 == len(pcm) // 2
        start = next(row for row in source.events if row["kind"] == "start")
        assert start["source_end_ms"] <= 500
    finally:
        await source.close()


async def test_audio_received_after_prediction_is_classified_before_stop_admission():
    models = Models()
    models.endpoint_gate = threading.Event()
    source, llm = make_source(models)
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(llm.boundaries) == 1)
        await feed(source, 320)
        await eventually(models.endpoint_entered.is_set)
        await drain(source)
        models.vad_entered.clear()
        models.vad_gate = threading.Event()
        await feed(source, 224)
        await eventually(models.vad_entered.is_set)
        models.endpoint_gate.set()
        await eventually(lambda: source.endpoint_task.done())
        # This continuation sits after the completed prediction in the FIFO.
        # The preceding quiet packet already supplies enough stabilization.
        await feed(source, 96, speech=True)
        models.vad_gate.set()
        await drain(source)
        assert source.endpoint_predictions == 1
        assert source._candidate is None
        assert llm.boundaries == [(True, 0, 0)]
    finally:
        models.endpoint_gate.set()
        if models.vad_gate:
            models.vad_gate.set()
        await source.close()


async def test_captured_audio_waiting_before_source_blocks_endpoint_commit(
    manual_pipeline, monkeypatch
):
    p = manual_pipeline
    await eventually(lambda: p.source.models is not None)
    models = p.source.models
    models.endpoint_gate = threading.Event()
    source_entered, release_source = asyncio.Event(), asyncio.Event()
    first = audioop.lin2ulaw(b"\x00\x04" * (96 * 8) + b"\0\0" * (320 * 8), 2)
    continuation = integration.synthetic_input(96, frequency=510, amplitude=140)

    def media(payload):
        for offset in range(0, len(payload), 160):
            p.carrier.feed(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
                }
            )

    try:
        media(first)
        await eventually(models.endpoint_entered.is_set)
        await drain(p.source)
        assert p.source.processed_samples == p.source.input_samples == len(first)
        assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0

        original_process = p.source.process_frame

        async def delayed_source_input(frame, direction):
            if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
                # The real ManualInputCapture has already accepted this frame.
                # Delay only source processing; the independent endpoint worker
                # and the normal SDK/input FIFO remain live.
                assert frame.metadata.get("manual_input_accepted") is True
                source_entered.set()
                await release_source.wait()
            await original_process(frame, direction)

        monkeypatch.setattr(p.source, "process_frame", delayed_source_input)
        media(continuation)
        await source_entered.wait()
        await eventually(lambda: p.llm.media_progress["input_ms_received"] == 512)
        await p.llm.manual.wait_idle()
        assert p.source.processed_samples == p.source.input_samples == len(first)
        assert p.source.report()["queued_before_source_samples"] == len(continuation)
        assert p.source.report()["unclassified_samples"] == len(continuation)

        models.endpoint_gate.set()
        await eventually(lambda: p.source.endpoint_predictions == 1)
        try:
            await asyncio.wait_for(p.model.commit_entered.wait(), 0.2)
        except TimeoutError:
            pass
        commits = integration.count_wire(p.model, "input_audio_buffer.commit")
        if commits:
            committed, pending = integration.committed_pcm(p.model)
            assert commits == 1 and not pending
            # Real SOXR flush/commit includes every captured continuation sample,
            # even though none of that continuation reached the detector yet.
            assert len(committed[0]) == (len(first) + len(continuation)) * 6
        assert commits == 0, (
            f"Endpoint committed {len(first) + len(continuation)} captured samples "
            f"after classifying only {p.source.processed_samples}; "
            "continuation is still blocked before LocalTurnSource.process_frame"
        )
        release_source.set()
        await eventually(lambda: p.source.processed_samples == len(first) + len(continuation))
        assert not p.source.failed and p.llm.manual.speaking and p.llm.manual.turn_id == 1
        assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0
        quiet = audioop.lin2ulaw(b"\0\0" * (320 * 8), 2)
        media(quiet)
        await eventually(lambda: integration.count_wire(p.model, "input_audio_buffer.commit") == 1)
        committed, _ = integration.committed_pcm(p.model)
        assert len(committed[0]) == (len(first) + len(continuation) + len(quiet)) * 6
    finally:
        models.endpoint_gate.set()
        release_source.set()
        # Revoke before releasing the fixture: the deliberately withheld PCM
        # must not start a second boundary concurrently with pipeline teardown.
        await asyncio.wait_for(p.source.close(), 1)


async def test_continuation_during_stop_admission_defers_and_retries_same_turn(manual_pipeline):
    p = manual_pipeline

    def media(pcm):
        payload = audioop.lin2ulaw(pcm, 2)
        for offset in range(0, len(payload), 160):
            p.carrier.feed(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
                }
            )

    media(b"\x00\x04" * (96 * 8))
    await eventually(lambda: p.source._owned_turn_id == 1)
    await p.llm._proposal_lock.acquire()
    try:
        media(b"\0\0" * (320 * 8))
        await eventually(lambda: len(p.llm._proposal_tasks) == 1)
        media(b"\x00\x04" * (96 * 8))
        await eventually(lambda: p.source.processed_samples == 512 * 8)
        assert p.source._pending_start is not None
        assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0
    finally:
        p.llm._proposal_lock.release()
    await eventually(lambda: any(row.get("deferred") for row in p.source.events))
    assert not p.source.failed
    assert p.source._active and p.source._pending_start is None
    assert p.source._owned_turn_id == p.llm.manual.turn_id == 1
    assert p.llm.manual.speaking and p.llm._proposed_open
    assert p.carrier.clears == 1
    media(b"\0\0" * (320 * 8))
    await eventually(lambda: integration.count_wire(p.model, "response.create") == 1)
    committed, pending = integration.committed_pcm(p.model)
    assert len(committed) == 1 and not pending
    assert len(committed[0]) == 832 * 8 * 6
    assert not p.source.failed and p.llm.manual.turn_id == 1


@pytest.mark.parametrize("close_before_ack", [False, True])
async def test_audio_after_validated_stop_stays_behind_cutoff_while_projection_waits(
    manual_pipeline, monkeypatch, close_before_ack
):
    p = manual_pipeline
    projecting, release_projection = asyncio.Event(), asyncio.Event()
    original_broadcast = p.llm.broadcast_frame

    async def delayed_stop(frame_class, **kwargs):
        if frame_class is ProposedUserStoppedSpeakingFrame:
            projecting.set()
            await release_projection.wait()
        await original_broadcast(frame_class, **kwargs)

    monkeypatch.setattr(p.llm, "broadcast_frame", delayed_stop)

    def media(pcm):
        payload = audioop.lin2ulaw(pcm, 2)
        for offset in range(0, len(payload), 160):
            p.carrier.feed(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
                }
            )

    try:
        media(b"\x00\x04" * (96 * 8) + b"\0\0" * (320 * 8))
        await asyncio.wait_for(projecting.wait(), 2)
        assert not p.llm.manual.speaking
        media(b"\x00\x04" * (96 * 8))
        await eventually(lambda: p.source.processed_samples == 512 * 8)
        assert p.source._pending_start is not None
        assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0
        # No commit or response is allowed before projection acknowledges the
        # admitted stop. Later raw PCM remains queued behind that stop.
        assert p.llm.media_progress["input_ms_dispatched"] == 416
        assert integration.count_wire(p.model, "response.create") == 0
        if close_before_ack:
            await asyncio.wait_for(p.source.close(), 1)
            release_projection.set()
            await asyncio.sleep(0.05)
            assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0
            assert integration.count_wire(p.model, "response.create") == 0
            assert p.llm.manual.closed and not p.llm._stop_projections
            return
        release_projection.set()
        await eventually(lambda: integration.count_wire(p.model, "input_audio_buffer.commit") == 1)
        await eventually(lambda: p.source._owned_turn_id == 2)
        await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
        committed, _ = integration.committed_pcm(p.model)
        assert len(committed) == 1 and len(committed[0]) == 416 * 8 * 6
        assert p.llm.media_progress["input_ms_dispatched"] == 512
        assert p.llm.manual.speaking and not p.source.failed
    finally:
        release_projection.set()
        await asyncio.wait_for(p.source.close(), 1)


@pytest.mark.parametrize("response_timing", ["after_onset", "late_response", "queued_playback"])
async def test_recognized_onset_revokes_reply_and_clears_without_waiting_for_next_vad(
    manual_pipeline, monkeypatch, response_timing, record_testsuite_property
):
    p = manual_pipeline
    projecting, release_projection = asyncio.Event(), asyncio.Event()
    voice_entered, quiet_entered = threading.Event(), threading.Event()
    voice_gate, quiet_gate = threading.Event(), threading.Event()
    original_broadcast = p.llm.broadcast_frame

    async def delayed_stop(frame_class, **kwargs):
        if frame_class is ProposedUserStoppedSpeakingFrame:
            projecting.set()
            await release_projection.wait()
        await original_broadcast(frame_class, **kwargs)

    monkeypatch.setattr(p.llm, "broadcast_frame", delayed_stop)

    def media(pcm):
        payload = audioop.lin2ulaw(pcm, 2)
        for offset in range(0, len(payload), 160):
            p.carrier.feed(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
                }
            )

    try:
        media(b"\x00\x04" * (96 * 8) + b"\0\0" * (320 * 8))
        await asyncio.wait_for(projecting.wait(), 2)
        original_vad = p.source.models.speech_probability

        def delayed_vad(pcm):
            if max(abs(value) for value in array("h", pcm)) > 20:
                voice_entered.set()
                assert voice_gate.wait(3)
            else:
                quiet_entered.set()
                assert quiet_gate.wait(3)
            return original_vad(pcm)

        monkeypatch.setattr(p.source.models, "speech_probability", delayed_vad)
        if response_timing == "after_onset":
            voice_gate.set()
        media(b"\x00\x04" * (96 * 8) + b"\0\0" * (32 * 8))
        if response_timing != "after_onset":
            await eventually(voice_entered.is_set)
            release_projection.set()
            await eventually(lambda: integration.count_wire(p.model, "response.create") == 1)
            if response_timing == "queued_playback":
                p.model.response("previous", milliseconds=1200, frequency=710)
                await eventually(lambda: p.carrier.audio_bytes >= 800)
                assert p.carrier.audio_bytes < 9600
            voice_gate.set()
        await eventually(quiet_entered.is_set)
        onset = [row for row in p.source.events if row["kind"] == "onset"][-1]
        assert onset["turn_id"] == 1
        assert not p.llm.manual.can_respond(1)
        if response_timing == "after_onset":
            assert p.source._pending_start is not None
            release_projection.set()
        elif response_timing == "late_response":
            p.model.response("previous", milliseconds=200, frequency=710)
            await eventually(lambda: p.llm.stale_delta_bytes > 0)

        # The source worker remains blocked in its next VAD job. Lifecycle
        # acknowledgement and the single existing clear must not wait for it.
        await eventually(lambda: p.source._owned_turn_id == 2, timeout=0.3)
        assert p.carrier.clears == 2 and not quiet_gate.is_set()
        assert p.source._pending_start is None
        clear = p.carrier.clear_events[-1]
        onset_to_clear = (clear.at - p.source.origin) * 1000 - onset["handled_at_ms"]
        record_testsuite_property(
            f"projection_{response_timing}_onset_to_clear_ms", round(onset_to_clear, 3)
        )
        assert 0 <= onset_to_clear < 250
        await asyncio.sleep(0.05)
        assert p.carrier.audio_after(clear.event_index) == b""
        assert integration.count_wire(p.model, "response.create") == (
            0 if response_timing == "after_onset" else 1
        )
        if response_timing != "queued_playback":
            assert p.carrier.audio_bytes == 0

        quiet_gate.set()
        await eventually(lambda: p.source.processed_samples == 544 * 8)
        media(b"\0\0" * (320 * 8))
        await eventually(
            lambda: (
                integration.count_wire(p.model, "response.create")
                == (1 if response_timing == "after_onset" else 2)
            )
        )
        committed, pending = integration.committed_pcm(p.model)
        assert not pending and [len(pcm) for pcm in committed] == [416 * 8 * 6, 448 * 8 * 6]
        before_reply = p.carrier.audio_bytes
        p.model.response("fresh", milliseconds=200, frequency=900)
        await eventually(lambda: p.carrier.audio_bytes == before_reply + 1600)
        integration.assert_waveform(
            p.carrier.audio_after(clear.event_index),
            milliseconds=200,
            frequency=900,
            absent_frequency=710,
        )
        assert not p.source.failed and p.llm.manual.turn_id == 2 and p.carrier.clears == 2
    finally:
        release_projection.set()
        voice_gate.set()
        quiet_gate.set()
        await asyncio.wait_for(p.source.close(), 1)


async def test_cancelled_close_stays_joinable_until_local_workers_are_cleaned():
    source, llm = make_source(Models())
    gate = asyncio.Event()

    async def close_controller():
        llm._external_closed = True
        llm.turn_epoch += 1
        await gate.wait()

    llm.close_external_turns = close_controller
    first = asyncio.create_task(source.close())
    await eventually(lambda: source.closed)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert not source._close_task.done()
    gate.set()
    await source.close()
    assert source._close_task.done() and source.worker.done()


async def test_source_close_revokes_shielded_stop_in_actual_pipeline(manual_pipeline):
    p = manual_pipeline

    async def media(milliseconds, speech=False):
        payload = audioop.lin2ulaw((b"\x00\x04" if speech else b"\0\0") * (milliseconds * 8), 2)
        for offset in range(0, len(payload), 160):
            p.carrier.feed(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
                }
            )

    await media(96, True)
    await eventually(lambda: p.source._owned_turn_id == 1)
    await p.llm._proposal_lock.acquire()
    try:
        await media(640)
        await eventually(lambda: len(p.llm._proposal_tasks) == 1)
        await asyncio.wait_for(p.source.close(), 1)
        assert p.llm._external_closed and not p.llm._proposal_tasks
    finally:
        p.llm._proposal_lock.release()
    await asyncio.sleep(0.05)
    assert integration.count_wire(p.model, "input_audio_buffer.commit") == 0
    assert integration.count_wire(p.model, "response.create") == 0
    await p.source.close()


@pytest.mark.parametrize("settings", ["bundled"], indirect=True)
async def test_real_quiet_stop_detector_drives_actual_pipeline(manual_pipeline):
    require_local_models()
    p = manual_pipeline
    await eventually(lambda: p.source.models is not None or p.source.failed, timeout=5)
    assert not p.source.failed
    pcm = audioop.mul(read_fixture("turn-source-stop.wav"), 2, 0.125)
    payload = audioop.lin2ulaw(pcm + b"\0\0" * (1600 * 8), 2)
    for offset in range(0, len(payload), 160):
        p.carrier.feed(
            {
                "event": "media",
                "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
            }
        )
        await asyncio.sleep(0.02)
    await eventually(lambda: integration.count_wire(p.model, "response.create") >= 1)
    assert not p.source.failed
    assert p.source.input_samples == len(payload)
    p.model.response("real_detector_reply", milliseconds=200, frequency=710)
    await eventually(lambda: p.carrier.audio_bytes == 1600)
    integration.assert_waveform(
        p.carrier.audio_after(0), milliseconds=200, frequency=710, absent_frequency=300
    )


async def test_hung_native_jobs_remain_bounded_across_closed_sources(monkeypatch):
    from dots_phone import pipecat_turn_source as module

    pool = module._InferencePool(2, "turn-source-test")
    monkeypatch.setattr(module, "_VAD_POOL", pool)
    gates = [threading.Event(), threading.Event()]
    sources = []
    try:
        for gate in gates:
            model = Models()
            model.vad_gate = gate
            source, _ = make_source(model)
            sources.append(source)
            await eventually(lambda source=source: source.models is not None)
            await feed(source, 32, speech=True)
            await eventually(model.vad_entered.is_set)
        assert pool.outstanding == 2
        rejected, controller = make_source(Models())
        sources.append(rejected)
        await eventually(lambda: rejected.failed)
        assert controller.voice_failure.call_args.args[0] == "external_source_capacity"
        # Cancellation of the asyncio wrappers does not free native worker slots.
        await asyncio.wait_for(asyncio.gather(*(s.close() for s in sources)), 0.1)
        assert pool.outstanding == 2
        another, controller = make_source(Models())
        sources.append(another)
        await eventually(lambda: another.failed)
        assert controller.voice_failure.call_args.args[0] == "external_source_capacity"
        assert pool.outstanding == 2
        for gate in gates:
            gate.set()
        await eventually(lambda: pool.outstanding == 0)
        recovered, controller = make_source(Models())
        sources.append(recovered)
        await eventually(lambda: recovered.models is not None)
        await feed(recovered, 96, speech=True)
        await eventually(lambda: len(controller.boundaries) == 1)
        assert not recovered.failed
    finally:
        for gate in gates:
            gate.set()
        await asyncio.gather(*(s.close() for s in sources))
        await eventually(lambda: pool.outstanding == 0)
        pool._executor.shutdown(wait=False, cancel_futures=True)


async def test_timed_out_native_endpoint_keeps_shared_permit_until_actual_exit(monkeypatch):
    from dots_phone import pipecat_turn_source as module

    pool = module._InferencePool(1, "turn-source-end-test")
    monkeypatch.setattr(module, "_ENDPOINT_POOL", pool)
    gate = threading.Event()
    model = Models()
    model.endpoint_gate = gate
    source, controller = make_source(model, inference_timeout_ms=100)
    try:
        await feed(source, 96, speech=True)
        await eventually(lambda: len(controller.boundaries) == 1)
        await feed(source, 320)
        await eventually(model.endpoint_entered.is_set)
        await eventually(lambda: source.failed)
        assert pool.outstanding == 1
        with pytest.raises(module._InferenceCapacityError):
            pool.submit(lambda: 0.99)
        await asyncio.wait_for(source.close(), 0.1)
        assert pool.outstanding == 1
    finally:
        gate.set()
        await source.close()
        await eventually(lambda: pool.outstanding == 0)
        pool._executor.shutdown(wait=False, cancel_futures=True)
