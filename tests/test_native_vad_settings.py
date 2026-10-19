"""Offline regressions for native GA VAD controls through the pinned SDK."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import events

from dots_phone.config import Settings
from dots_phone.pipecat_realtime import (
    InputAudioBufferTimeoutTriggered,
    NativeServerVAD,
    SupervisedRealtimeService,
    accepted_native_input_configuration,
    native_general_turn_detection,
)
from dots_phone.task_voice import task_turn_detection
from tests.test_general_vad_threshold import (
    input_settings,
)
from tests.test_general_vad_threshold import service as service
from tests.test_general_vad_threshold import settings as settings
from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_manual_integration import manual_pipeline as manual_pipeline
from tests.test_realtime_playout import eventually


@pytest.mark.parametrize(
    "settings",
    [{"pipecat_general_vad_threshold": 0.7, "pipecat_general_vad_idle_timeout_ms": 6000}],
    indirect=True,
)
async def test_native_candidate_survives_real_pipeline_serialization_and_ack(
    settings, service, audio_pipeline
):
    wire = input_settings(audio_pipeline)["turn_detection"]
    assert wire == {
        "type": "server_vad",
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 300,
        "idle_timeout_ms": 6000,
        "create_response": True,
        "interrupt_response": True,
    }
    accepted = service.call("owner", audio_pipeline.call_id)["usage"][
        "accepted_voice_configuration"
    ]
    assert accepted["turn_detection"] == wire
    assert audio_pipeline.llm.ready.is_set()

    # A same-session rollback must send null, not omit and retain the old value.
    settings.pipecat_general_vad_idle_timeout_ms = 0
    llm = audio_pipeline.llm
    llm._settings.session_properties.audio.input.turn_detection = native_general_turn_detection(
        settings
    )
    await llm._send_session_update()
    assert input_settings(audio_pipeline)["turn_detection"]["idle_timeout_ms"] is None
    await eventually(
        lambda: (
            service.call("owner", audio_pipeline.call_id)["usage"]["accepted_voice_configuration"][
                "turn_detection"
            ]["idle_timeout_ms"]
            is None
        )
    )


@pytest.mark.parametrize("create_response,interrupt_response", [(True, True), (False, False)])
async def test_native_schema_preserves_explicit_booleans_and_unrelated_partial_updates(
    service, brief, create_response, interrupt_response
):
    call_id = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    detector = NativeServerVAD(
        idle_timeout_ms=6000,
        create_response=create_response,
        interrupt_response=interrupt_response,
    )
    await llm.send_client_event(
        events.SessionUpdateEvent(
            session=events.SessionProperties(
                audio=events.AudioConfiguration(input=events.AudioInput(turn_detection=detector))
            )
        )
    )
    wire = json.loads(llm._websocket.send.call_args.args[0])["session"]["audio"]["input"][
        "turn_detection"
    ]
    assert wire["idle_timeout_ms"] == 6000
    assert wire["create_response"] is create_response
    assert wire["interrupt_response"] is interrupt_response
    await llm.send_client_event(
        events.SessionUpdateEvent(session=events.SessionProperties(instructions="Fixture update"))
    )
    assert json.loads(llm._websocket.send.call_args.args[0])["session"] == {
        "type": "realtime",
        "instructions": "Fixture update",
    }


@pytest.mark.parametrize("settings", [{"pipecat_general_vad_idle_timeout_ms": 6000}], indirect=True)
@pytest.mark.parametrize("audio_start,audio_end", [(1000, 7000), (1000.25, 7000.25)])
async def test_idle_notification_does_not_crash_receiver_or_create_duplicate_response(
    audio_pipeline,
    audio_start,
    audio_end,
):
    model, llm, carrier = audio_pipeline.model, audio_pipeline.llm, audio_pipeline.carrier
    before_creates = sum(event["type"] == "response.create" for event in model.sent)
    before_clears = carrier.clears
    model.emit(
        {
            "type": "input_audio_buffer.timeout_triggered",
            "audio_start_ms": audio_start,
            "audio_end_ms": audio_end,
            "item_id": "idle_input_1",
        }
    )
    model.emit({"type": "input_audio_buffer.committed", "item_id": "idle_input_1"})
    model.response("provider_idle_reply", milliseconds=600)
    await eventually(lambda: carrier.audio_bytes == 4800)
    await eventually(lambda: llm._current_audio_response is None)
    assert llm.media_progress["idle_timeout_events"] == 1
    assert sum(event["type"] == "response.create" for event in model.sent) == before_creates
    assert carrier.clears == before_clears
    assert not llm.caller_speaking
    assert llm.receive_outcome is None
    assert not audio_pipeline.runner_task.done()


@pytest.mark.parametrize(
    "settings",
    [
        {
            "pipecat_general_turn_detection": "semantic_vad",
            "pipecat_general_vad_idle_timeout_ms": 6000,
        }
    ],
    indirect=True,
)
async def test_native_timeout_does_not_change_semantic_route(audio_pipeline):
    assert input_settings(audio_pipeline)["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }


@pytest.mark.parametrize(
    "settings",
    [{"pipecat_general_vad_idle_timeout_ms": 6000, "pipecat_turn_detection": "server_vad"}],
    indirect=True,
)
@pytest.mark.parametrize("service", ["hotel"], indirect=True)
async def test_native_timeout_does_not_change_registered_route(audio_pipeline):
    assert input_settings(audio_pipeline)["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 1000,
    }


@pytest.mark.parametrize("settings", [{"pipecat_general_vad_idle_timeout_ms": 6000}], indirect=True)
async def test_native_timeout_does_not_change_manual_route(manual_pipeline):
    assert input_settings(manual_pipeline)["turn_detection"] is None


@pytest.mark.parametrize("mode", ["semantic_vad", "server_vad"])
def test_task_detector_does_not_adopt_general_timeout(mode):
    settings = Settings(
        profile="evaluation",
        pipecat_turn_detection=mode,
        pipecat_general_vad_idle_timeout_ms=6000,
    )
    assert "idle_timeout_ms" not in task_turn_detection(settings).model_dump()


def test_acknowledgement_never_infers_missing_timeout_or_retains_arbitrary_content():
    def accepted(value):
        return accepted_native_input_configuration({"audio": {"input": {"turn_detection": value}}})[
            "turn_detection"
        ]

    assert "idle_timeout_ms" not in accepted({"type": "server_vad"})
    assert accepted({"type": "server_vad", "idle_timeout_ms": None})["idle_timeout_ms"] is None
    for value in (6000, 6000.0, 30000):
        assert accepted({"idle_timeout_ms": value})["idle_timeout_ms"] == value
    for value in (True, 0, 30001, "private fixture words", {"private": "data"}):
        assert accepted({"idle_timeout_ms": value})["idle_timeout_ms"] == "unknown"


@pytest.mark.parametrize("value", [-1, True, "1000", float("nan"), float("inf")])
def test_timeout_notification_rejects_invalid_timestamps(value):
    with pytest.raises(ValidationError):
        InputAudioBufferTimeoutTriggered.model_validate(
            {
                "type": "input_audio_buffer.timeout_triggered",
                "event_id": "fixture_timeout",
                "audio_start_ms": value,
                "audio_end_ms": 7000,
                "item_id": "idle_input_1",
            }
        )


@pytest.mark.parametrize("value", [-1, 1, 4999, 30001, 6000.5, "invalid"])
def test_idle_timeout_rejects_out_of_api_bounds(value):
    with pytest.raises(ValidationError, match="pipecat_general_vad_idle_timeout_ms"):
        Settings(profile="evaluation", pipecat_general_vad_idle_timeout_ms=value)


def test_idle_timeout_environment_default_enable_and_disable(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("DOTS_PHONE_") or key in {
            "OPENAI_API_KEY",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_API_KEY",
            "TWILIO_API_SECRET",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    assert Settings.from_env().pipecat_general_vad_idle_timeout_ms == 0
    for value in (5000, 6000, 30000, 0):
        monkeypatch.setenv("DOTS_PHONE_GENERAL_VAD_IDLE_TIMEOUT_MS", str(value))
        assert Settings.from_env().pipecat_general_vad_idle_timeout_ms == value
