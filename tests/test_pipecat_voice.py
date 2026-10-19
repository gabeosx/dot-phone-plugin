import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import InputAudioRawFrame, SpeechOutputAudioRawFrame
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.openai.live import events
from starlette.websockets import WebSocketDisconnect, WebSocketState

from dots_phone.pipecat_voice import (
    PipecatTelephony,
    SupervisedLiveService,
    bind_media,
    call_instructions,
)
from dots_phone.service import Refused
from tests.conftest import started


@pytest.mark.parametrize("policy", [None, "always_consult", "direct_with_codex"])
async def test_frozen_direct_policy_exposes_validated_local_keypad_controls(policy):
    from dots_phone.keypad_tools import task_keypad_tools

    call = {"call_id": "fixture", "runtime_supervision": "local_reasoner"}
    if policy:
        call["reasoner_response_policy"] = policy
    provider = SimpleNamespace(
        press_phone_key=AsyncMock(return_value={"carrier_control_accepted": True}),
        enter_verification_fact=AsyncMock(),
    )
    llm = SimpleNamespace(return_local_tool_result=AsyncMock())
    existing = [object()]
    tools, prompt = task_keypad_tools(provider, llm, call, existing, account=True)
    if policy != "direct_with_codex":
        assert tools is existing
        assert "use consult_call_reasoner" in prompt
        return
    assert {tool.__name__ for tool in tools[1:]} == {"press_phone_key", "enter_verification_fact"}
    assert "use enter_verification_fact" in prompt.lower()
    params = SimpleNamespace()
    await tools[1](params, "2", "For balance inquiries, press 2.")
    provider.press_phone_key.assert_awaited_once_with(
        "fixture", "2", "For balance inquiries, press 2."
    )
    llm.return_local_tool_result.assert_awaited_once_with(
        params, {"carrier_control_accepted": True}, run_llm=False
    )


@pytest.mark.parametrize("peer_closed,close_race", [(True, False), (False, True), (False, False)])
async def test_carrier_close_is_idempotent_after_peer_disconnect(
    settings, service, peer_closed, close_race
):
    import asyncio

    provider = PipecatTelephony(settings, service, MagicMock())
    closed = asyncio.Event()
    closed.set()
    websocket = SimpleNamespace(
        application_state=WebSocketState.CONNECTED,
        client_state=WebSocketState.DISCONNECTED if peer_closed else WebSocketState.CONNECTED,
        close=AsyncMock(side_effect=WebSocketDisconnect(1006) if close_race else None),
    )
    provider.sessions["CAfixture"] = SimpleNamespace(
        llm=SimpleNamespace(closed=closed), websocket=websocket
    )
    await provider.close_voice({"provider_sid": "CAfixture"})
    assert websocket.close.await_count == (0 if peer_closed else 1)


async def test_carrier_completion_keeps_final_voice_usage(service, brief):
    call_id, _ = await started(service, brief)

    async def finalize(call):
        fresh = service.store.get("call", call_id)
        fresh["usage"].update(live_seconds=18, live_usage_confirmed=True)
        service.save(fresh)

    service.provider.close_voice = finalize
    await service.callback(call_id, "CA" + "1" * 32, "completed", 20)
    usage = service.store.get("call", call_id)["usage"]
    assert usage["live_usage_confirmed"]
    assert usage["live_seconds"] == 18
    assert usage["provider_seconds"] == 20


async def test_media_binding_rejects_foreign_account_and_sid(service, brief):
    call_id, _ = await started(service, brief)
    start = {
        "accountSid": "AC" + "2" * 32,
        "callSid": "CA" + "1" * 32,
        "streamSid": "MZ" + "3" * 32,
        "customParameters": {"dots_call_id": call_id},
    }
    with pytest.raises(Refused):
        bind_media(service, start, "AC" + "4" * 32)
    assert bind_media(service, start, start["accountSid"])["call_id"] == call_id
    start["callSid"] = "CA" + "5" * 32
    with pytest.raises(Refused):
        bind_media(service, start, start["accountSid"])


async def test_twilio_serializer_preserves_owned_stream_pcmu_codec():
    serializer = TwilioFrameSerializer(
        stream_sid="fixture-stream", params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
    )
    await serializer.setup(SimpleNamespace(audio_in_sample_rate=8000))
    frame = await serializer.deserialize(
        json.dumps(
            {"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}}
        )
    )
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 8000 and len(frame.audio) == 320
    outgoing = await serializer.serialize(
        SpeechOutputAudioRawFrame(audio=frame.audio, sample_rate=8000, num_channels=1)
    )
    payload = json.loads(outgoing)
    assert payload["streamSid"] == "fixture-stream"
    assert len(base64.b64decode(payload["media"]["payload"])) == 160
    # Carrier stop is followed by WebSocket disconnection; our transport
    # callback queues graceful EndFrame rather than cancellation.
    assert await serializer.deserialize('{"event":"stop"}') is None


async def test_pipecat_dial_uses_separate_media_origin_and_bounded_options(
    settings, service, brief
):
    settings.media_base_url = "https://media.example"
    settings.phone_number = "+12125550101"
    call_id = service.prepare("owner", brief)["call_id"]
    client = MagicMock()
    client.calls.create.return_value = SimpleNamespace(sid="CA" + "1" * 32)
    provider = PipecatTelephony(settings, service, client)
    assert await provider.dial(service.store.get("call", call_id)) == "CA" + "1" * 32
    options = client.calls.create.call_args.kwargs
    assert "wss://media.example/voice" in options["twiml"]
    assert call_id in options["twiml"]
    assert options["status_callback"].startswith("https://media.example/twilio/status/")
    assert options["record"] is False
    assert options["time_limit"] == brief.max_seconds


async def test_supervisor_delivery_failure_propagates(service, brief):
    call_id = service.prepare("owner", brief)["call_id"]
    llm = SupervisedLiveService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock(side_effect=ConnectionError("fixture failure")))
    with pytest.raises(ConnectionError):
        await llm.send_supervisor_event(
            events.SessionCommentaryAppendEvent(content="synthetic answer", delegation_id="fixture")
        )


async def test_live_transcript_fragments_do_not_authorize_hangup_before_turn_ends(
    service, brief, monkeypatch
):
    from pipecat.services.openai.live.llm import OpenAILiveLLMService

    call_id, _ = await started(service, brief)
    llm = SupervisedLiveService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    monkeypatch.setattr(OpenAILiveLLMService, "_handle_server_event", AsyncMock())

    async def fragment(role, text):
        await llm._handle_server_event(
            events.TranscriptDeltaEvent(type=f"session.{role}_transcript.delta", delta=text)
        )

    await fragment("input", "Please hang up")
    assert not llm.stop_requested
    await fragment("input", " if that is all you need.")
    await fragment("output", "Main menu.")
    assert not llm.stop_requested
    service.end.assert_not_awaited()

    await fragment("input", "Please hang up now.")
    assert not llm.stop_requested
    await fragment("output", "Goodbye.")
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")


@pytest.mark.parametrize("holding_sentence", [True, False])
def test_voice_prompt_contains_only_approved_disclosed_facts(holding_sentence):
    call = {
        "brief": {
            "facts": [
                {"name": "public", "value": "fiction"},
                {"name": "private", "value": "do-not-disclose"},
            ],
            "disclosures": ["public"],
        }
    }
    prompt = call_instructions(call, holding_sentence=holding_sentence)
    assert "fiction" in prompt
    assert "do-not-disclose" not in prompt
