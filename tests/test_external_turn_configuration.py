"""Default-off admission checks; no call, model, or carrier connection."""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.config import Settings
from dots_phone.pipecat_voice import PipecatTelephony
from dots_phone.service import Refused


def external_settings(**kwargs):
    values = {
        "profile": "evaluation",
        "voice_backend": "pipecat",
        "pipecat_engine": "realtime",
        "pipecat_external_turns": True,
    }
    return Settings(**(values | kwargs))


def test_default_native_path_requires_no_source(settings, service):
    assert not settings.pipecat_external_turns
    provider = PipecatTelephony(settings, service, MagicMock())
    assert provider.external_turn_source_factory is None


@pytest.mark.parametrize("factory", [None, 3, "detector"])
def test_enabled_path_requires_explicit_factory_before_client_creation(
    service, monkeypatch, factory
):
    client = MagicMock(side_effect=AssertionError("No client construction on invalid config"))
    monkeypatch.setattr("dots_phone.pipecat_voice.Client", client)
    with pytest.raises(ValueError, match="explicit per-call source"):
        PipecatTelephony(external_settings(), service, external_turn_source_factory=factory)
    client.assert_not_called()


def test_source_cannot_silently_enable_mode(settings, service):
    with pytest.raises(ValueError, match="PIPECAT_EXTERNAL_TURNS"):
        PipecatTelephony(
            settings, service, MagicMock(), external_turn_source_factory=lambda _: None
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"voice_backend": "tac"},
        {"pipecat_engine": "live"},
        {"account_tasks": True},
        {"pipecat_owned_dtmf": True},
        {"local_gmail_worker_directory": "/tmp/unused-gmail-fixture"},
        {"local_mail_worker_directory": "/tmp/unused-mail-fixture"},
        {"local_context_worker_directory": "/tmp/unused-context-fixture"},
    ],
)
def test_unsupported_modes_are_rejected_at_configuration(overrides):
    with pytest.raises(ValueError, match="ordinary general Realtime"):
        external_settings(**overrides)


async def test_prepared_route_rejected_before_carrier_dial(service):
    client = MagicMock()
    provider = PipecatTelephony(
        external_settings(), service, client, external_turn_source_factory=lambda _: None
    )
    with pytest.raises(Refused, match="prepared-evidence"):
        await provider.dial({"prepared_evidence": {"synthetic": True}})
    client.calls.create.assert_not_called()


async def test_registered_worker_route_rejected_before_dial(service):
    client = MagicMock()
    service.local_gmail_worker = MagicMock()
    service.local_gmail_worker.registered.return_value = True
    provider = PipecatTelephony(
        external_settings(), service, client, external_turn_source_factory=lambda _: None
    )
    with pytest.raises(Refused, match="worker routes"):
        await provider.dial({})
    client.calls.create.assert_not_called()


async def test_persisted_reasoner_route_is_rejected_even_without_current_registry(service):
    client = MagicMock()
    provider = PipecatTelephony(
        external_settings(), service, client, external_turn_source_factory=lambda _: None
    )
    with pytest.raises(Refused, match="worker routes"):
        await provider.dial({"runtime_supervision": "local_reasoner"})
    client.calls.create.assert_not_called()


async def test_mutated_mode_cannot_enable_unconfigured_source(settings, service):
    client = MagicMock()
    provider = PipecatTelephony(settings, service, client)
    settings.pipecat_external_turns = True
    with pytest.raises(Refused, match="turn mode changed"):
        await provider.dial({})
    client.calls.create.assert_not_called()
