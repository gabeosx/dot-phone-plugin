"""Pending-source races through the production response and audio boundaries."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from dots_phone.call_task import task_tools
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from tests.conftest import started
from tests.test_realtime_interruption_ordering import assert_waveform
from tests.test_realtime_interruption_ordering import (
    audio_pipeline as audio_pipeline,
)
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_playout import eventually


async def direct_voice(service, brief, monkeypatch, *, human_stop=False):
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    brief = brief.model_copy(update={"stop_on_human": human_stop})
    cid, _ = await started(service, brief)
    call = service.call("owner", cid)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    service.local_context_worker = SimpleNamespace(scope=lambda call: {}, directory=None)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.prepare_listen_first(
        LLMContext([], tools=task_tools(llm, reasoner=True, source_tools=False))
    )
    llm._websocket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    forwarded = AsyncMock()
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", forwarded)
    return llm, forwarded


async def audio(llm, response_id, text):
    await llm._handle_evt_audio_delta(
        SimpleNamespace(
            response_id=response_id,
            item_id=response_id + "_speech",
            delta=base64.b64encode(b"\1\0" * 80).decode(),
        )
    )
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": response_id,
            "item_id": response_id + "_speech",
            "transcript": text,
        }
    )


async def test_pending_answer_stays_muted_but_a_new_known_question_can_be_answered(
    service, brief, monkeypatch
):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    try:
        await llm.lookup_wait.begin("zip_lookup")
        session = json.loads(llm._websocket.send.call_args.args[0])["session"]
        assert session["tool_choice"] == "auto"
        names = {t["name"] for t in session["tools"]}
        assert {"hang_up", "wait_for_recipient", "consult_call_reasoner"} <= names
        assert "finish_call" not in names
        await llm.observe({"type": "response.created", "response": {"id": "repeated"}})
        await audio(llm, "repeated", "I don't have the ZIP. Can we verify another way?")
        forwarded.assert_not_awaited()
        assert llm.dialog == []

        await llm.observe({"type": "input_audio_buffer.speech_started"})
        await llm.observe({"type": "input_audio_buffer.speech_stopped"})
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "known_question",
                "transcript": "While we wait, what is the booking date?",
            }
        )
        await llm.observe({"type": "response.created", "response": {"id": "known_answer"}})
        await audio(llm, "known_answer", "November sixth.")
        forwarded.assert_awaited_once()
        assert llm.lookup_wait.pending == {"zip_lookup": False}
        assert llm.dialog[-1]["text"] == "November sixth."

        # The result has been sent; a network send alone is not acknowledgement.
        llm.lookup_wait.sent("zip_lookup")
        llm.source_continuations["zip_lookup"] = {"request_id": "decision", "acknowledged": False}
        await llm.lookup_wait.finish("zip_lookup")
        assert llm.lookup_wait.pending
        await llm.observe(
            {
                "type": "conversation.item.added",
                "item": {"type": "function_call_output", "call_id": "zip_lookup"},
            }
        )
        assert not llm.lookup_wait.pending
        # Late audio from the blocked response stays blocked after the result.
        await audio(llm, "repeated", "I still don't have the ZIP.")
        forwarded.assert_awaited_once()
        await llm.observe({"type": "response.created", "response": {"id": "verified"}})
        await audio(llm, "verified", "0 2 1 3 9")
        assert forwarded.await_count == 2
        assert llm.dialog[-1]["text"] == "0 2 1 3 9"
        await llm.send_client_event(events.ResponseCreateEvent())
        assert (
            json.loads(llm._websocket.send.call_args.args[0])["response"]["tool_choice"] == "auto"
        )
    finally:
        await llm.lookup_wait.close()


async def test_old_response_cannot_leak_audio_when_lookup_starts_mid_response(
    service, brief, monkeypatch
):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    try:
        await llm.observe({"type": "response.created", "response": {"id": "old"}})
        await llm.lookup_wait.begin("lookup")
        # Even if the first old audio arrives only after the lookup resolves,
        # it was generated without that result and must not become current.
        await llm.lookup_wait.acknowledged("lookup")
        await audio(llm, "old", "An unsupported answer.")
        forwarded.assert_not_awaited()
        assert llm.dialog == []
    finally:
        await llm.lookup_wait.close()


async def test_repeated_waits_do_not_cancel_or_restart_the_original_lookup(
    service, brief, monkeypatch
):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    started_lookup, resolve_lookup = asyncio.Event(), asyncio.Event()

    async def pending_lookup(*args):
        started_lookup.set()
        await resolve_lookup.wait()

    llm._request_supervision = AsyncMock(side_effect=pending_lookup)
    request = asyncio.create_task(
        llm.request_supervision(
            SimpleNamespace(tool_call_id="original_lookup"),
            '{"operation":"reason"}',
            defer_source_speech=True,
        )
    )
    try:
        await asyncio.wait_for(started_lookup.wait(), 1)
        wait = next(
            t for t in task_tools(llm) if getattr(t, "__name__", None) == "wait_for_recipient"
        )
        for turn in range(2):
            await llm.observe({"type": "input_audio_buffer.speech_started"})
            await llm.observe({"type": "input_audio_buffer.speech_stopped"})
            callback = AsyncMock()
            await wait(
                SimpleNamespace(arguments={}, tool_call_id=f"wait_{turn}", result_callback=callback)
            )
            assert callback.call_args.args[0] == {"listening": True}
            assert llm.lookup_wait.pending == {"original_lookup": False}
            assert not request.done() and llm.lookup_lock.locked()
        llm._request_supervision.assert_awaited_once()
        forwarded.assert_not_awaited()
        resolve_lookup.set()
        await request
        assert not llm.lookup_wait.pending and not llm.lookup_lock.locked()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await llm.lookup_wait.close()


async def test_legacy_consultation_mode_retains_its_pending_gate(service, brief, monkeypatch):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    call = service.call("owner", llm.call_id)
    call["reasoner_response_policy"] = "always_consult"
    service.save(call)
    try:
        await llm.lookup_wait.begin("legacy_lookup")
        session = json.loads(llm._websocket.send.call_args.args[0])["session"]
        assert session["tool_choice"] == "required"
        assert {t["name"] for t in session["tools"]} == {"hang_up", "wait_for_recipient"}
        await llm.observe({"type": "input_audio_buffer.speech_started"})
        await llm.observe({"type": "input_audio_buffer.speech_stopped"})
        await llm.observe({"type": "response.created", "response": {"id": "legacy_new_turn"}})
        await audio(llm, "legacy_new_turn", "A gated answer.")
        forwarded.assert_not_awaited()
    finally:
        await llm.lookup_wait.close()


async def test_one_context_free_holding_phrase_and_new_turn_revokes_its_audio(
    service, brief, monkeypatch
):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    try:
        await llm.lookup_wait.begin("lookup")
        await llm.lookup_wait.task
        messages = [json.loads(c.args[0]) for c in llm._websocket.send.call_args_list]
        holds = [m for m in messages if m["type"] == "response.create"]
        assert len(holds) == 1
        response = holds[0]["response"]
        assert response["conversation"] == "none" and response["input"] == []
        assert response["tools"] == [] and response["tool_choice"] == "none"
        assert brief.facts[0].value not in response["instructions"]
        await llm.observe(
            {
                "type": "response.created",
                "response": {"id": "hold", "metadata": response["metadata"]},
            }
        )
        assert "hold" not in llm.reasoner_blocked_responses, (
            llm.lookup_wait.holding_responses,
            llm.lookup_wait.tokens,
            llm.stop_requested,
            service.call("owner", llm.call_id)["state"],
        )
        phrase = llm.lookup_wait.last_holding_phrase
        assert phrase and json.dumps(phrase) in response["instructions"]
        assert "check" not in phrase.lower() and "look" not in phrase.lower()
        await audio(llm, "hold", phrase)
        forwarded.assert_awaited_once()
        await llm.observe({"type": "input_audio_buffer.speech_started"})
        await audio(llm, "hold", "A late holding phrase.")
        forwarded.assert_awaited_once()
        assert llm.dialog[-1]["text"] == phrase
    finally:
        await llm.lookup_wait.close()


async def test_stop_on_human_stays_available_and_silent_during_lookup(service, brief, monkeypatch):
    brief = brief.model_copy(update={"stop_conditions": ["Verification rejected"]})
    llm, forwarded = await direct_voice(service, brief, monkeypatch, human_stop=True)
    try:
        await llm.lookup_wait.begin("lookup")
        session = json.loads(llm._websocket.send.call_args.args[0])["session"]
        assert {"hang_up", "end_test_on_human", "end_test_at_stop_condition"} <= {
            t["name"] for t in session["tools"]
        }
        tools = task_tools(llm, reasoner=True, source_tools=False)
        tool = next(t for t in tools if getattr(t, "name", None) == "end_test_on_human")
        await tool.handler(
            SimpleNamespace(
                arguments={
                    "evidence": "This is Avery. How can I help?",
                    "reason": "Live agent greeting",
                },
                result_callback=AsyncMock(),
                tool_call_id="stop",
            )
        )
        await llm.stop_task
        assert service.call("owner", llm.call_id)["state"] == "completed"
        forwarded.assert_not_awaited()
    finally:
        await llm.lookup_wait.close()


@pytest.mark.parametrize("change", ["result_arrived", "new_recipient_turn"])
async def test_late_holding_response_cannot_start_after_its_scope_ends(
    service, brief, monkeypatch, change
):
    llm, forwarded = await direct_voice(service, brief, monkeypatch)
    llm.lookup_wait.pending["zip"] = True
    llm.lookup_wait.tokens["issued_hold"] = llm.reasoner_turn
    if change == "result_arrived":
        await llm.lookup_wait.acknowledged("zip")
    else:
        await llm.observe({"type": "input_audio_buffer.speech_started"})
    await llm.observe(
        {
            "type": "response.created",
            "response": {"id": "late_hold", "metadata": {"lookup_hold": "issued_hold"}},
        }
    )
    await audio(llm, "late_hold", "One sec, let me check.")
    forwarded.assert_not_awaited()
    assert llm.dialog == []


async def test_lookup_failure_releases_gate_and_cancellation_does_not_leave_it_stuck(
    service, brief, monkeypatch
):
    llm, _ = await direct_voice(service, brief, monkeypatch)
    llm._request_supervision = AsyncMock(side_effect=TimeoutError)
    params = SimpleNamespace(tool_call_id="lookup")
    try:
        with pytest.raises(TimeoutError):
            await llm.request_supervision(
                params, '{"operation":"reason"}', defer_source_speech=True
            )
        assert not llm.lookup_wait.pending and not llm.lookup_lock.locked()
        llm._request_supervision = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await llm.request_supervision(
                params, '{"operation":"reason"}', defer_source_speech=True
            )
        assert not llm.lookup_wait.pending and not llm.lookup_lock.locked()
    finally:
        await llm.lookup_wait.close()


async def test_keypad_result_ack_releases_gate_without_a_speaking_continuation(
    service, brief, monkeypatch
):
    llm, _ = await direct_voice(service, brief, monkeypatch)
    try:
        await llm.lookup_wait.begin("keypad_advice")
        llm.lookup_wait.sent("keypad_advice")
        assert llm.source_continuations == {}
        await llm.observe(
            {
                "type": "conversation.item.added",
                "item": {"type": "function_call_output", "call_id": "keypad_advice"},
            }
        )
        assert not llm.lookup_wait.pending
        assert llm.source_continuations == {}
    finally:
        await llm.lookup_wait.close()


async def test_premature_finish_during_lookup_is_rejected_without_another_decision(
    service, brief, monkeypatch
):
    llm, _ = await direct_voice(service, brief, monkeypatch)
    llm.lookup_wait.pending["zip"] = False
    llm.request_supervision = AsyncMock()
    tool = next(
        t for t in task_tools(llm, reasoner=True) if getattr(t, "name", None) == "finish_call"
    )
    callback = AsyncMock()
    await tool.handler(
        SimpleNamespace(
            arguments={
                "outcome": "unresolved",
                "evidence": [],
                "unresolved_questions": ["ZIP is missing"],
            },
            tool_call_id="finish",
            result_callback=callback,
        )
    )
    assert callback.call_args.args[0] == {"status": "pending", "result_recorded": False}
    llm.request_supervision.assert_not_awaited()


async def test_actual_audio_pipeline_blocks_pending_answer_but_resumes_after_ack(audio_pipeline):
    p = audio_pipeline
    await p.llm.lookup_wait.begin("zip")
    # Actual SDK events traverse the codec, transport and carrier boundaries.
    p.model.response("premature", milliseconds=200)
    await eventually(lambda: "premature" in p.llm.response_usage)
    assert p.carrier.audio_bytes == 0
    p.llm.lookup_wait.sent("zip")
    p.llm.source_continuations["zip"] = {"request_id": "pending", "acknowledged": False}
    p.model.emit(
        {
            "type": "conversation.item.added",
            "event_id": "source_ack",
            "item": {
                "id": "source",
                "type": "function_call_output",
                "call_id": "zip",
                "output": "verified",
            },
        }
    )
    await eventually(lambda: not p.llm.lookup_wait.pending)
    p.model.response("answer", milliseconds=200)
    await eventually(lambda: p.carrier.audio_bytes > 0)


async def test_actual_audio_pipeline_allows_a_new_known_answer_while_lookup_stays_pending(
    audio_pipeline,
):
    p = audio_pipeline
    service = p.llm.supervisor
    call = service.call("owner", p.call_id)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    service.local_context_worker = SimpleNamespace(scope=lambda call: {}, directory=None)
    p.llm._context.set_tools(task_tools(p.llm, reasoner=True, source_tools=False))
    await p.llm.lookup_wait.begin("unknown_zip")
    p.model.response("obsolete_answer", milliseconds=200, frequency=400)
    await eventually(lambda: "obsolete_answer" in p.llm.response_usage)
    assert p.carrier.audio_bytes == 0

    # The recipient now asks about the already disclosed booking date instead.
    p.model.speech(started=True, turn=1)
    p.model.speech(started=False, turn=1)
    p.model.emit(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "recipient_1",
            "content_index": 0,
            "transcript": "While we wait, what is the booking date?",
        }
    )
    p.model.response("known_date_answer", milliseconds=400, frequency=700)
    await eventually(lambda: p.carrier.audio_bytes >= 3200)
    await eventually(lambda: p.llm._current_audio_response is None)
    assert p.llm.lookup_wait.pending == {"unknown_zip": False}
    assert_waveform(
        p.carrier.audio_after(p.carrier.clear_events[-1].event_index),
        milliseconds=400,
        frequency=700,
        absent_frequency=400,
    )

    # An old response remains obsolete both during and after source delivery.
    for resolved in (False, True):
        if resolved:
            await p.llm.lookup_wait.acknowledged("unknown_zip")
        before = p.llm.speech_delivery["model_output_audio_events"]
        p.model.emit(
            {
                "type": "response.output_audio.delta",
                "response_id": "obsolete_answer",
                "item_id": "obsolete_answer_item",
                "output_index": 0,
                "content_index": 0,
                "delta": base64.b64encode(b"\0\x10" * 2400).decode(),
            }
        )
        await eventually(
            lambda before=before: p.llm.speech_delivery["model_output_audio_events"] > before
        )
        assert p.carrier.audio_bytes == 3200
