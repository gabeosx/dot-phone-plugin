"""Offline wire checks for the general-call VAD setting, not classifier tests."""

import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import events

from dots_phone.config import Settings
from dots_phone.task_voice import task_turn_detection
from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)


@pytest.fixture
def settings(settings, request):
    if hasattr(request, "param"):
        if isinstance(request.param, dict):
            for key, value in request.param.items():
                setattr(settings, key, value)
        else:
            settings.pipecat_general_vad_threshold = request.param
    return settings


@pytest.fixture
def service(service, request, monkeypatch, tmp_path):
    route = getattr(request, "param", None)
    if route == "hotel":
        from dots_phone import local_context_worker, prepared_evidence

        service.local_context_worker = SimpleNamespace(
            schema=3, directory=tmp_path, registered=lambda call: True, scope=lambda call: {}
        )
        monkeypatch.setattr(
            local_context_worker, "hotel_instructions", lambda call: "Fixture hotel"
        )
        monkeypatch.setattr(prepared_evidence, "prepared_instructions", lambda *args: "")
    elif route in {"mail", "gmail"}:
        from dots_phone import local_gmail_worker, local_mail_worker

        bridge = SimpleNamespace(
            registered=lambda call: True, scope=lambda call: {"narrow_tool": route == "gmail"}
        )
        if route == "gmail":
            service.local_gmail_worker = bridge
            monkeypatch.setattr(local_gmail_worker, "marker_instructions", lambda call: "Fixture")
        else:
            service.local_mail_worker = bridge
            monkeypatch.setattr(local_mail_worker, "mail_instructions", lambda call: "Fixture")
    return service


def input_settings(pipeline):
    updates = [event for event in pipeline.model.sent if event["type"] == "session.update"]
    assert updates, "The real SDK must send a session update to the in-memory model"
    return updates[-1]["session"]["audio"]["input"]


async def test_general_default_reaches_actual_session_update(audio_pipeline):
    config = input_settings(audio_pipeline)
    assert config["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 300,
        "idle_timeout_ms": None,
        "create_response": True,
        "interrupt_response": True,
    }
    assert config["noise_reduction"] is None
    assert audio_pipeline.llm.interruption_barrier is not None


@pytest.mark.parametrize("service", ["general", "hotel"], indirect=True)
@pytest.mark.parametrize(
    "settings",
    [{"local_reasoner_voice_effort": effort} for effort in ("low", "medium")],
    indirect=True,
)
async def test_ordinary_and_hotel_voice_use_the_configured_effort(settings, audio_pipeline):
    updates = [e for e in audio_pipeline.model.sent if e["type"] == "session.update"]
    assert updates[-1]["session"]["reasoning"] == {"effort": settings.local_reasoner_voice_effort}
    # The voice setting does not silently retune the separate Codex model.
    assert settings.local_reasoner_effort == "medium"


@pytest.mark.parametrize(
    "settings",
    [
        {"pipecat_general_turn_detection": "semantic_vad"},
    ],
    indirect=True,
)
async def test_general_semantic_candidate_reaches_actual_wire(audio_pipeline):
    assert input_settings(audio_pipeline)["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }


@pytest.mark.parametrize(
    "settings",
    [{"pipecat_noise_reduction": mode} for mode in ("near_field", "far_field", "off")],
    indirect=True,
)
async def test_filter_and_explicit_disable_reach_actual_wire(settings, audio_pipeline):
    expected = (
        None
        if settings.pipecat_noise_reduction == "off"
        else {"type": settings.pipecat_noise_reduction}
    )
    assert input_settings(audio_pipeline)["noise_reduction"] == expected


@pytest.mark.parametrize("settings", [0.5, 0.6, 0.0, 1.0], indirect=True)
async def test_general_threshold_override_reaches_wire(settings, audio_pipeline):
    assert input_settings(audio_pipeline)["turn_detection"] == {
        "type": "server_vad",
        "threshold": settings.pipecat_general_vad_threshold,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 300,
        "idle_timeout_ms": None,
        "create_response": True,
        "interrupt_response": True,
    }


@pytest.mark.parametrize("service", ["hotel", "mail", "gmail"], indirect=True)
@pytest.mark.parametrize(
    "settings",
    [
        {"pipecat_turn_detection": "semantic_vad", "pipecat_vad_eagerness": "low"},
        {"pipecat_turn_detection": "server_vad", "pipecat_vad_silence_ms": 1700},
    ],
    indirect=True,
)
async def test_registered_routes_honor_the_task_detector(settings, service, audio_pipeline):
    wire = input_settings(audio_pipeline)
    if settings.pipecat_turn_detection == "semantic_vad":
        assert wire["turn_detection"] == {
            "type": "semantic_vad",
            "eagerness": "low",
            "create_response": True,
            "interrupt_response": True,
        }
    else:
        assert wire["turn_detection"] == {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 1700,
        }
    update = [e["session"] for e in audio_pipeline.model.sent if e["type"] == "session.update"][-1]
    assert update["max_output_tokens"] == 1024


@pytest.mark.parametrize("mode", ["semantic_vad", "server_vad"])
def test_shared_account_detector_ignores_general_threshold(mode):
    def wire(threshold):
        settings = Settings(
            profile="evaluation",
            pipecat_turn_detection=mode,
            pipecat_general_vad_threshold=threshold,
        )
        session = events.SessionProperties(
            audio=events.AudioConfiguration(
                input=events.AudioInput(turn_detection=task_turn_detection(settings))
            )
        )
        return events.SessionUpdateEvent(session=session).model_dump(exclude_none=True)["session"]

    assert wire(0.6) == wire(0.5)


def test_general_threshold_environment_and_rollback(monkeypatch):
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
    assert Settings.from_env().pipecat_general_vad_threshold == 0.5
    monkeypatch.setenv("DOTS_PHONE_GENERAL_VAD_THRESHOLD", "0.6")
    assert Settings.from_env().pipecat_general_vad_threshold == 0.6
    monkeypatch.setenv("DOTS_PHONE_GENERAL_VAD_THRESHOLD", "0.5")
    assert Settings.from_env().pipecat_general_vad_threshold == 0.5
    monkeypatch.setenv("DOTS_PHONE_GENERAL_VAD_THRESHOLD", "1.01")
    with pytest.raises(ValidationError, match="pipecat_general_vad_threshold"):
        Settings.from_env()


@pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan"), float("inf"), "invalid"])
def test_general_threshold_rejects_invalid_values(threshold):
    with pytest.raises(ValidationError, match="pipecat_general_vad_threshold"):
        Settings(profile="evaluation", pipecat_general_vad_threshold=threshold)
