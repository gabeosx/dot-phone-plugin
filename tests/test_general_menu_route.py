"""A quote inquiry with menu permission stays in the scoped autonomous route."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree

import pytest

pytest.importorskip("pipecat")

from dots_phone.pipecat_voice import PipecatTelephony
from dots_phone.service import Refused
from tests.test_general_call_reasoner import prepare_general, start

ACTIONS = ["ask_question", "record_call_result", "navigate_phone_menu", "read_call_sources"]


@pytest.fixture(autouse=True)
def no_provider_connections(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("General menu checks must not contact a provider")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.mark.parametrize("enabled", [False, True])
def test_quote_with_menu_permission_reports_actual_autonomous_capabilities(
    service, brief, tmp_path, enabled
):
    service.settings.pipecat_account_dtmf = enabled
    service.provider = PipecatTelephony(service.settings, service, MagicMock())
    prepared = prepare_general(service, brief, tmp_path, actions=ACTIONS)
    assert prepared["calling_worker_mode"] == "autonomous_local_reasoner"
    assert prepared["information_capabilities"]["independent_reasoner"] is True
    assert prepared["interaction_capabilities"] == {
        "keypad_menu": enabled,
        "keypad_verification": False,
        "owner_voice_join": False,
    }
    assert not prepared["host_lifecycle"]["required"]
    assert "three_active_call_event_subscriptions" not in prepared["approval_requirements"]
    call = service.call("owner", prepared["call_id"])
    assert call["runtime_supervision"] == "local_reasoner"
    assert service.local_context_worker.scope(call)["read_only_general"]
    assert call["provider_sid"] is None


@pytest.mark.parametrize("failure", ["missing_worker", "wrong_client", "stale_heartbeat"])
def test_menu_permission_does_not_bypass_general_start_guards(service, brief, tmp_path, failure):
    import json

    prepared = prepare_general(service, brief, tmp_path, actions=ACTIONS)
    if failure == "missing_worker":
        service.local_context_worker = None
    elif failure == "stale_heartbeat":
        (tmp_path / "reasoner-ready.json").write_text(
            json.dumps({"serving": True, "heartbeat_ms": 0})
        )
    with pytest.raises(Refused):
        start(service, prepared, client_id="other" if failure == "wrong_client" else "client")
    assert not service.provider.dials


@pytest.mark.parametrize(
    "extra", ["purchase_ticket", "make_reservation", "verify_account_identity"]
)
def test_general_menu_route_does_not_admit_unapproved_action_classes(
    service, brief, tmp_path, extra
):
    prepared = prepare_general(service, brief, tmp_path, actions=[*ACTIONS, extra])
    assert prepared["calling_worker_mode"] == "manual"
    with pytest.raises(Refused):
        service.local_context_worker.scope(service.call("owner", prepared["call_id"]))
    assert not service.provider.dials


async def test_general_menu_control_preserves_call_and_checks_exact_authorized_scope(
    service, brief, tmp_path
):
    prepared = prepare_general(service, brief, tmp_path, actions=ACTIONS)
    service.local_context_worker.before_dial = AsyncMock()
    start(service, prepared)
    await service.tick()
    call = service.call("owner", prepared["call_id"])
    assert service.provider.dials == [call["call_id"]]
    service.settings.pipecat_account_dtmf = True
    client = MagicMock()
    client.calls.return_value.update.return_value = SimpleNamespace(sid=call["provider_sid"])
    provider = PipecatTelephony(service.settings, service, client)
    session = SimpleNamespace(
        carrier=SimpleNamespace(
            begin_change=lambda: "offline-reattach",
            wait_change=AsyncMock(),
            close=AsyncMock(),
            stream_count=2,
        ),
        ending=False,
        llm=SimpleNamespace(
            dialog=[{"role": "user", "text": "For fares and schedules, press two."}],
            clear_playback=AsyncMock(),
        ),
        menu_lock=asyncio.Lock(),
        used_menus=set(),
    )
    provider.sessions[call["provider_sid"]] = session
    result = await provider.press_phone_key(call["call_id"], "2")
    assert result["carrier_control_accepted"] and result["audio_reconnected"]
    command = ElementTree.fromstring(client.calls.return_value.update.call_args.kwargs["twiml"])
    assert command.find("Play").attrib["digits"] == "2"
    client.calls.create.assert_not_called()
    with pytest.raises(Refused):
        await provider.press_phone_key(call["call_id"], "2")
    session.llm.dialog.append({"role": "user", "text": "Please enter the account number."})
    with pytest.raises(Refused, match="approved"):
        await provider.enter_verification_fact(call["call_id"], "account_number")
    assert client.calls.return_value.update.call_count == 1
