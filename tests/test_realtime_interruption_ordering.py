"""No-dial, actual-pipeline regressions for server-VAD interruption ordering."""

import asyncio
import audioop
import base64
import json
import math
import socket
import time
from array import array
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import llm as realtime_sdk

from dots_phone.pipecat_voice import PipecatTelephony
from tests.test_realtime_playout import MemoryCarrier, MemoryRealtime, eventually


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Offline audio regression attempted a connection or DNS lookup")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    yield
    assert not attempts


class HarmonicRealtime(MemoryRealtime):
    """Stream distinct non-silent waveforms through the real codec/resampler."""

    def __init__(self):
        self.frequencies = {}
        self.sample_offsets = {}
        super().__init__()

    def emit(self, event):
        if event["type"] == "response.output_audio.delta":
            name = event["response_id"]
            frequency = self.frequencies[name]
            count = len(base64.b64decode(event["delta"])) // 2
            offset = self.sample_offsets.get(name, 0)
            samples = array(
                "h",
                (
                    int(
                        9000
                        * sum(
                            weight * math.sin(2 * math.pi * frequency * harmonic * i / 24000)
                            for harmonic, weight in ((1, 0.7), (2, 0.2), (3, 0.1))
                        )
                    )
                    for i in range(offset, offset + count)
                ),
            )
            event = {**event, "delta": base64.b64encode(samples.tobytes()).decode()}
            self.sample_offsets[name] = offset + count
        super().emit(event)

    def response(self, name, *, milliseconds, frequency=700):
        self.frequencies[name] = frequency
        super().response(name, milliseconds=milliseconds)


class TimedCarrier(MemoryCarrier):
    def __init__(self, start):
        super().__init__(start)
        self.clear_events = []

    async def send_text(self, message):
        await super().send_text(message)
        if json.loads(message)["event"] == "clear":
            self.clear_events.append(
                SimpleNamespace(
                    at=time.monotonic(),
                    epoch_ms=int(time.time() * 1000),
                    event_index=len(self.sent),
                    audio_bytes=self.audio_bytes,
                )
            )

    def audio_after(self, event_index):
        return b"".join(
            base64.b64decode(event["media"]["payload"])
            for event in self.sent[event_index:]
            if event["event"] == "media"
        )


def assert_waveform(payload, *, milliseconds, frequency, absent_frequency):
    # Exact carrier-duration accounting catches erased chunks, while the two
    # frequency checks reject stale speech, digital silence, and padded filler.
    assert len(payload) == milliseconds * 8
    pcm = audioop.ulaw2lin(payload, 2)
    assert audioop.rms(pcm, 2) > 2000
    samples = array("h", pcm)

    def power(tone):
        real = sum(
            sample * math.cos(2 * math.pi * tone * i / 8000) for i, sample in enumerate(samples)
        )
        imaginary = sum(
            sample * math.sin(2 * math.pi * tone * i / 8000) for i, sample in enumerate(samples)
        )
        return real * real + imaginary * imaginary

    assert power(frequency) > 1000 * max(power(absent_frequency), 1)


@pytest.fixture
async def audio_pipeline(service, settings, brief, monkeypatch):
    model = HarmonicRealtime()
    connect = MagicMock()

    async def memory_connect(**kwargs):
        connect(**kwargs)
        return model

    monkeypatch.setattr(realtime_sdk, "websocket_connect", memory_connect)
    settings.pipecat_engine = "realtime"
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-model-fixture")
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("This regression must never dial")
    telephony = PipecatTelephony(settings, service, client)
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
    worker = asyncio.create_task(telephony.media(carrier))

    def feed_input():
        carrier.feed(
            {"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}}
        )

    try:
        await eventually(lambda: call["provider_sid"] in telephony.sessions)
        llm = telephony.sessions[call["provider_sid"]].llm
        await asyncio.wait_for(llm.ready.wait(), 3)
        feed_input()
        await eventually(lambda: llm.media_progress["input_frames_dispatched"] == 1)
        yield SimpleNamespace(
            model=model,
            carrier=carrier,
            llm=llm,
            feed_input=feed_input,
            call_id=call_id,
            worker=telephony.sessions[call["provider_sid"]].worker,
            runner_task=worker,
        )
    finally:
        await carrier.close()
        try:
            await asyncio.wait_for(worker, 4)
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            pytest.fail("Offline pipeline failed to shut down")
        client.calls.create.assert_not_called()
        assert service.provider.dials == []
        assert connect.call_count == 1


async def test_new_reply_survives_delayed_native_interruption(audio_pipeline, monkeypatch):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    native_started = asyncio.Event()
    original_interrupt = llm._handle_interruption

    async def delayed_interrupt():
        native_started.set()
        await asyncio.sleep(0.15)
        await original_interrupt()

    monkeypatch.setattr(llm, "_handle_interruption", delayed_interrupt)
    # This first turn deliberately has no current assistant response. Native
    # VAD must still produce exactly one clear, and the receiver must await it.
    assert llm._current_audio_response is None
    model.speech(started=True, turn=1)
    await asyncio.wait_for(native_started.wait(), 2)
    model.speech(started=True, turn=1)  # Duplicate must not wait for another clear.
    model.speech(started=False, turn=1)
    model.response("fresh_after_native_delay", milliseconds=1200, frequency=700)
    await eventually(lambda: carrier.audio_bytes >= 9600)
    await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 1
    assert carrier.clear_events[0].audio_bytes == 0
    assert_waveform(carrier.audio_after(0), milliseconds=1200, frequency=700, absent_frequency=400)

    # A genuine subsequent interruption must still clear queued old speech and
    # allow a complete, different reply. This is not an uninterruptible stream.
    model.response("old_second_turn", milliseconds=3000, frequency=400)
    await eventually(lambda: carrier.audio_bytes >= 9600 + 640)
    model.speech(started=True, turn=2)
    await eventually(lambda: carrier.clears == 2)
    second_clear = carrier.clear_events[-1]
    assert second_clear.audio_bytes - 9600 < 24000
    model.speech(started=False, turn=2)
    model.response("recovery", milliseconds=1200, frequency=900)
    await eventually(lambda: carrier.audio_bytes >= second_clear.audio_bytes + 9600)
    await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 2
    assert_waveform(
        carrier.audio_after(second_clear.event_index),
        milliseconds=1200,
        frequency=900,
        absent_frequency=400,
    )


async def test_clear_precedes_slow_truncate_and_input_continues(audio_pipeline, monkeypatch):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    model.response("old_answer", milliseconds=3000, frequency=400)
    await eventually(lambda: carrier.audio_bytes >= 640)
    assert llm._current_audio_response is not None
    old_start_ms = llm._current_audio_response.start_time_ms
    truncate_entered = asyncio.Event()
    truncate_finished = asyncio.Event()
    truncations = []
    original_send = model.send

    async def delayed_truncate(message):
        event = json.loads(message)
        if event["type"] == "conversation.item.truncate":
            # Strong ordering assertion, independent of scheduler/wall timing:
            # carrier clear must already have happened BEFORE provider I/O waits.
            assert carrier.clears == 1
            truncations.append(event)
            truncate_entered.set()
            await asyncio.sleep(0.15)
            truncate_finished.set()
        await original_send(message)

    monkeypatch.setattr(model, "send", delayed_truncate)
    model.speech(started=True, turn=1)
    model.speech(started=True, turn=1)
    model.speech(started=False, turn=1)
    # New wire events are already waiting while source processing is blocked.
    model.response("fresh_answer", milliseconds=1200, frequency=700)
    await asyncio.wait_for(truncate_entered.wait(), 2)
    cleared = carrier.clear_events[0]
    input_before = llm.media_progress["input_frames_dispatched"]
    for _ in range(4):
        pipeline.feed_input()
    await eventually(lambda: llm.media_progress["input_frames_dispatched"] >= input_before + 4)
    assert not truncate_finished.is_set()
    assert carrier.audio_bytes == cleared.audio_bytes

    await eventually(lambda: carrier.audio_bytes >= cleared.audio_bytes + 9600)
    await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 1
    assert len(truncations) == 1
    assert truncations[0]["item_id"] == "old_answer_item"
    # A delayed send must not inflate the endpoint by its 150ms wait. The event
    # freezes the endpoint before awaiting provider I/O, with a small clock margin.
    expected_endpoint = max(0, min(cleared.epoch_ms - old_start_ms, 3000))
    assert abs(truncations[0]["audio_end_ms"] - expected_endpoint) <= 10
    assert_waveform(
        carrier.audio_after(cleared.event_index),
        milliseconds=1200,
        frequency=700,
        absent_frequency=400,
    )


async def test_session_reset_closes_old_turn_before_fresh_native_start(audio_pipeline):
    model, carrier, llm = audio_pipeline.model, audio_pipeline.carrier, audio_pipeline.llm
    model.speech(started=True, turn=1)
    await eventually(lambda: carrier.clears == 1)
    await eventually(lambda: llm.interruption_barrier.pending is None)
    assert llm.interruption_barrier.remote_turn_open

    # Exercise the actual recovery hook, without connecting/replacing sockets.
    # Resetting only the helper would leave the aggregator's old turn open and
    # its first new-session start would never produce a native interruption.
    await llm.reset_interruption_session()
    assert not llm.interruption_barrier.remote_turn_open
    model.speech(started=True, turn=2)
    model.speech(started=False, turn=2)
    model.response("fresh_session", milliseconds=1200, frequency=700)
    await eventually(lambda: carrier.audio_bytes >= 9600)
    await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 2
    assert_waveform(carrier.audio_after(0), milliseconds=1200, frequency=700, absent_frequency=400)


async def test_back_to_back_ivr_segments_do_not_lose_native_interruption(audio_pipeline):
    """Scripted back-to-back VAD events must preserve interruption ordering."""
    model, carrier, llm = audio_pipeline.model, audio_pipeline.carrier, audio_pipeline.llm
    for turn in range(1, 7):
        model.speech(started=True, turn=turn)
        model.speech(started=False, turn=turn)
    model.response("after_segmented_ivr", milliseconds=1200, frequency=700)
    await eventually(lambda: carrier.audio_bytes >= 9600, timeout=5)
    assert not llm.stop_requested
    assert not llm.interruption_barrier.failed
    assert carrier.clears == 6
    assert_waveform(carrier.audio_after(0), milliseconds=1200, frequency=700, absent_frequency=400)
