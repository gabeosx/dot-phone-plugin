"""Actual media/SDK/codec regressions with authored external turn boundaries.

No classifier results are asserted. No sockets, DNS, carrier calls, or provider
requests are allowed: both wire peers are memory objects, while the production
manual service, aggregator, frame queues, codec and SOXR resampler are real.
"""

import asyncio
import audioop
import base64
import hashlib
import json
import math
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from pydantic import SecretStr

pytest.importorskip("soxr")

import soxr

pytest.importorskip("pipecat")

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TTSAudioRawFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openai.realtime import llm as realtime_sdk

from dots_phone.pipecat_voice import PipecatTelephony
from tests.test_realtime_interruption_ordering import (
    HarmonicRealtime,
    TimedCarrier,
    assert_waveform,
)
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_playout import eventually


class ScriptedSource(FrameProcessor):
    """Explicit fixture evidence; this source performs no speech detection."""

    def __init__(self, llm):
        super().__init__()
        self.llm = llm
        self.epoch = llm.turn_epoch
        self.audio = []
        self.boundaries = {}

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            assert frame.metadata.get("manual_input_accepted") is True
            self.audio.append(frame.audio)
            if len(self.audio) in self.boundaries:
                assert await self.llm.propose_turn(
                    started=self.boundaries[len(self.audio)],
                    epoch=self.epoch,
                    turn_id=self.llm.manual.turn_id,
                )
        await self.push_frame(frame, direction)

    async def start_turn(self):
        return await self.llm.propose_turn(
            started=True, epoch=self.epoch, turn_id=self.llm.manual.turn_id
        )

    async def end_turn(self):
        return await self.llm.propose_turn(
            started=False, epoch=self.epoch, turn_id=self.llm.manual.turn_id
        )


class CommitRealtime(HarmonicRealtime):
    def __init__(self):
        super().__init__()
        self.ack_commits = True
        self.commit_entered = asyncio.Event()
        self.commit_count = 0

    def acknowledge_commit(self):
        self.emit({"type": "input_audio_buffer.committed", "item_id": f"input_{self.commit_count}"})

    async def send(self, message):
        await super().send(message)
        if json.loads(message)["type"] == "input_audio_buffer.commit":
            self.commit_count += 1
            self.commit_entered.set()
            if self.ack_commits:
                self.acknowledge_commit()


def count_wire(model, kind):
    return sum(event["type"] == kind for event in model.sent)


def synthetic_input(milliseconds=400, *, frequency=310, amplitude=1100):
    """A known waveform for byte integrity, not a proxy for human speech."""
    pcm = array(
        "h",
        (
            int(amplitude * math.sin(2 * math.pi * frequency * i / 8000))
            for i in range(milliseconds * 8)
        ),
    ).tobytes()
    return audioop.lin2ulaw(pcm, 2)


@pytest.fixture
async def manual_pipeline(service, settings, brief, monkeypatch):
    model = CommitRealtime()
    connect = MagicMock()

    async def memory_connect(**kwargs):
        connect(**kwargs)
        return model

    monkeypatch.setattr(realtime_sdk, "websocket_connect", memory_connect)
    settings.pipecat_engine = "realtime"
    settings.voice_backend = "pipecat"
    settings.pipecat_external_turns = True
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-model-fixture")
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("Manual regression must never dial")
    sources = []

    def factory(llm):
        source = ScriptedSource(llm)
        sources.append(source)
        return source

    telephony = PipecatTelephony(settings, service, client, external_turn_source_factory=factory)
    call_id = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", call_id)
    call.update(state="active", provider_sid="CA" + "1" * 32)
    service.save(call)
    carrier = TimedCarrier(
        {
            "accountSid": settings.twilio_account.get_secret_value(),
            "callSid": call["provider_sid"],
            "streamSid": "MZ" + "3" * 32,
            "customParameters": {"dots_call_id": call_id},
        }
    )
    runner = asyncio.create_task(telephony.media(carrier))
    llm = None
    try:
        await eventually(lambda: call["provider_sid"] in telephony.sessions)
        llm = telephony.sessions[call["provider_sid"]].llm
        await asyncio.wait_for(llm.ready.wait(), 3)
        assert len(sources) == 1
        source = sources[0]
        # External mode explicitly listens first; no unowned startup response.
        await asyncio.sleep(0.05)
        initial_creates = count_wire(model, "response.create")
        assert initial_creates == 0
        updates = [event for event in model.sent if event["type"] == "session.update"]
        assert updates[-1]["session"]["audio"]["input"]["turn_detection"] is None
        yield SimpleNamespace(
            model=model,
            carrier=carrier,
            llm=llm,
            source=source,
            runner=runner,
            service=service,
            call_id=call_id,
            telephony=telephony,
            initial_creates=initial_creates,
            worker=telephony.sessions[call["provider_sid"]].worker,
        )
    finally:
        await carrier.close()
        try:
            await asyncio.wait_for(asyncio.shield(runner), 5)
        except TimeoutError:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            pytest.fail("Manual media pipeline failed to join during teardown")
        if llm is not None:
            assert llm.manual.closed
            assert llm.manual._worker is None or llm.manual._worker.done()
            assert llm.manual._failure_task is None or llm.manual._failure_task.done()
        client.calls.create.assert_not_called()
        assert service.provider.dials == []
        assert connect.call_count == 1
        assert not any(event["type"] == "input_audio_buffer.clear" for event in model.sent)


async def feed(p, payload, *, pace=0):
    assert payload
    before = len(p.source.audio)
    for offset in range(0, len(payload), 160):
        p.carrier.feed(
            {
                "event": "media",
                "media": {
                    "payload": base64.b64encode(payload[offset : offset + 160]).decode(),
                },
            }
        )
        await asyncio.sleep(pace)
    await eventually(lambda: len(p.source.audio) == before + (len(payload) + 159) // 160)


def committed_pcm(model):
    pending, result = [], []
    for event in model.sent:
        if event["type"] == "input_audio_buffer.append":
            pending.append(base64.b64decode(event["audio"]))
        elif event["type"] == "input_audio_buffer.commit":
            result.append(b"".join(pending))
            pending = []
    return result, b"".join(pending)


def assert_committed_input(p, source_segments):
    actual, pending = committed_pcm(p.model)
    assert not pending
    assert len(actual) == len(source_segments)
    for got, source in zip(actual, source_segments, strict=True):
        expected = soxr.resample(
            np.frombuffer(audioop.ulaw2lin(source, 2), dtype=np.int16),
            8000,
            24000,
            quality="VHQ",
        )
        observed = np.frombuffer(got, dtype=np.int16)
        assert len(observed) == len(expected) == len(source) * 3
        # Independent SOXR instances can differ by one-LSB dithering.
        assert np.max(np.abs(observed.astype(np.int32) - expected.astype(np.int32))) <= 2
    assert b"".join(p.source.audio) == audioop.ulaw2lin(b"".join(source_segments), 2)
    assert p.llm.media_progress["input_frames_received"] == len(p.source.audio)
    assert p.llm.media_progress["input_frames_dispatched"] == len(p.source.audio)


async def fresh_reply(p, name="fresh"):
    before = p.carrier.audio_bytes
    p.model.response(name, milliseconds=1200, frequency=900)
    await eventually(lambda: p.carrier.audio_bytes == before + 9600)
    await eventually(lambda: not p.llm.active_responses)
    assert_waveform(
        p.carrier.audio_after(p.carrier.clear_events[-1].event_index),
        milliseconds=1200,
        frequency=900,
        absent_frequency=400,
    )


async def test_media_continuation_invalidates_pending_commit_and_preserves_pcm(manual_pipeline):
    p = manual_pipeline
    first, continuation = synthetic_input(400), synthetic_input(600, frequency=510, amplitude=140)
    assert await p.source.start_turn()
    await feed(p, first)
    p.model.ack_commits = False
    assert await p.source.end_turn()
    await asyncio.wait_for(p.model.commit_entered.wait(), 2)
    assert await p.source.start_turn()
    await feed(p, continuation)
    p.model.acknowledge_commit()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert count_wire(p.model, "response.create") == p.initial_creates
    assert p.carrier.audio_bytes == 0
    p.model.ack_commits = True
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert count_wire(p.model, "response.create") == p.initial_creates + 1
    await fresh_reply(p)
    assert_committed_input(p, [first, continuation])


async def test_media_commit_flushes_real_soxr_tail_before_commit(manual_pipeline, monkeypatch):
    p = manual_pipeline
    tails = []
    original = p.llm.input_resampler.flush

    async def flush():
        tail = await original()
        tails.append(tail)
        return tail

    monkeypatch.setattr(p.llm.input_resampler, "flush", flush)
    payload = synthetic_input(420, amplitude=139)
    assert await p.source.start_turn()
    await feed(p, payload)
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert len(tails) == 1 and len(tails[0]) > 0
    assert_committed_input(p, [payload])
    commits = [
        i for i, event in enumerate(p.model.sent) if event["type"] == "input_audio_buffer.commit"
    ]
    assert len(commits) == 1
    assert base64.b64decode(p.model.sent[commits[0] - 1]["audio"]) == tails[0]
    await fresh_reply(p)


async def test_source_priority_end_cannot_overtake_its_captured_audio(manual_pipeline):
    p = manual_pipeline
    payload = synthetic_input(400)
    p.source.boundaries = {1: True, 20: False}
    await feed(p, payload)
    await eventually(lambda: count_wire(p.model, "input_audio_buffer.commit") == 1)
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert_committed_input(p, [payload])
    assert count_wire(p.model, "response.create") == 1
    await fresh_reply(p)


async def test_first_source_onset_preserves_entire_burst_waiting_at_ingress(manual_pipeline):
    p = manual_pipeline
    payload = synthetic_input(2000, frequency=510, amplitude=140)
    p.source.boundaries = {1: True}
    assert len(payload) == 100 * 160
    # Intentionally do not yield between original packets. The first source
    # start propagates an InterruptionFrame upstream while the remainder waits
    # in real transport/processor queues. No loss is allowed at that boundary.
    for offset in range(0, len(payload), 160):
        p.carrier.feed(
            {
                "event": "media",
                "media": {"payload": base64.b64encode(payload[offset : offset + 160]).decode()},
            }
        )
    await eventually(lambda: p.llm.media_progress["input_frames_received"] == 100)
    await eventually(lambda: p.llm.media_progress["input_frames_dispatched"] == 100)
    await eventually(lambda: len(p.source.audio) == 100 and p.llm.manual.speaking)
    assert p.llm.manual.audio_frames == 100
    assert p.carrier.clears == 1
    assert count_wire(p.model, "input_audio_buffer.commit") == 0
    assert count_wire(p.model, "response.create") == 0
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert_committed_input(p, [payload])
    assert p.llm.media_progress["input_frames_received"] == 100
    assert p.llm.media_progress["input_frames_dispatched"] == 100
    await fresh_reply(p)


@pytest.mark.parametrize("hook", ["before_push", "observer"])
async def test_media_continuation_revokes_audio_after_late_sdk_hooks(
    manual_pipeline, monkeypatch, hook
):
    p = manual_pipeline
    first, continuation = synthetic_input(300), synthetic_input(400, frequency=510)
    assert await p.source.start_turn()
    await feed(p, first)
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(frame):
        if isinstance(frame, TTSAudioRawFrame) and not entered.is_set():
            entered.set()
            await release.wait()

    if hook == "before_push":

        async def before_push(processor, frame):
            await pause(frame)

        p.llm.add_event_handler("on_before_push_frame", before_push)
    else:
        observer = p.llm._setup.observer or BaseObserver()
        original_push = observer.on_push_frame

        async def observer_push(data):
            if data.source is p.llm:
                await pause(data.frame)
            await original_push(data)

        monkeypatch.setattr(observer, "on_push_frame", observer_push)
        monkeypatch.setattr(p.llm._setup, "observer", observer)
    p.model.response("superseded", milliseconds=1200, frequency=400)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await p.source.start_turn()
        await eventually(lambda: p.carrier.clears == 2)
        await feed(p, continuation)
        assert p.carrier.audio_bytes == 0
    finally:
        release.set()
    await eventually(lambda: not p.llm.active_responses)
    await asyncio.sleep(0.12)
    assert p.carrier.audio_bytes == 0
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await fresh_reply(p)
    assert_committed_input(p, [first, continuation])


@pytest.mark.parametrize("boundary", ["pipeline_stop", "disconnect"])
async def test_media_close_joins_pending_commit_without_late_response(manual_pipeline, boundary):
    p = manual_pipeline
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    p.model.ack_commits = False
    assert await p.source.end_turn()
    await asyncio.wait_for(p.model.commit_entered.wait(), 2)
    creates = count_wire(p.model, "response.create")
    if boundary == "pipeline_stop":
        await p.worker.queue_frames([EndFrame()])
    else:
        await p.carrier.close()
    await asyncio.wait_for(asyncio.shield(p.runner), 5)
    assert p.llm.manual.closed
    p.model.acknowledge_commit()
    assert not await p.source.start_turn()
    await asyncio.sleep(0)
    assert count_wire(p.model, "response.create") == creates
    # The pinned output transport emits configured shutdown silence on a local
    # EndFrame. It must contain no stale/model speech and is never input audio.
    assert audioop.rms(audioop.ulaw2lin(p.carrier.audio_after(0), 2), 2) == 0


@pytest.mark.parametrize("failure_kind", ["raises", "wrong_type", "close_raises"])
async def test_invalid_external_source_ends_before_model_connection(
    service, settings, brief, monkeypatch, failure_kind
):
    settings.voice_backend = "pipecat"
    settings.pipecat_engine = "realtime"
    settings.pipecat_external_turns = True
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-model-fixture")
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("Invalid source must never dial")
    connected = MagicMock(side_effect=AssertionError("Invalid source must not connect model"))
    monkeypatch.setattr(realtime_sdk, "websocket_connect", connected)
    captured = []

    def invalid_factory(llm):
        captured.append(llm)
        if failure_kind == "raises":
            raise RuntimeError("synthetic source failure")
        return object()

    telephony = PipecatTelephony(
        settings, service, client, external_turn_source_factory=invalid_factory
    )
    call_id = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", call_id)
    call.update(state="active", provider_sid="CA" + "1" * 32)
    service.save(call)
    carrier = TimedCarrier(
        {
            "accountSid": settings.twilio_account.get_secret_value(),
            "callSid": call["provider_sid"],
            "streamSid": "MZ" + "3" * 32,
            "customParameters": {"dots_call_id": call_id},
        }
    )
    if failure_kind == "close_raises":

        async def failed_close(*args, **kwargs):
            raise OSError("synthetic carrier already disconnected")

        monkeypatch.setattr(carrier, "close", failed_close)
    await asyncio.wait_for(telephony.media(carrier), 3)
    assert len(captured) == 1 and captured[0].manual.closed
    assert not telephony.sessions
    connected.assert_not_called()
    client.calls.create.assert_not_called()
    assert service.provider.dials == []
    assert service.provider.stops == [call["provider_sid"]]
    assert carrier.audio_bytes == 0


def quiet_fixture(name):
    from tests.audio_assets import audio_asset

    directory = Path(__file__).parent / "fixtures" / "external-turns"
    manifest = json.loads((directory / "manifest.json").read_text())
    payload = audio_asset(Path("external-turns") / name).read_bytes()
    assert manifest["synthetic"] is True
    assert hashlib.sha256(payload).hexdigest() == manifest["clips"][name]["sha256"]
    assert len(payload) == manifest["clips"][name]["bytes"]
    return payload


async def test_quiet_phrase_continuation_preserves_bytes_and_invalidates_reply(manual_pipeline):
    p = manual_pipeline
    quiet = quiet_fixture("quiet-continuation.pcmu")
    # The earlier ASR alignment estimates a clause pause near1.28s. This is an
    # authored boundary inside the retained sentence, never a detector verdict.
    first, continuation = quiet[: 1280 * 8], quiet[1280 * 8 :]
    assert await p.source.start_turn()
    await feed(p, first)
    p.model.ack_commits = False
    assert await p.source.end_turn()
    await asyncio.wait_for(p.model.commit_entered.wait(), 2)
    assert await p.source.start_turn()
    await feed(p, continuation)
    p.model.acknowledge_commit()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    assert count_wire(p.model, "response.create") == 0
    assert p.carrier.audio_bytes == 0
    p.model.ack_commits = True
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await fresh_reply(p)
    assert_committed_input(p, [first, continuation])


async def test_paced_quiet_interruption_clears_completed_generation_and_recovers(manual_pipeline):
    p = manual_pipeline
    primer = synthetic_input(300)
    assert await p.source.start_turn()
    await feed(p, primer)
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    p.model.response("playing_before_quiet_input", milliseconds=3000, frequency=400)
    await eventually(lambda: p.carrier.audio_bytes >= 640 and not p.llm.active_responses)
    # Generation is finished, but its waveform remains queued for playout.
    quiet = quiet_fixture("quiet-interruption.pcmu")
    await feed(p, quiet[:160])
    assert await p.source.start_turn()
    cleared = p.carrier.clear_events[-1]
    assert 640 <= cleared.audio_bytes < 24000
    await feed(p, quiet[160:], pace=0.02)
    await asyncio.sleep(0.08)
    assert p.carrier.audio_bytes == cleared.audio_bytes
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await fresh_reply(p)
    assert_committed_input(p, [primer, quiet])


async def test_open_external_turn_has_no_endpoint_or_eof_recovery(manual_pipeline):
    p = manual_pipeline
    assert await p.source.start_turn()
    await feed(p, synthetic_input(400, amplitude=140))
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await asyncio.sleep(0.12)
    assert count_wire(p.model, "input_audio_buffer.commit") == 0
    assert count_wire(p.model, "response.create") == p.initial_creates
    await p.carrier.close()
    await asyncio.wait_for(asyncio.shield(p.runner), 5)
    assert count_wire(p.model, "input_audio_buffer.commit") == 0
    assert count_wire(p.model, "response.create") == p.initial_creates


async def test_model_session_reset_revokes_old_source_epoch_and_closes(manual_pipeline):
    p = manual_pipeline
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    p.model.ack_commits = False
    assert await p.source.end_turn()
    await asyncio.wait_for(p.model.commit_entered.wait(), 2)
    old_epoch = p.source.epoch
    creates = count_wire(p.model, "response.create")
    await p.llm.reset_interruption_session()
    assert p.llm.turn_epoch != old_epoch
    assert p.llm.manual.closed
    p.model.acknowledge_commit()
    assert not await p.source.start_turn()
    await eventually(lambda: p.llm.stop_requested)
    assert count_wire(p.model, "response.create") == creates
    assert p.carrier.audio_bytes == 0


async def test_model_eof_revokes_pending_commit_without_reconnect(manual_pipeline):
    p = manual_pipeline
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    p.model.ack_commits = False
    assert await p.source.end_turn()
    await asyncio.wait_for(p.model.commit_entered.wait(), 2)
    creates = count_wire(p.model, "response.create")
    await p.model.close()
    await eventually(lambda: p.llm.stop_requested and p.llm.manual.closed)
    await eventually(lambda: bool(p.service.provider.stops))
    assert not await p.source.start_turn()
    assert p.llm.turn_epoch != p.source.epoch
    assert p.llm.reconnect_attempts == 0
    assert count_wire(p.model, "response.create") == creates
    assert p.carrier.audio_bytes == 0


async def test_manual_provider_send_failure_ends_call_without_reconnect(
    manual_pipeline, monkeypatch
):
    p = manual_pipeline
    original_send = p.model.send

    async def failing_commit(message):
        if json.loads(message)["type"] == "input_audio_buffer.commit":
            raise OSError("fixture-private-text must not escape sanitized failure metadata")
        await original_send(message)

    monkeypatch.setattr(p.model, "send", failing_commit)
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    assert await p.source.end_turn()
    await eventually(lambda: p.llm.stop_requested)
    await eventually(lambda: bool(p.service.provider.stops))
    assert p.llm.manual.failure is not None
    assert p.llm.reconnect_attempts == 0
    assert count_wire(p.model, "response.create") == p.initial_creates
    failure = p.service.call("owner", p.call_id)["usage"]["realtime_path_failure"]
    assert "fixture-private-text" not in str(failure)
    assert p.carrier.audio_bytes == 0


async def test_priority_clear_revokes_audio_already_admitted_to_output_input_queue(
    manual_pipeline,
):
    p = manual_pipeline
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    output = p.llm.output
    await output.pause_processing_system_frames()
    continuation = None
    try:
        p.model.response("queued_before_clear", milliseconds=1200, frequency=400)
        await eventually(lambda: not p.llm.active_responses and bool(p.llm.admitted_pcm))
        assert sum(p.llm.admitted_pcm.values()) == 1200 * 48
        assert p.carrier.audio_bytes == 0
        # Default-priority audio is already admitted. A new priority clear can
        # overtake it, so admission-time ownership alone is insufficient.
        continuation = asyncio.create_task(p.source.start_turn())
        await eventually(lambda: p.llm.manual.speaking)
    finally:
        await output.resume_processing_system_frames()
    assert continuation is not None and await asyncio.wait_for(continuation, 2)
    await feed(p, synthetic_input(300, frequency=510))
    await asyncio.sleep(0.12)
    assert p.carrier.audio_bytes == 0
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await fresh_reply(p)


@pytest.mark.parametrize("stale_started", [True, False])
async def test_old_same_session_decision_cannot_mutate_new_turn(manual_pipeline, stale_started):
    p = manual_pipeline
    assert await p.source.start_turn()
    old_turn = p.llm.manual.turn_id
    await feed(p, synthetic_input(300))
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    p.model.response("first_completed", milliseconds=0)
    await eventually(
        lambda: "first_completed" in p.llm.responses_created and not p.llm.active_responses
    )
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300, frequency=510))
    current_turn = p.llm.manual.turn_id
    assert current_turn > old_turn
    if stale_started:
        assert await p.source.end_turn()
        await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    before = count_wire(p.model, "input_audio_buffer.commit")
    assert not await p.llm.propose_turn(
        started=stale_started, epoch=p.source.epoch, turn_id=old_turn
    )
    assert p.llm.manual.turn_id == current_turn
    assert p.llm.manual.speaking is (not stale_started)
    assert count_wire(p.model, "input_audio_buffer.commit") == before
    if not stale_started:
        assert await p.source.end_turn()
        await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    await fresh_reply(p)


@pytest.mark.parametrize("accepted_detection", ["native", "missing"])
async def test_provider_must_explicitly_accept_null_native_detection_before_input(
    service, settings, brief, monkeypatch, accepted_detection
):
    class WrongSessionRealtime(CommitRealtime):
        def emit(self, event):
            if event["type"] == "session.updated":
                event = json.loads(json.dumps(event))
                audio_input = event["session"]["audio"]["input"]
                if accepted_detection == "native":
                    audio_input["turn_detection"] = {"type": "server_vad"}
                else:
                    audio_input.pop("turn_detection", None)
            super().emit(event)

    model = WrongSessionRealtime()

    async def memory_connect(**kwargs):
        return model

    monkeypatch.setattr(realtime_sdk, "websocket_connect", memory_connect)
    settings.voice_backend = "pipecat"
    settings.pipecat_engine = "realtime"
    settings.pipecat_external_turns = True
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-model-fixture")
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("Rejected session must never dial")
    sources = []

    def factory(llm):
        source = ScriptedSource(llm)
        sources.append(source)
        return source

    telephony = PipecatTelephony(settings, service, client, external_turn_source_factory=factory)
    call_id = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", call_id)
    call.update(state="active", provider_sid="CA" + "1" * 32)
    service.save(call)
    carrier = TimedCarrier(
        {
            "accountSid": settings.twilio_account.get_secret_value(),
            "callSid": call["provider_sid"],
            "streamSid": "MZ" + "3" * 32,
            "customParameters": {"dots_call_id": call_id},
        }
    )
    runner = asyncio.create_task(telephony.media(carrier))
    try:
        await eventually(lambda: sources and sources[0].llm.stop_requested)
        llm = sources[0].llm
        assert not llm.ready.is_set()
        assert llm.manual.closed
        carrier.feed(
            {
                "event": "media",
                "media": {
                    "payload": base64.b64encode(synthetic_input(20)).decode(),
                },
            }
        )
        await asyncio.sleep(0.05)
        assert count_wire(model, "input_audio_buffer.append") == 0
        assert count_wire(model, "response.create") == 0
        assert llm.reconnect_attempts == 0
        failure = service.call("owner", call_id)["usage"]["realtime_path_failure"]
        assert failure["phase"] == "external_native_detection_enabled"
    finally:
        await carrier.close()
        await asyncio.wait_for(asyncio.shield(runner), 5)
        client.calls.create.assert_not_called()
        assert service.provider.dials == []
