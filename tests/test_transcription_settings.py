"""Explicit transcription selection reaches both production voice profiles."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dots_phone.config import Settings


def test_transcription_environment_default_and_explicit_rollback(monkeypatch):
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
    assert Settings.from_env().pipecat_transcription_model == "gpt-transcribe"
    for model in ("gpt-realtime-whisper", "gpt-transcribe"):
        monkeypatch.setenv("DOTS_PHONE_PIPECAT_TRANSCRIPTION_MODEL", model)
        assert Settings.from_env().pipecat_transcription_model == model


@pytest.mark.parametrize("model", [None, "", "typo", "gpt-live-transcribe", "whisper-1"])
def test_unconfigured_transcription_models_fail_before_a_voice_session(model):
    with pytest.raises(ValidationError, match="pipecat_transcription_model"):
        Settings(profile="evaluation", pipecat_transcription_model=model)


@pytest.mark.parametrize("route", ["general", "account"])
@pytest.mark.parametrize("model", ["gpt-transcribe", "gpt-realtime-whisper"])
async def test_general_and_task_factories_send_the_configured_model(
    service, brief, tmp_path, route, model
):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice
    from tests.test_account_task import account_call
    from tests.test_general_call_reasoner import prepare_general

    if route == "general":
        prepared = prepare_general(service, brief, tmp_path)
    else:
        prepared, _ = account_call(service, brief)
    service.settings.pipecat_transcription_model = model
    call = service.call("owner", prepared["call_id"])
    voice, _, _ = create_task_voice(service, call, api_key="offline-fixture")
    voice._websocket = SimpleNamespace(send=AsyncMock())
    await voice._send_session_update()
    sent = json.loads(voice._websocket.send.call_args.args[0])
    expected = {"model": model}
    if model == "gpt-realtime-whisper":
        expected["language"] = "en"
    assert sent["session"]["audio"]["input"]["transcription"] == expected
    assert not service.provider.dials
