"""Response policy and voice effort are independent deployment choices."""

import pytest
from pydantic import ValidationError

from dots_phone.config import Settings


def test_direct_policy_keeps_independent_reasoning_available(settings):
    settings.voice_backend = "pipecat"
    settings.pipecat_engine = "realtime"
    settings.account_tasks = True
    settings.local_reasoner_enabled = True
    settings.local_reasoner_directory = settings.database.parent / "reasoner"
    configured = Settings.model_validate(settings.model_dump())
    assert configured.local_reasoner_response_policy == "direct_with_codex"
    assert configured.local_reasoner_enabled
    assert configured.local_reasoner_voice_effort == "medium"
    assert configured.local_reasoner_effort == "medium"


def test_policy_and_effort_environment_are_independent(monkeypatch):
    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    monkeypatch.setenv("DOTS_PHONE_LOCAL_REASONER_RESPONSE_POLICY", "always_consult")
    monkeypatch.setenv("DOTS_PHONE_LOCAL_REASONER_VOICE_EFFORT", "low")
    settings = Settings.from_env()
    assert settings.local_reasoner_response_policy == "always_consult"
    assert settings.local_reasoner_voice_effort == "low"
    assert settings.local_reasoner_effort == "medium"
    monkeypatch.setenv("DOTS_PHONE_LOCAL_REASONER_RESPONSE_POLICY", "direct_with_codex")
    assert Settings.from_env().local_reasoner_voice_effort == "low"


def test_invalid_response_policy_is_rejected():
    with pytest.raises(ValidationError):
        Settings(profile="evaluation", local_reasoner_response_policy="skip_validation")


@pytest.mark.parametrize("timeout", [1, 2])
def test_reasoner_cannot_accept_a_timeout_consumed_by_its_transport_margin(timeout, tmp_path):
    with pytest.raises(ValidationError, match="at least 3 seconds"):
        Settings(
            profile="evaluation",
            voice_backend="pipecat",
            pipecat_engine="realtime",
            account_tasks=True,
            local_reasoner_enabled=True,
            local_reasoner_directory=tmp_path,
            decision_seconds=timeout,
        )
    # The legacy host decision path has no Codex transport margin.
    assert Settings(profile="evaluation", decision_seconds=timeout).decision_seconds == timeout
