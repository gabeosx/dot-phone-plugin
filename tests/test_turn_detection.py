import pytest

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import events

from dots_phone.config import Settings
from dots_phone.task_voice import task_turn_detection


def serialized_detector(settings):
    session = events.SessionProperties(
        audio=events.AudioConfiguration(
            input=events.AudioInput(turn_detection=task_turn_detection(settings))
        )
    )
    return events.SessionUpdateEvent(session=session).model_dump(exclude_none=True)["session"][
        "audio"
    ]["input"]["turn_detection"]


@pytest.mark.parametrize("eagerness", ["low", "medium", "high", "auto"])
def test_semantic_turn_detection_survives_provider_serialization_without_server_fields(eagerness):
    assert serialized_detector(Settings(profile="evaluation", pipecat_vad_eagerness=eagerness)) == {
        "type": "semantic_vad",
        "eagerness": eagerness,
        "create_response": True,
        "interrupt_response": True,
    }


def test_silence_detector_can_restore_prior_setting():
    detector = serialized_detector(
        Settings(profile="evaluation", pipecat_turn_detection="server_vad")
    )
    assert detector["type"] == "server_vad" and detector["silence_duration_ms"] == 1000
    assert "eagerness" not in detector


def test_turn_detection_environment_settings_and_bounds(monkeypatch):
    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_TURN_DETECTION", "server_vad")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_VAD_SILENCE_MS", "900")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_VAD_EAGERNESS", "medium")
    settings = Settings.from_env()
    assert settings.pipecat_vad_eagerness == "medium"
    assert serialized_detector(settings)["silence_duration_ms"] == 900
    for change in (
        {"pipecat_turn_detection": "typo"},
        {"pipecat_vad_eagerness": "typo"},
        {"pipecat_vad_silence_ms": 0},
        {"pipecat_general_turn_detection": "typo"},
        {"pipecat_noise_reduction": "typo"},
    ):
        with pytest.raises(ValueError):
            Settings(profile="evaluation", **change)


def test_native_noise_candidates_are_explicit_and_defaults_stay_unfiltered(monkeypatch):
    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    monkeypatch.delenv("DOTS_PHONE_GENERAL_TURN_DETECTION", raising=False)
    monkeypatch.delenv("DOTS_PHONE_PIPECAT_NOISE_REDUCTION", raising=False)
    settings = Settings.from_env()
    assert settings.pipecat_general_turn_detection == "server_vad"
    assert settings.pipecat_noise_reduction == "off"
    monkeypatch.setenv("DOTS_PHONE_GENERAL_TURN_DETECTION", "semantic_vad")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_NOISE_REDUCTION", "far_field")
    settings = Settings.from_env()
    assert settings.pipecat_general_turn_detection == "semantic_vad"
    assert settings.pipecat_noise_reduction == "far_field"
