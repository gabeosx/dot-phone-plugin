import asyncio
import json
from types import SimpleNamespace

import pytest

from dots_phone.voice import LiveTelephony, SupervisedProvider, voice_config


async def test_legacy_stop_waits_for_complete_input_and_closes_once(service, brief, monkeypatch):
    from unittest.mock import AsyncMock

    from dots_phone.voice import GPTLiveProvider
    from tests.conftest import started

    call_id, _ = await started(service, brief)
    provider = SupervisedProvider.__new__(SupervisedProvider)
    provider.supervisor = service
    dialog = []
    provider.get_transcript = lambda conversation: dialog
    session = SimpleNamespace(metadata={"dots_call_id": call_id})
    service.end = AsyncMock()
    monkeypatch.setattr(GPTLiveProvider, "_dispatch_model_event", AsyncMock())

    async def event(role):
        await provider._dispatch_model_event(
            "fixture", session, {"type": f"session.{role}_transcript.delta"}
        )

    dialog.append({"role": "user", "text": "Please hang up"})
    await event("input")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    dialog[-1]["text"] += " if you are done."
    dialog.append({"role": "assistant", "text": "Main menu."})
    await event("output")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()

    dialog.append({"role": "user", "text": "Please hang up now."})
    await event("input")
    dialog.append({"role": "assistant", "text": "Goodbye."})
    await event("output")
    await event("output")
    await asyncio.sleep(0)
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")


async def test_telephony_options_bound_duration_no_recording_or_redial(brief):
    captured = []

    async def initiate(options):
        captured.append(options)
        return SimpleNamespace(call_sid="CA" + "1" * 32)

    provider = LiveTelephony.__new__(LiveTelephony)
    provider.settings = SimpleNamespace(public_base_url="https://phone.example")
    provider.channel = SimpleNamespace(initiate_outbound_conversation=initiate)
    call = {"call_id": "owned-job", "brief": brief.model_dump(mode="json")}
    assert await provider.dial(call) == "CA" + "1" * 32
    options = captured[0]
    assert options.call_options.timeout == 20
    assert options.call_options.time_limit == 300
    assert options.call_options.record is False
    assert options.twiml_options.custom_parameters == {"dots_call_id": "owned-job"}
    assert options.session_config["delegation"] == {"type": "client"}
    assert options.session_config["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert "without a routine unsolicited AI introduction" in options.session_config["instructions"]
    assert (
        "If asked about your identity or whether you are AI, answer truthfully"
        in options.session_config["instructions"]
    )
    assert (
        "Follow explicit call-specific disclosure requirements"
        in options.session_config["instructions"]
    )
    assert len(captured) == 1


async def test_carrier_stops_before_model_finalization_and_usage_wait():
    order = []
    closed = asyncio.Event()

    async def send(data):
        order.append(json.loads(data)["type"])
        closed.set()

    async def close():
        order.append("socket.close")

    def update(**args):
        assert args == {"status": "completed"}
        order.append("carrier.hangup")

    def fetch():
        order.append("carrier.fetch")
        return SimpleNamespace(status="completed")

    provider = LiveTelephony.__new__(LiveTelephony)
    provider.client = SimpleNamespace(calls=lambda sid: SimpleNamespace(update=update, fetch=fetch))
    socket = SimpleNamespace(send=send, close=close)
    provider.channel = SimpleNamespace(
        _provider=SimpleNamespace(
            _calls={"CA1": SimpleNamespace(model_ws=socket, twilio_ws=socket, closed_event=closed)}
        )
    )
    await provider.stop({"provider_sid": "CA1"})
    assert order == [
        "carrier.hangup",
        "session.close",
        "socket.close",
        "socket.close",
        "carrier.fetch",
    ]


def test_undisclosed_facts_excluded_from_voice_context(brief):
    brief.disclosures = []
    config = voice_config({"brief": brief.model_dump(mode="json")})
    assert "2026-11-06" not in config["instructions"]
    assert "client" == config["delegation"]["type"]


def test_legacy_voice_uses_task_opening_and_preserves_explicit_disclosures(brief):
    requirement = "Tell the person before the request that you are an AI assistant."
    brief.constraints = [requirement]
    instructions = voice_config({"brief": brief.model_dump(mode="json")})["instructions"]
    assert (
        "make the approved request directly without a routine unsolicited AI introduction"
        in instructions
    )
    assert "If asked about your identity or whether you are AI, answer truthfully" in instructions
    assert "never claim to be human or the owner" in instructions
    assert (
        "Follow explicit call-specific disclosure requirements in the approved brief"
        in instructions
    )
    assert requirement in instructions
    assert "Stay silent through automated greetings and menus" in instructions
    assert "Introduce yourself briefly as an AI assistant" not in instructions


async def test_supervisor_update_does_not_succeed_on_missing_voice_socket():
    provider = LiveTelephony.__new__(LiveTelephony)
    provider.channel = SimpleNamespace(_provider=SimpleNamespace(_calls={}))
    with pytest.raises(RuntimeError, match="unavailable"):
        await provider.update(
            {"provider_sid": "CA1", "pending": {"request_id": "request"}},
            "delegation",
            "Scoped answer",
        )


async def test_pinned_tac_disconnect_cleanup_awaits_final_usage(monkeypatch):
    waits, commands, ended = [], [], []
    original = asyncio.wait_for
    closed = asyncio.Event()

    async def wait_for(awaitable, timeout):
        waits.append(timeout)
        return await original(awaitable, timeout)

    async def send(message):
        commands.append(json.loads(message)["type"])
        asyncio.get_running_loop().call_later(0.01, closed.set)

    async def close():
        commands.append("socket.close")

    async def end_conversation(id):
        ended.append(id)

    provider = SupervisedProvider.__new__(SupervisedProvider)
    provider._calls = {
        "CA1": SimpleNamespace(
            model_ws=SimpleNamespace(send=send, close=close), closed_event=closed
        )
    }
    provider.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)
    provider.tac_config = SimpleNamespace(account_sid="synthetic")
    provider.channel = SimpleNamespace(
        _end_conversation=end_conversation,
        tac=SimpleNamespace(is_orchestrator_enabled=lambda: False),
    )
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await provider._cleanup_call("CA1")
    assert waits == [15]
    assert commands == ["session.close", "socket.close"]
    assert ended == ["CA1"] and provider._calls == {}
