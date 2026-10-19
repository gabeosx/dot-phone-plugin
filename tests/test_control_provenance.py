"""Structural action provenance through delayed native tools and carrier waits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.keypad_tools import keypad_tools
from dots_phone.models import Start, Steer
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.pipecat_voice import PipecatTelephony
from tests.conftest import subscribed
from tests.test_account_task import account_call


async def control_voice(service, brief):
    fact = brief.facts[0].model_copy(update={"name": "member", "value": "004217"})
    brief = brief.model_copy(update={"facts": [fact], "disclosures": ["member"]})
    prepared, worker = account_call(service, brief)
    worker.directory = None
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.settings.pipecat_account_dtmf = True
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.clear_playback = AsyncMock()
    llm.return_local_tool_result = AsyncMock()
    session = SimpleNamespace(
        carrier=SimpleNamespace(
            begin_change=lambda: "offline-only-nonce",
            wait_change=AsyncMock(),
            close=AsyncMock(),
            stream_count=2,
        ),
        ending=False,
        llm=llm,
        menu_lock=asyncio.Lock(),
        used_menus=set(),
    )
    client = MagicMock()
    client.calls.return_value.update.return_value = SimpleNamespace(sid=call["provider_sid"])
    telephony = PipecatTelephony(service.settings, service, client)
    telephony.sessions[call["provider_sid"]] = session
    tools = {tool.__name__: tool for tool in keypad_tools(telephony, llm, call, account=True)}
    await recipient(llm, "one", "For balance press one, or enter your member number.")
    return cid, llm, session, client, telephony, tools


async def recipient(llm, item, text):
    await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": item})
    await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": item})
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item,
            "transcript": text,
        }
    )


async def response(llm, response_id):
    await llm.observe({"type": "response.created", "response": {"id": response_id}})


async def function_event(llm, response_id, tool_id, *, kind="response.output_item.added"):
    event = {
        "type": kind,
        "response_id": response_id,
        "item": {"type": "function_call", "call_id": tool_id},
    }
    if kind == "response.function_call_arguments.done":
        event.update(call_id=tool_id, arguments="{}")
    await llm.observe(event)


def tool_params(tool_id):
    return SimpleNamespace(tool_call_id=tool_id, result_callback=AsyncMock())


async def invoke(tools, operation, params, *, corrected=False):
    if operation == "menu":
        await tools["press_phone_key"](params, "2" if corrected else "1")
    else:
        await tools["enter_verification_fact"](params, "member", terminator="#")


@pytest.mark.parametrize("operation", ["menu", "verification"])
@pytest.mark.parametrize("wait_at", ["late_function_event", "menu_lock", "playback_clear"])
@pytest.mark.parametrize("change", ["new_input", "stop", "steer", "late_asr", "unchanged"])
async def test_action_keeps_origin_across_scheduling_and_only_submitted_actions_reserve_input(
    service, brief, operation, wait_at, change
):
    cid, llm, session, client, provider, tools = await control_voice(service, brief)
    await response(llm, "old_response")
    clear_entered, clear_release = asyncio.Event(), asyncio.Event()
    invocation = None
    if wait_at == "menu_lock":
        await session.menu_lock.acquire()
    if wait_at == "playback_clear":

        async def clear(*_):
            clear_entered.set()
            await clear_release.wait()

        llm.clear_playback.side_effect = clear
    if wait_at != "late_function_event":
        await function_event(llm, "old_response", "old_action")
        invocation = asyncio.create_task(invoke(tools, operation, tool_params("old_action")))
        if wait_at == "playback_clear":
            await asyncio.wait_for(clear_entered.wait(), 1)
        else:
            await asyncio.sleep(0)
            assert not invocation.done()

    if change == "new_input":
        await recipient(llm, "two", "Correction: for balance press two. Use the same member ID.")
    elif change == "stop":
        # This immediate stop intent precedes asynchronous durable teardown.
        # Keep the call active to exercise exactly that scheduling window.
        llm.stop_requested = True
        assert service.call("owner", cid)["state"] == "active"
    elif change == "steer":
        await service.steer(
            "owner", Steer(call_id=cid, revision=1, instruction="Keep pursuing the approved goal.")
        )
    elif change == "late_asr":
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "one",
                "content_index": 1,
                "transcript": "The rest of the same recorded request.",
            }
        )

    if wait_at == "late_function_event":
        # Old arguments arriving late must retain the response's turn/revision.
        await function_event(
            llm, "old_response", "old_action", kind="response.function_call_arguments.done"
        )
        invocation = asyncio.create_task(invoke(tools, operation, tool_params("old_action")))
    elif wait_at == "menu_lock":
        session.menu_lock.release()
    else:
        clear_release.set()
    await asyncio.wait_for(invocation, 1)
    result = llm.return_local_tool_result.call_args.args[1]
    stale = change in {"new_input", "stop", "steer"}
    assert result["carrier_control_accepted"] is not stale
    assert client.calls.return_value.update.call_count == (0 if stale else 1)
    assert len(session.used_menus) == (0 if stale else 1)
    if stale:
        assert result["input_status"] == "not_submitted"
        assert not service.call("owner", cid).get("private_keypad_commands")
    else:
        command = next(iter(service.call("owner", cid)["private_keypad_commands"].values()))
        assert command["recipient_audio_turn"] == 1
        assert command["originating_response_id"] == "old_response"
        assert command["digits"] == ("1" if operation == "menu" else "004217#")

    if change in {"new_input", "steer"}:
        # A deliberate fresh native decision remains possible; the stale one
        # neither sent input nor spent this request's duplicate suppression.
        llm.clear_playback.side_effect = None
        await response(llm, "fresh_response")
        await function_event(llm, "fresh_response", "fresh_action")
        await invoke(tools, operation, tool_params("fresh_action"), corrected=True)
        assert llm.return_local_tool_result.call_args.args[1]["carrier_control_accepted"]
        assert client.calls.return_value.update.call_count == 1
        command = next(iter(service.call("owner", cid)["private_keypad_commands"].values()))
        assert command["digits"] == ("2" if operation == "menu" else "004217#")
        assert command["recipient_audio_turn"] == (2 if change == "new_input" else 1)


@pytest.mark.parametrize(
    "event_kind",
    [
        "response.output_item.added",
        "response.output_item.done",
        "response.function_call_arguments.done",
        "response.done",
    ],
)
async def test_function_origin_comes_from_original_response_even_when_first_function_event_is_late(
    service, brief, event_kind
):
    _, llm, _, client, _, tools = await control_voice(service, brief)
    await response(llm, "old")
    await recipient(llm, "two", "Choose two instead.")
    if event_kind == "response.done":
        await llm.observe(
            {
                "type": event_kind,
                "response": {"id": "old", "output": [{"type": "function_call", "call_id": "late"}]},
            }
        )
    else:
        await function_event(llm, "old", "late", kind=event_kind)
    assert llm.keypad_control_origin("late")["recipient_audio_turn"] == 1
    await tools["press_phone_key"](tool_params("late"), "1")
    assert not llm.return_local_tool_result.call_args.args[1]["carrier_control_accepted"]
    client.calls.return_value.update.assert_not_called()


async def test_missing_native_response_origin_requests_reconsideration_without_sending(
    service, brief
):
    _, llm, session, client, _, tools = await control_voice(service, brief)
    await tools["press_phone_key"](tool_params("unbound"), "1")
    result = llm.return_local_tool_result.call_args.args[1]
    assert not result["carrier_control_accepted"] and result["input_status"] == "not_submitted"
    assert "reconsider" in result["reason"]
    assert not session.used_menus
    client.calls.return_value.update.assert_not_called()


@pytest.mark.parametrize("operation", ["menu", "verification"])
async def test_actual_model_stop_prevents_submission_before_async_teardown_starts(
    service, brief, operation
):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    await response(llm, "response")
    await function_event(llm, "response", "action")

    async def stop_while_clearing(*_):
        assert llm.request_stop("The recipient has ended this conversation.")
        # request_stop schedules teardown separately; before the event loop
        # runs it, its immediate intent must already prevent new side effects.
        assert service.call("owner", cid)["state"] == "active"

    llm.clear_playback.side_effect = stop_while_clearing
    await invoke(tools, operation, tool_params("action"))
    assert not llm.return_local_tool_result.call_args.args[1]["carrier_control_accepted"]
    assert not session.used_menus
    client.calls.return_value.update.assert_not_called()
    await llm.stop_task
    assert service.result("owner", cid)["termination_reason"] == "recipient_stop"


@pytest.mark.parametrize("operation", ["menu", "verification"])
async def test_uncertain_carrier_submission_keeps_duplicate_guard_and_is_never_retried(
    service, brief, operation
):
    _, llm, session, client, _, tools = await control_voice(service, brief)
    await response(llm, "response")
    await function_event(llm, "response", "first_action")
    client.calls.return_value.update.side_effect = TimeoutError("offline uncertain carrier result")
    with pytest.raises(RuntimeError, match="no automatic retry"):
        await invoke(tools, operation, tool_params("first_action"))
    assert len(session.used_menus) == 1
    await function_event(llm, "response", "second_action")
    await invoke(tools, operation, tool_params("second_action"))
    result = llm.return_local_tool_result.call_args.args[1]
    assert not result["carrier_control_accepted"]
    assert result["input_status"] == "previous_attempt"
    assert client.calls.return_value.update.call_count == 1


@pytest.mark.parametrize("invalid", ["grant", "disclosure", "budget_limit"])
async def test_structural_flexibility_does_not_bypass_authority_or_data_limits(
    service, brief, invalid
):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    await response(llm, "allowed")
    await function_event(llm, "allowed", "action")
    fact_name = "member"
    if invalid == "grant":
        call = service.call("owner", cid)
        grant = service.store.get("approval", call["started_authorization"], "owner")
        grant["consumed"] = False
        service.store.put("approval", grant["id"], "owner", grant)
    elif invalid == "disclosure":
        fact_name = "undisclosed"
    else:
        service.settings.max_seconds = 1
    await tools["enter_verification_fact"](tool_params("action"), fact_name)
    assert not llm.return_local_tool_result.call_args.args[1]["carrier_control_accepted"]
    assert not session.used_menus
    client.calls.return_value.update.assert_not_called()
