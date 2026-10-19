"""Model-led closure honors judgment without promoting closure into task success."""

import asyncio

import pytest

pytest.importorskip("pipecat")

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from tests.conftest import started
from tests.test_realtime_farewell import hear
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)


async def model_voice(service, brief):
    call_id, _ = await started(service, brief)
    call = service.call("owner", call_id)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    return SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")


@pytest.mark.parametrize(
    "transcript,rationale",
    [
        (
            "Take care, and thanks for calling today.",
            "The recipient has ended the conversation.",
        ),
        (
            "The birthday you enter does not match the account.",
            "The recipient rejected the birthday entered; this test should stop here.",
        ),
        (
            "",
            "The recipient asked us to stop; the separate transcription missed it.",
        ),
    ],
)
async def test_model_judgment_closes_without_exact_words_or_transcript(
    service, brief, transcript, rationale
):
    llm = await model_voice(service, brief)
    if transcript:
        await hear(llm, transcript)
    before = service.call("owner", llm.call_id)
    binding = (before["brief_hash"], before["revision"], before["started_authorization"])
    assert llm.request_stop(rationale)
    await llm.stop_task
    result = service.result("owner", llm.call_id)
    assert result["termination_reason"] == "recipient_stop"
    assert result["outcome"] == "unresolved"
    assert result["confirmed_facts"] == []
    call = service.call("owner", llm.call_id)
    assert (call["brief_hash"], call["revision"], call["started_authorization"]) == binding
    audit = call["private_voice_stop_decisions"][-1]
    assert audit["rationale"] == rationale
    assert audit["brief_hash"] == binding[0] and audit["revision"] == binding[1]
    assert audit["dialog_context"] == llm.dialog[-8:]
    assert call["usage"]["voice_control_events"][-1]["source"] == "model_judgment"
    assert "rationale" not in call["usage"]["voice_control_events"][-1]
    assert len(service.provider.stops) == 1


@pytest.mark.parametrize(
    "transcript",
    [
        "Please hang up now.",
        "If you're finished, hang up. Otherwise say main menu.",
        "Goodbye. Actually wait, I have another question.",
    ],
)
async def test_raw_asr_does_not_choose_stop_for_model_led_call(service, brief, transcript):
    llm = await model_voice(service, brief)
    await hear(llm, transcript)
    assert not llm.stop_requested
    assert not llm.request_stop()
    await asyncio.sleep(0)
    assert service.provider.stops == []
    assert service.call("owner", llm.call_id)["state"] == "active"


@pytest.mark.parametrize("rationale", [None, "", " \n ", "x" * 401, 123])
async def test_model_stop_requires_bounded_nonempty_tool_intent(service, brief, rationale):
    llm = await model_voice(service, brief)
    await hear(llm, "Please hang up now.")
    assert not llm.request_stop(rationale)
    assert not llm.stop_requested
    assert not service.call("owner", llm.call_id).get("private_voice_stop_decisions")
    assert service.provider.stops == []


async def test_model_stop_audit_is_snapshot_and_second_stop_is_idempotent(service, brief):
    llm = await model_voice(service, brief)
    await hear(llm, "That is everything. Take care.")
    assert llm.request_stop("The recipient is done.")
    llm.dialog[-1]["text"] = "Mutated after the decision."
    await llm.stop_task
    assert llm.request_stop("Another decision.")
    call = service.call("owner", llm.call_id)
    assert len(call["private_voice_stop_decisions"]) == 1
    assert call["private_voice_stop_decisions"][0]["dialog_context"][-1]["text"] == (
        "That is everything. Take care."
    )
    assert len(service.provider.stops) == 1


async def test_already_closed_model_session_does_not_reopen_or_change_result(service, brief):
    llm = await model_voice(service, brief)
    await service.end("owner", llm.call_id)
    before = service.result("owner", llm.call_id)
    assert llm.request_stop("The recipient is done.")
    assert llm.stop_task is None
    assert service.result("owner", llm.call_id) == before
    assert len(service.provider.stops) == 1


async def test_model_result_close_still_requires_durable_conclusion(service, brief):
    llm = await model_voice(service, brief)
    assert not llm.request_stop(after_result=True)
    call = service.call("owner", llm.call_id)
    call["conclusion"] = {
        "outcome": "unresolved",
        "confirmed_facts": [],
        "commitments": [],
        "unresolved_questions": ["The task could not be completed."],
    }
    service.save(call)
    assert llm.request_stop(after_result=True)
    await llm.stop_task
    assert service.result("owner", llm.call_id)["termination_reason"] == "task_finished"
    assert not service.call("owner", llm.call_id).get("private_voice_stop_decisions")
