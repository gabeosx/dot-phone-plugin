"""Offline wire/pipeline coverage for content-free native interruption metadata."""

import asyncio
import copy
import hashlib
import json
import socket

import pytest

from dots_phone.pipecat_diagnostics import (
    LIMIT,
    NativeInterruptionDiagnostics,
    accepted_input_configuration,
)

pytest.importorskip("pipecat")

from pipecat.frames.frames import InterruptionFrame

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from tests.test_realtime_interruption_ordering import (
    assert_waveform,
)
from tests.test_realtime_interruption_ordering import (
    audio_pipeline as audio_pipeline,
)
from tests.test_realtime_playout import eventually


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Offline diagnostics test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    yield
    assert not attempted


def test_accepted_detector_values_are_explicit_bounded_and_content_free():
    private = "fictional private diagnostic words"
    result = accepted_input_configuration(
        {
            "audio": {
                "input": {
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.6,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 300,
                        "interrupt_response": True,
                        "create_response": True,
                        "message": private,
                    },
                    "noise_reduction": {"type": "near_field", "message": private},
                    "transcription": {"prompt": private},
                }
            }
        }
    )
    assert result == {
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.6,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 300,
            "interrupt_response": True,
            "create_response": True,
        },
        "noise_reduction": {"type": "near_field"},
    }
    assert private not in json.dumps(result)
    assert accepted_input_configuration({}) == {}  # Missing is not an inferred default.
    assert accepted_input_configuration({"turn_detection": None}) == {"turn_detection": None}
    assert accepted_input_configuration({"turn_detection": False}) == {"turn_detection": False}
    semantic = accepted_input_configuration(
        {"turn_detection": {"type": "semantic_vad", "eagerness": "high"}}
    )
    assert semantic["turn_detection"] == {"type": "semantic_vad", "eagerness": "high"}
    rejected = accepted_input_configuration(
        {
            "turn_detection": {
                "type": private,
                "threshold": float("nan"),
                "prefix_padding_ms": -1,
                "silence_duration_ms": True,
                "eagerness": private,
                "interrupt_response": private,
                "create_response": {"message": private},
            },
            "input_audio_noise_reduction": {"type": private},
        }
    )
    assert private not in json.dumps(rejected, allow_nan=False)
    assert rejected["turn_detection"]["threshold"] is None
    assert rejected["turn_detection"]["interrupt_response"] == "unknown"
    assert accepted_input_configuration({"turn_detection": {"threshold": 10**400}}) == {
        "turn_detection": {"threshold": None}
    }


def test_timeline_bounds_redacts_unknowns_and_separates_sessions():
    diagnostics = NativeInterruptionDiagnostics(1234000)
    private = "fictional private diagnostic words"
    for index in range(LIMIT + 4):
        identity = f"response_{index}"
        diagnostics.observe(
            {
                "type": "response.created",
                "response": {"id": identity, "status": "in_progress"},
            },
            at_ms=index * 10,
            session_count=1,
        )
        diagnostics.audio(identity, 4800, at_ms=index * 10 + 1, session_count=1)
        diagnostics.audio(identity, 4800, at_ms=index * 10 + 2, session_count=1, forwarded=True)
        diagnostics.observe(
            {
                "type": "response.done",
                "response": {
                    "id": identity,
                    "status": "cancelled",
                    "status_details": {
                        "type": "cancelled",
                        "reason": private,
                        "error": {"message": private, "code": private},
                    },
                    "output": [{"transcript": private}],
                },
            },
            at_ms=index * 10 + 3,
            session_count=1,
        )
    diagnostics.observe(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": private,
            "audio_start_ms": 2000,
            "transcript": private,
        },
        at_ms=1400,
        session_count=1,
    )
    diagnostics.observe(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "recipient_1",
            "audio_end_ms": float("inf"),
        },
        at_ms=1401,
        session_count=1,
    )
    before = diagnostics.report()
    assert len(before["events"]) == len(before["responses"]) == LIMIT
    assert before["events_dropped"] == 138
    assert before["response_records_dropped"] == 4
    assert before["responses"][-1]["model_audio_bytes"] == 4800
    assert before["responses"][-1]["reason"] == "unknown"
    assert "item_id" not in before["events"][-2]
    assert before["events"][-1]["audio_end_ms"] is None
    serialized = json.dumps(before, allow_nan=False)
    assert private not in serialized
    assert "response_131" not in serialized
    assert "transcript" not in serialized and "message" not in serialized
    # A repeated provider ID after transport replacement cannot combine counters.
    diagnostics.audio("response_131", 9600, at_ms=1500, session_count=2)
    after = diagnostics.report()
    assert after["responses"][-1]["session_count"] == 2
    assert after["responses"][-1]["model_audio_bytes"] == 9600
    assert before["responses"][-1]["model_audio_bytes"] == 4800  # Snapshot, not shared references.
    assert before["responses"][-1]["response_id"] == after["responses"][-1]["response_id"]


async def test_wire_ack_cancelled_response_and_recovery_preserve_input_and_playout(
    audio_pipeline, service, monkeypatch
):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    acknowledged = copy.deepcopy(
        next(event["session"] for event in model.sent if event["type"] == "session.update")
    )
    acknowledged["audio"]["input"]["turn_detection"].update(
        threshold=0.65, interrupt_response=True, create_response=True
    )
    acknowledged["audio"]["input"]["noise_reduction"] = {"type": "near_field"}
    model.emit({"type": "session.updated", "session": {"id": "offline-playout", **acknowledged}})
    await eventually(
        lambda: (
            service.call("owner", pipeline.call_id)["usage"]
            .get("accepted_voice_configuration", {})
            .get("turn_detection", {})
            .get("threshold")
            == 0.65
        )
    )
    accepted = service.call("owner", pipeline.call_id)["usage"]["accepted_voice_configuration"]
    assert accepted["turn_detection"]["interrupt_response"] is True
    assert accepted["turn_detection"]["create_response"] is True
    assert accepted["noise_reduction"] == {"type": "near_field"}

    original_emit = model.emit

    def cancelled_response(event):
        if event["type"] == "response.done" and event["response"]["id"] == "cancelled_first":
            event = copy.deepcopy(event)
            event["response"].update(
                status="cancelled",
                status_details={
                    "type": "cancelled",
                    "reason": "turn_detected",
                    "error": {"message": "fictional private diagnostic words"},
                },
            )
        original_emit(event)

    monkeypatch.setattr(model, "emit", cancelled_response)
    model.response("cancelled_first", milliseconds=600, frequency=700)
    await eventually(lambda: carrier.audio_bytes == 4800)
    await eventually(lambda: llm._current_audio_response is None)
    native_entered, release_native = asyncio.Event(), asyncio.Event()
    output = llm.interruption_barrier.output
    original_process = output.process_frame

    async def held_output_ack(frame, direction):
        if isinstance(frame, InterruptionFrame):
            native_entered.set()
            await release_native.wait()
        await original_process(frame, direction)

    monkeypatch.setattr(output, "process_frame", held_output_ack)
    model.speech(started=True, turn=1)
    try:
        await asyncio.wait_for(native_entered.wait(), 2)
        dispatched = llm.media_progress["input_frames_dispatched"]
        for _ in range(3):
            pipeline.feed_input()
        await eventually(lambda: llm.media_progress["input_frames_dispatched"] == dispatched + 3)
    finally:
        release_native.set()
    await eventually(lambda: carrier.clears == 1)
    clear = carrier.clear_events[-1]
    model.speech(started=False, turn=1)
    model.response("recovery_second", milliseconds=1200, frequency=900)
    await eventually(lambda: carrier.audio_bytes == 14400)
    await eventually(lambda: llm._current_audio_response is None)
    assert_waveform(
        carrier.audio_after(clear.event_index),
        milliseconds=1200,
        frequency=900,
        absent_frequency=700,
    )
    llm.save_usage(closed=True)
    recorded = service.call("owner", pipeline.call_id)["usage"]["realtime_interruption_diagnostics"]
    assert len(recorded["events"]) == 6
    first, second = recorded["responses"]
    assert first["status"] == "cancelled" and first["reason"] == "turn_detected"
    assert first["model_audio_bytes"] == first["forwarded_audio_bytes"] == 28800
    assert first["audio_delta_events"] == 6
    assert second["status"] == "completed"
    assert second["model_audio_bytes"] == second["forwarded_audio_bytes"] == 57600
    assert first["response_id"] == hashlib.sha256(b"cancelled_first").hexdigest()[:24]
    assert all(event["session_count"] == 1 for event in recorded["events"])
    assert recorded["clock_epoch_ms"] == llm.stage_clock_epoch_ms
    starts = [e for e in recorded["events"] if e["type"] == "input_audio_buffer.speech_started"]
    stops = [e for e in recorded["events"] if e["type"] == "input_audio_buffer.speech_stopped"]
    assert starts[0]["audio_start_ms"] == stops[0]["audio_end_ms"] == 2000
    assert starts[0]["item_id"] == stops[0]["item_id"]
    assert "fictional private diagnostic words" not in json.dumps(recorded)
    assert carrier.clears == 1


async def test_native_timeline_adds_no_storage_before_interruption(service, brief, monkeypatch):
    call_id = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")

    def forbidden(*args, **kwargs):
        raise AssertionError("Native diagnostic observation must not access storage")

    with monkeypatch.context() as blocked:
        blocked.setattr(service.store, "get", forbidden)
        blocked.setattr(service, "save", forbidden)
        # No item ID means the existing recipient-turn bookkeeping has no
        # independent reason to write. Diagnostic timing must remain in memory.
        await llm.observe({"type": "input_audio_buffer.speech_started", "audio_start_ms": 100})
        await llm.observe({"type": "input_audio_buffer.speech_stopped", "audio_end_ms": 400})
    assert len(llm.interruption_diagnostics.report()["events"]) == 2
    assert "realtime_interruption_diagnostics" not in service.call("owner", call_id)["usage"]
    llm.save_usage(closed=True)
    recorded = service.call("owner", call_id)["usage"]["realtime_interruption_diagnostics"]
    assert [event["audio_start_ms"] for event in recorded["events"][:1]] == [100]
    assert recorded["events"][1]["audio_end_ms"] == 400


def test_last_native_state_survives_rollover_without_inventing_stop_or_commit():
    diagnostics = NativeInterruptionDiagnostics(1234000)
    assert diagnostics.report()["native_turn_state"] == {}
    for kind, audio_key, value in (
        ("speech_started", "audio_start_ms", 100),
        ("speech_stopped", "audio_end_ms", 400),
        ("committed", None, None),
    ):
        diagnostics.observe(
            {"type": "input_audio_buffer." + kind, "item_id": "turn_one", audio_key: value},
            at_ms=500,
            session_count=1,
        )
    diagnostics.observe(
        {"type": "input_audio_buffer.speech_started", "item_id": "turn_two", "audio_start_ms": 700},
        at_ms=800,
        session_count=2,
    )
    for index in range(LIMIT + 1):
        diagnostics.observe(
            {"type": "response.created", "response": {"id": f"reply_{index}"}},
            at_ms=900 + index,
            session_count=2,
        )
    report = diagnostics.report()
    state = report["native_turn_state"]
    assert all(event["type"] == "response.created" for event in report["events"])
    assert state["last_event"] == state["last_started"]
    assert state["last_started"]["session_count"] == 2
    assert state["last_stopped"]["session_count"] == state["last_committed"]["session_count"] == 1
    assert state["last_started"]["item_id"] != state["last_stopped"]["item_id"]
    assert state["last_stopped"]["audio_end_ms"] == 400
    state["last_started"]["audio_start_ms"] = -1
    assert diagnostics.report()["native_turn_state"]["last_started"]["audio_start_ms"] == 700


def test_connection_history_preserves_first_observation_and_redacts_all_other_content():
    diagnostics = NativeInterruptionDiagnostics(1234000)
    private = "fictional private closure words"
    assert not diagnostics.connection(private, at_ms=1, session_count=1, code=1000)
    diagnostics.connection(
        "carrier_transport_disconnect",
        at_ms=1,
        session_count=1,
        code=1006,
        input_state={
            "queued_frames": 2,
            "dispatch_phase": private,
            "stop_requested": False,
            "queued_ms": float("inf"),
            "transcript": private,
        },
    )
    for index in range(LIMIT + 1):
        diagnostics.connection("model_receive_ended", at_ms=2 + index, session_count=1, code=4001)
    lifecycle = diagnostics.report()["connection_lifecycle"]
    assert len(lifecycle["events"]) == LIMIT
    assert lifecycle["events_dropped"] == 2
    assert lifecycle["first_observed"]["side"] == "logical_carrier"
    assert lifecycle["first_observed"]["code"] == 1006
    assert lifecycle["first_observed"]["input_state"] == {
        "queued_frames": 2,
        "queued_ms": None,
        "dispatch_phase": "unknown",
        "stop_requested": False,
    }
    assert all("code" not in event for event in lifecycle["events"])
    assert private not in json.dumps(lifecycle, allow_nan=False)
    lifecycle["first_observed"]["input_state"]["queued_frames"] = -1
    assert (
        diagnostics.report()["connection_lifecycle"]["first_observed"]["input_state"][
            "queued_frames"
        ]
        == 2
    )
