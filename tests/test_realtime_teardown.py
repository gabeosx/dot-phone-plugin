"""No-network service teardown tests; observations do not alter audio or close."""

import asyncio
import json
import socket
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import EndFrame, InputAudioRawFrame

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from tests.test_realtime_playout import eventually


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Offline teardown test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    yield
    assert not attempts


class LocalSocket:
    def __init__(self):
        self.close_code = None
        self.incoming = asyncio.Queue()
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()
        self.sent = []

    async def __aiter__(self):
        while (message := await self.incoming.get()) is not None:
            yield message

    async def send(self, message):
        self.send_entered.set()
        await self.release_send.wait()
        self.sent.append(json.loads(message))

    async def close(self):
        self.close_code = 1000
        self.incoming.put_nowait(None)


def make_service(service, brief):
    call_id = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", call_id)
    call["state"] = "active"
    service.save(call)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm.session_ids.add("offline-session")
    return llm


async def cancel_task(task, **kwargs):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("end", ["normal_close", "fatal_api"])
async def test_receive_end_is_observed_before_failure_without_claiming_socket_close(
    service, brief, monkeypatch, end
):
    llm = make_service(service, brief)
    raw = LocalSocket()
    llm._websocket = raw
    llm.voice_failure = Mock()
    llm._handle_evt_error = AsyncMock()
    if end == "normal_close":
        await raw.close()
    else:
        raw.incoming.put_nowait(
            json.dumps(
                {
                    "event_id": "offline_error",
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_value",
                        "message": "fictional private failure words",
                    },
                }
            )
        )
    await llm._receive_task_handler()
    lifecycle = llm.interruption_diagnostics.report()["connection_lifecycle"]
    assert [event["kind"] for event in lifecycle["events"]] == ["model_receive_ended"]
    first = lifecycle["first_observed"]
    assert first["input_state"]["receive_outcome"] == "returned"
    assert first["input_state"]["stop_requested"] is False
    assert first.get("code") == (1000 if end == "normal_close" else None)
    assert "fictional private failure words" not in json.dumps(lifecycle)
    llm.voice_failure.assert_called_once_with("receive_ended")
    await llm._disconnect()
    saved = service.call("owner", llm.call_id)["usage"]["realtime_interruption_diagnostics"]
    assert [event["kind"] for event in saved["connection_lifecycle"]["events"]] == [
        "model_receive_ended",
        "model_close_requested",
        "model_closed",
    ]
    assert llm.closed.is_set()
    assert raw.sent == []
    count = len(llm.interruption_diagnostics.report()["connection_lifecycle"]["events"])
    await llm._disconnect()  # SDK cleanup may call disconnect repeatedly.
    assert len(llm.interruption_diagnostics.report()["connection_lifecycle"]["events"]) == count


async def test_shutdown_preserves_queued_inflight_and_resampler_counts_without_flushing(
    service, brief, monkeypatch
):
    llm = make_service(service, brief)
    raw = LocalSocket()
    llm._websocket = raw
    llm.ready.set()
    monkeypatch.setattr(llm, "create_task", lambda coro, name: asyncio.create_task(coro))
    monkeypatch.setattr(llm, "cancel_task", cancel_task)
    flush = AsyncMock(side_effect=AssertionError("Teardown must not flush or reset input"))
    monkeypatch.setattr(llm.input_resampler, "flush", flush)
    monkeypatch.setattr(llm.input_resampler, "reset", flush)
    for _ in range(20):
        await llm._send_user_audio(InputAudioRawFrame(b"\x00\x10" * 160, 8000, 1))
    await asyncio.wait_for(raw.send_entered.wait(), 1)
    queued = llm.input_queue.queue.qsize()
    dispatched = llm.media_progress["input_frames_dispatched"]
    tail = llm.input_resampler._soxr_stream.delay()
    assert queued > 0 and tail > 0
    assert dispatched + queued + 1 == 20
    await llm.stop(EndFrame())
    saved = service.call("owner", llm.call_id)["usage"]["realtime_interruption_diagnostics"]
    events = saved["connection_lifecycle"]["events"]
    assert [event["kind"] for event in events] == [
        "pipeline_stop",
        "model_close_requested",
        "model_closed",
    ]
    before, after = events[0]["input_state"], events[-1]["input_state"]
    assert before["queued_frames"] == after["queued_frames"] == queued
    assert before["queued_ms"] == after["queued_ms"] == queued * 20
    assert before["inflight_frame_ms"] == after["inflight_frame_ms"] == 20
    assert before["inflight_resampled_bytes"] > 0
    assert before["dispatch_phase"] == after["dispatch_phase"] == "sending"
    assert (
        before["resampler_pending_output_samples"]
        == after["resampler_pending_output_samples"]
        == tail
    )
    assert before["resampler_pending_output_ms"] == pytest.approx(tail / 24)
    assert before["input_frames_received"] == 20
    assert before["input_frames_dispatched"] == after["input_frames_dispatched"] == dispatched
    assert before["model_audio_packets_sent"] == after["model_audio_packets_sent"] == 0
    assert before["input_dispatch_cancelled"] is False
    assert after["input_dispatch_cancelled"] is True
    assert saved["native_turn_state"] == {}
    assert raw.sent == []
    flush.assert_not_awaited()
    assert service.provider.dials == service.provider.stops == []


async def test_local_close_request_precedes_receive_cancel_and_is_not_remote_hangup(
    service, brief, monkeypatch
):
    llm = make_service(service, brief)
    raw = LocalSocket()
    llm._websocket = raw
    monkeypatch.setattr(llm, "cancel_task", cancel_task)
    llm._receive_task = asyncio.create_task(llm._receive_task_handler())
    await eventually(lambda: llm._websocket is not raw)
    llm.stop_requested = True
    await llm._disconnect()
    events = llm.interruption_diagnostics.report()["connection_lifecycle"]["events"]
    assert events[0]["kind"] == "model_close_requested"
    assert events[0]["input_state"]["stop_requested"] is True
    receive = next(event for event in events if event["kind"] == "model_receive_ended")
    assert receive["input_state"]["receive_outcome"] in {"returned", "cancelled"}
    assert receive["input_state"]["disconnecting"] is True
    assert events[-1]["kind"] == "model_closed"
    assert events[-1]["code"] == 1000
    assert not llm.stop_task and not service.provider.stops


async def test_connection_diagnostics_failures_cannot_block_teardown(service, brief, monkeypatch):
    llm = make_service(service, brief)
    llm._websocket = LocalSocket()
    monkeypatch.setattr(llm, "_observe_connection_event", Mock(side_effect=RuntimeError("private")))
    await llm._disconnect()
    assert llm.closed.is_set()
    assert llm.interruption_diagnostics.report()["connection_lifecycle"]["events"] == []


async def test_websocket_enum_close_code_confirms_failed_voice_closure(service, brief):
    from websockets.frames import CloseCode

    class AbnormalSocket(LocalSocket):
        async def close(self):
            self.close_code = CloseCode.ABNORMAL_CLOSURE
            self.incoming.put_nowait(None)

    llm = make_service(service, brief)
    llm._websocket = AbnormalSocket()
    await llm._disconnect()
    usage = service.call("owner", llm.call_id)["usage"]
    assert usage["voice_closed_confirmed"] is True
    assert type(usage["realtime_close_code"]) is int
    assert usage["realtime_close_code"] == 1006
