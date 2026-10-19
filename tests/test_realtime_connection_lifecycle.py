"""No-dial integration coverage for observed carrier lifecycle metadata."""

import asyncio
import socket

import pytest
from starlette.websockets import WebSocketState

pytest.importorskip("pipecat")

from pipecat.frames.frames import EndFrame

from tests.test_realtime_interruption_ordering import assert_waveform
from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_playout import eventually


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Offline lifecycle regression attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    yield
    assert not attempted


def wire_count(model, kind):
    return sum(event["type"] == kind for event in model.sent)


@pytest.mark.parametrize("close_code", [1000, 1006])
async def test_remote_carrier_hangup_is_observed_before_model_cleanup(
    audio_pipeline, service, monkeypatch, close_code
):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    model.response("before_carrier_end", milliseconds=200, frequency=700)
    await eventually(lambda: carrier.audio_bytes == 1600)
    await eventually(lambda: llm._current_audio_response is None)
    if close_code == 1006:
        model.speech(started=True, turn=1)
        await eventually(lambda: carrier.clears == 1)
    # A real Starlette receive marks the client disconnected. Do that at the
    # receive boundary, rather than using MemoryCarrier.close(), which models
    # a local close request as well as injecting a disconnect into its queue.
    original_receive = carrier.receive

    async def receive():
        message = await original_receive()
        if message["type"] == "websocket.disconnect":
            carrier.client_state = WebSocketState.DISCONNECTED
        return message

    monkeypatch.setattr(carrier, "receive", receive)
    initial_creates = wire_count(model, "response.create")
    initial_appends = wire_count(model, "input_audio_buffer.append")
    carrier.feed({"event": "stop", "stop": {"reason": "fictional private closure words"}})
    carrier.incoming.put_nowait({"type": "websocket.disconnect", "code": close_code})
    await asyncio.wait_for(llm.closed.wait(), 4)
    await eventually(
        lambda: service.call("owner", pipeline.call_id)["usage"].get("voice_closed_confirmed")
    )

    saved = service.call("owner", pipeline.call_id)["usage"]["realtime_interruption_diagnostics"]
    lifecycle = saved["connection_lifecycle"]
    events = lifecycle["events"]
    kinds = [event["kind"] for event in events]
    assert kinds[:2] == ["carrier_stop", "carrier_disconnect"]
    assert events[1]["code"] == close_code
    assert events[0]["side"] == events[1]["side"] == "carrier"
    assert kinds.index("carrier_disconnect") < kinds.index("model_close_requested")
    assert "model_closed" in kinds
    assert all(
        left["at_ms"] <= right["at_ms"] for left, right in zip(events, events[1:], strict=False)
    )
    assert "fictional private closure words" not in str(saved)
    # Teardown observes EOF; it neither fabricates input silence/a native turn
    # end nor asks for a fresh answer after the destination has disconnected.
    assert wire_count(model, "input_audio_buffer.append") == initial_appends
    assert wire_count(model, "input_audio_buffer.commit") == 0
    assert wire_count(model, "response.create") == initial_creates
    assert not any(
        event["type"] == "input_audio_buffer.speech_stopped" for event in saved["events"]
    )
    if close_code == 1006:
        assert (
            saved["native_turn_state"]["last_event"]["type"] == "input_audio_buffer.speech_started"
        )
        assert "last_stopped" not in saved["native_turn_state"]


async def test_native_turn_recovery_keeps_carrier_open_without_lifecycle_events(
    audio_pipeline, service
):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    initial_creates = wire_count(model, "response.create")
    model.response("interrupted_reply", milliseconds=3000, frequency=400)
    await eventually(lambda: carrier.audio_bytes >= 640)
    model.speech(started=True, turn=1)
    await eventually(lambda: carrier.clears == 1)
    interrupted_at = carrier.clear_events[0]
    received_before = llm.media_progress["input_frames_dispatched"]
    for _ in range(5):
        pipeline.feed_input()
    await eventually(lambda: llm.media_progress["input_frames_dispatched"] == received_before + 5)
    # These are explicit server events, not a claim that synthetic EOF or our
    # fixture waveform can reproduce the provider's native speech detector.
    model.speech(started=False, turn=1)
    model.response("fresh_after_turn", milliseconds=1200, frequency=900)
    await eventually(lambda: carrier.audio_bytes >= interrupted_at.audio_bytes + 9600)
    await eventually(lambda: llm._current_audio_response is None)
    assert_waveform(
        carrier.audio_after(interrupted_at.event_index),
        milliseconds=1200,
        frequency=900,
        absent_frequency=400,
    )
    assert carrier.clears == 1
    assert carrier.client_state == WebSocketState.CONNECTED
    assert not llm.closed.is_set()
    assert service.call("owner", pipeline.call_id)["state"] == "active"
    assert not service.provider.stops
    report = llm.interruption_diagnostics.report()
    assert report["connection_lifecycle"]["events"] == []
    assert report["connection_lifecycle"]["events_dropped"] == 0
    saved = service.call("owner", pipeline.call_id)["usage"]["realtime_interruption_diagnostics"]
    assert saved["connection_lifecycle"]["events"] == []
    assert wire_count(model, "input_audio_buffer.commit") == 0
    assert wire_count(model, "response.create") == initial_creates


async def test_application_end_persists_later_carrier_close_request(audio_pipeline, service):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    model.response("before_application_end", milliseconds=200, frequency=700)
    await eventually(lambda: carrier.audio_bytes == 1600)
    await eventually(lambda: llm._current_audio_response is None)
    initial_creates = wire_count(model, "response.create")

    await pipeline.worker.queue_frames([EndFrame()])
    await asyncio.wait_for(asyncio.shield(pipeline.runner_task), 4)
    assert llm.closed.is_set()
    saved = service.call("owner", pipeline.call_id)["usage"]["realtime_interruption_diagnostics"]
    lifecycle = saved["connection_lifecycle"]
    kinds = [event["kind"] for event in lifecycle["events"]]
    assert kinds[0] == "pipeline_stop"
    # The output transport closes the carrier after the model has finished
    # shutdown. Final persistence must include that later local observation.
    assert kinds.index("model_closed") < kinds.index("carrier_close_requested")
    assert "carrier_stop" not in kinds
    assert "carrier_disconnect" not in kinds
    assert lifecycle["first_observed"] == lifecycle["events"][0]
    assert lifecycle["last_observed"] == lifecycle["events"][-1]
    assert saved == llm.interruption_diagnostics.report()
    assert wire_count(model, "input_audio_buffer.commit") == 0
    assert wire_count(model, "response.create") == initial_creates


async def test_model_eof_persists_failure_before_carrier_cleanup(audio_pipeline, service):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    model.response("before_model_end", milliseconds=200, frequency=700)
    await eventually(lambda: carrier.audio_bytes == 1600)
    await eventually(lambda: llm._current_audio_response is None)
    initial_creates = wire_count(model, "response.create")
    initial_appends = wire_count(model, "input_audio_buffer.append")

    # End the fake provider's iterator while its carrier is still connected.
    # Use the real voice_failure/end policy; only carrier API calls are faked.
    await model.close()
    await eventually(lambda: llm.stop_task is not None)
    await asyncio.wait_for(asyncio.shield(llm.stop_task), 3)
    call = service.call("owner", pipeline.call_id)
    assert call["state"] == "failed"
    assert call["stop_reason"] == "voice_path_failed"
    assert call["usage"]["realtime_path_failure"]["phase"] == "receive_ended"
    lifecycle = call["usage"]["realtime_interruption_diagnostics"]["connection_lifecycle"]
    assert [event["kind"] for event in lifecycle["events"]] == ["model_receive_ended"]
    assert lifecycle["first_observed"]["side"] == "model"
    assert lifecycle["first_observed"]["code"] == 1000
    assert lifecycle["first_observed"]["input_state"]["stop_requested"] is False
    assert carrier.client_state == WebSocketState.CONNECTED
    assert service.provider.stops == [call["provider_sid"]]
    assert service.provider.dials == []
    assert llm.reconnect_task is None
    assert wire_count(model, "input_audio_buffer.append") == initial_appends
    assert wire_count(model, "input_audio_buffer.commit") == 0
    assert wire_count(model, "response.create") == initial_creates
