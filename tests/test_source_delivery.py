"""Source acceptance must survive a long interruption in the original call."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.models import Decision
from dots_phone.pipecat_voice import PipecatTelephony
from dots_phone.service import Refused
from tests.test_pipecat_realtime import pending_voice


async def accept_source(service, llm, pending, text="Verified source from the original scope."):
    service.settings.pipecat_engine = "realtime"
    provider = PipecatTelephony(service.settings, service)
    provider.stop = AsyncMock()
    provider.sessions["CA" + "1" * 32] = SimpleNamespace(llm=llm)
    service.provider = provider
    await service.decision(
        "owner",
        Decision(
            call_id=llm.call_id,
            request_id=pending["request_id"],
            revision=1,
            answer=text,
            fact_names=["date"],
        ),
    )


async def acknowledge(llm, tool_id="tool_fixture"):
    await llm.observe(
        {
            "type": "conversation.item.added",
            "item": {"type": "function_call_output", "call_id": tool_id},
        }
    )


async def cleanup(llm, lookup):
    if llm.source_continuation_task:
        llm.source_continuation_task.cancel()
        await asyncio.gather(llm.source_continuation_task, return_exceptions=True)
    if not lookup.done():
        lookup.cancel()
    await asyncio.gather(lookup, return_exceptions=True)


async def test_ready_source_is_accepted_during_interruption_without_expiring_or_cutting_speech(
    service, brief
):
    cid, llm, params, lookup, pending = await pending_voice(service, brief, source=True)
    before = service.call("owner", cid)
    llm.caller_speaking = True
    llm.active_responses.add("independent_answer")
    llm._current_audio_response = object()
    try:
        await asyncio.wait_for(accept_source(service, llm, pending), 0.2)
        await asyncio.wait_for(lookup, 0.2)
        call = service.call("owner", cid)
        assert call["state"] == "active" and call["pending"] is None
        assert call["brief"] == before["brief"]
        assert call["brief_hash"] == before["brief_hash"]
        wire = json.loads(llm._websocket.send.call_args.args[0])
        assert wire["item"]["type"] == "function_call_output"
        assert "Verified source" in wire["item"]["output"]
        assert params.result_callback.call_args.kwargs["properties"].run_llm is False
        llm.clear_playback.assert_not_awaited()
        llm._truncate_current_audio_response.assert_not_awaited()
        llm._create_response.assert_not_awaited()
        await acknowledge(llm)
        # The retrieval deadline must not keep running after data was accepted.
        llm.source_continuations["tool_fixture"]["sent_at"] -= service.settings.decision_seconds + 1
        await llm.observe({"type": "input_audio_buffer.speech_stopped"})
        llm.active_responses.clear()
        llm._current_audio_response = None
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        llm._create_response.assert_awaited_once()
        llm._websocket.send.assert_awaited_once()
    finally:
        await cleanup(llm, lookup)


async def test_next_vad_response_uses_ready_source_without_duplicate_response(service, brief):
    _, llm, _, lookup, pending = await pending_voice(service, brief, source=True)
    llm.caller_speaking = True
    try:
        await accept_source(service, llm, pending)
        await lookup
        await acknowledge(llm)
        await llm.observe({"type": "input_audio_buffer.speech_stopped"})
        await llm.observe({"type": "response.created", "response": {"id": "natural_reply"}})
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        assert llm.answer_response_requests["natural_reply"] == pending["request_id"]
        llm._create_response.assert_not_awaited()
        assert not llm.source_continuations
    finally:
        await cleanup(llm, lookup)


@pytest.mark.parametrize("change", ["revision", "termination"])
async def test_ready_source_does_not_restart_speech_after_scope_or_call_changes(
    service, brief, change
):
    cid, llm, _, lookup, pending = await pending_voice(service, brief, source=True)
    llm.caller_speaking = True
    try:
        await accept_source(service, llm, pending)
        await lookup
        await acknowledge(llm)
        call = service.call("owner", cid)
        if change == "revision":
            call["revision"] += 1
        else:
            call["state"] = "completed"
        service.save(call)
        llm.caller_speaking = False
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        llm._create_response.assert_not_awaited()
    finally:
        await cleanup(llm, lookup)


async def test_ready_source_socket_failure_preserves_pending_request(service, brief):
    cid, llm, params, lookup, pending = await pending_voice(service, brief, source=True)
    llm.caller_speaking = True
    llm._websocket.send.side_effect = ConnectionError("offline socket failure")
    try:
        with pytest.raises(ConnectionError):
            await accept_source(service, llm, pending)
        assert service.call("owner", cid)["pending"]["request_id"] == pending["request_id"]
        assert not llm.source_continuations
        assert "tool_fixture" not in llm._completed_tool_calls
        params.result_callback.assert_not_awaited()
    finally:
        await cleanup(llm, lookup)


async def test_source_acceptance_still_rejects_expired_decisions(service, brief):
    cid, llm, params, lookup, pending = await pending_voice(service, brief, source=True)
    call = service.call("owner", cid)
    call["pending"]["expires"] = 0
    service.save(call)
    try:
        with pytest.raises(Refused, match="stale"):
            await accept_source(service, llm, pending)
        llm._websocket.send.assert_not_awaited()
        params.result_callback.assert_not_awaited()
    finally:
        await cleanup(llm, lookup)


async def test_two_sources_arriving_during_one_turn_need_only_one_spoken_followup(service, brief):
    _, llm, _, first, pending = await pending_voice(service, brief, source=True)
    llm.caller_speaking = True
    second = None
    try:
        await accept_source(service, llm, pending, "First source.")
        await first
        params = SimpleNamespace(tool_call_id="second_source", result_callback=AsyncMock())
        second = asyncio.create_task(
            llm.request_supervision(params, "Another missing fact", defer_source_speech=True)
        )
        await asyncio.sleep(0)
        pending = service.call("owner", llm.call_id)["pending"]
        await accept_source(service, llm, pending, "Second source.")
        await second
        await acknowledge(llm)
        await acknowledge(llm, "second_source")
        llm.caller_speaking = False
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        llm._websocket.send.assert_awaited()
        assert llm._websocket.send.await_count == 2
        llm._create_response.assert_awaited_once()
        assert not llm.source_continuations
    finally:
        await cleanup(llm, first)
        if second:
            if not second.done():
                second.cancel()
            await asyncio.gather(second, return_exceptions=True)


async def test_unacknowledged_source_ends_unresolved_without_retrying_the_call(service, brief):
    cid, llm, _, lookup, pending = await pending_voice(service, brief, source=True)
    try:
        await accept_source(service, llm, pending)
        await lookup
        llm.source_continuations["tool_fixture"]["sent_at"] -= service.settings.decision_seconds + 1
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        await llm.stop_task
        call = service.call("owner", cid)
        assert call["state"] == "failed"
        assert call["result"]["outcome"] == "unresolved"
        assert call["result"]["termination_reason"] == "source_continuation_failed"
        assert llm._websocket.send.await_count == 1
        llm._create_response.assert_not_awaited()
        service.provider.stop.assert_awaited_once()
    finally:
        await cleanup(llm, lookup)


@pytest.mark.parametrize("kind", ["late_holding", "context_free", "blocked"])
async def test_ineligible_response_cannot_consume_acknowledged_source_or_answer_request(
    service, brief, kind
):
    _, llm, _, lookup, pending = await pending_voice(service, brief, source=True)
    llm.caller_speaking = True
    try:
        await accept_source(service, llm, pending)
        await lookup
        await acknowledge(llm)
        llm.next_answer_request_id = pending["request_id"]
        response = {"id": "ineligible"}
        if kind == "late_holding":
            response["metadata"] = {"lookup_hold": "expired-context-free-token"}
        elif kind == "context_free":
            response["conversation_id"] = None
        else:
            llm.reasoner_blocked_responses.add("ineligible")
        await llm.observe({"type": "response.created", "response": response})
        assert llm.source_continuations["tool_fixture"]["acknowledged"] is True
        assert llm.next_answer_request_id == pending["request_id"]
        assert "ineligible" not in llm.answer_response_requests
        # The next ordinary response can use the already-delivered source.
        await llm.observe({"type": "response.created", "response": {"id": "normal_answer"}})
        assert not llm.source_continuations
        assert llm.next_answer_request_id is None
        assert llm.answer_response_requests["normal_answer"] == pending["request_id"]
        await asyncio.wait_for(llm.source_continuation_task, 0.5)
        llm._create_response.assert_not_awaited()
    finally:
        await cleanup(llm, lookup)
