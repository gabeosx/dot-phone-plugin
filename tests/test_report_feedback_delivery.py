"""Internal report feedback must not wait for a quiet acoustic turn."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from tests.test_pipecat_realtime import pending_voice


async def pending_report(service, brief):
    cid, llm, params, lookup, pending = await pending_voice(service, brief)
    llm.finalizing_calls.add(params.tool_call_id)
    llm.get_llm_adapter = lambda: SimpleNamespace(
        get_llm_invocation_params=lambda _: {"tools": [{"name": "finish_call"}]}
    )
    return cid, llm, params, lookup, pending


async def close_fixture(llm, lookup):
    tasks = [lookup, llm.source_continuation_task, llm.stop_task]
    for task in tasks:
        if task and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)


async def acknowledge(llm):
    await llm.observe(
        {
            "type": "conversation.item.added",
            "item": {"type": "function_call_output", "call_id": "tool_fixture"},
        }
    )


REJECTED = json.dumps({"result_recorded": False, "guidance": "Clarify the cancellation fee."})


@pytest.mark.parametrize("busy", ["caller", "active_response", "requested_response", "playback"])
async def test_rejected_report_is_delivered_during_speech_without_taking_the_floor(
    service, brief, busy
):
    cid, llm, params, lookup, _ = await pending_report(service, brief)
    if busy == "caller":
        llm.caller_speaking = True
    elif busy == "active_response":
        llm.active_responses.add("current-reply")
    elif busy == "requested_response":
        llm.pending_response_requests = 1
    else:
        llm._current_audio_response = object()
    try:
        await asyncio.wait_for(
            llm.deliver_decision(service.call("owner", cid), "tool_fixture", REJECTED), 0.2
        )
        await lookup
        llm._websocket.send.assert_awaited_once()
        wire = json.loads(llm._websocket.send.call_args.args[0])
        assert wire["item"]["call_id"] == "tool_fixture"
        assert json.loads(wire["item"]["output"])["supervisor_answer"] == REJECTED
        assert params.result_callback.call_args.kwargs["properties"].run_llm is False
        assert llm.report_rejections == 1
        llm.clear_playback.assert_not_awaited()
        llm._truncate_current_audio_response.assert_not_awaited()
        await acknowledge(llm)
        await asyncio.sleep(0.03)
        llm._create_response.assert_not_awaited()
    finally:
        await close_fixture(llm, lookup)


async def test_rejected_report_waits_for_provider_ack_then_requests_one_safe_response(
    service, brief
):
    cid, llm, _, lookup, _ = await pending_report(service, brief)
    try:
        await llm.deliver_decision(service.call("owner", cid), "tool_fixture", REJECTED)
        await lookup
        await asyncio.sleep(0.03)
        llm._create_response.assert_not_awaited()
        await acknowledge(llm)
        await asyncio.wait_for(llm.source_continuation_task, 0.3)
        llm._create_response.assert_awaited_once()
        assert llm.source_continuations == {}
        assert llm.report_rejections == 1
    finally:
        await close_fixture(llm, lookup)


async def test_native_response_consumes_feedback_without_duplicate_retry(service, brief):
    cid, llm, _, lookup, _ = await pending_report(service, brief)
    llm.caller_speaking = True
    try:
        await llm.deliver_decision(service.call("owner", cid), "tool_fixture", REJECTED)
        await lookup
        await acknowledge(llm)
        await llm.observe({"type": "response.created", "response": {"id": "native-answer"}})
        llm.caller_speaking = False
        await asyncio.wait_for(llm.source_continuation_task, 0.3)
        assert llm.source_continuations == {}
        assert llm.report_rejections == 1
        llm._create_response.assert_not_awaited()
    finally:
        await close_fixture(llm, lookup)


async def test_rejected_report_send_failure_does_not_count_or_resolve_decision(service, brief):
    cid, llm, params, lookup, _ = await pending_report(service, brief)
    llm._websocket.send.side_effect = ConnectionError("offline failure")
    try:
        with pytest.raises(ConnectionError):
            await llm.deliver_decision(service.call("owner", cid), "tool_fixture", REJECTED)
        assert service.call("owner", cid)["state"] == "awaiting_decision"
        assert "tool_fixture" not in llm._completed_tool_calls
        assert not lookup.done()
        assert not llm.source_continuations
        assert llm.report_rejections == 0
        params.result_callback.assert_not_awaited()
    finally:
        await close_fixture(llm, lookup)


async def test_repeated_rejection_feedback_during_speech_keeps_call_open(service, brief):
    cid, llm, _, lookup, _ = await pending_report(service, brief)
    llm.caller_speaking = True
    llm.report_rejections = 2
    service.end = AsyncMock()
    try:
        await llm.deliver_decision(service.call("owner", cid), "tool_fixture", REJECTED)
        await lookup
        assert llm.report_rejections == 3
        assert not llm.stop_requested and llm.stop_task is None
        service.end.assert_not_awaited()
        llm._create_response.assert_not_awaited()
    finally:
        await close_fixture(llm, lookup)


async def test_verified_completion_retains_existing_quiet_and_playback_checks(service, brief):
    cid, llm, _, lookup, _ = await pending_report(service, brief)
    llm.caller_speaking = True
    delivery = asyncio.create_task(
        llm.deliver_decision(
            service.call("owner", cid), "tool_fixture", json.dumps({"result_recorded": True})
        )
    )
    try:
        await asyncio.sleep(0.03)
        assert not delivery.done()
        llm._websocket.send.assert_not_awaited()
        llm.caller_speaking = False
        await asyncio.wait_for(delivery, 0.3)
        await lookup
        llm.clear_playback.assert_awaited_once_with("verified_result")
        llm._create_response.assert_not_awaited()
    finally:
        if not delivery.done():
            delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        await close_fixture(llm, lookup)
