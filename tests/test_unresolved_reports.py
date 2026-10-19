import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from dots_phone.call_task import task_conclusion, task_tools
from dots_phone.models import TaskReport
from dots_phone.report_review import new_review_state
from dots_phone.service import Refused
from tests.test_report_delivery import new_observation, reviewed_pending
from tests.test_report_review import Service, evaluate, observation, proposal, validate

# Authored uncertainty proposal with two conflicting fictional values.
SYNTHETIC_UNRESOLVED = {
    "outcome": "unresolved",
    "evidence": [
        {
            "criterion_id": "balance",
            "text": "Available rewards balance could not be confirmed due to conflicting reported values of 2,100 and 2,200 points.",
            "quote": None,
        }
    ],
    "unresolved_questions": [
        "Which value is the correct available rewards balance: 2,100 points or 2,200 points?"
    ],
}


def test_observed_uncited_uncertainty_becomes_question_not_confirmed_evidence():
    report = TaskReport.model_validate(SYNTHETIC_UNRESOLVED)
    assert report.evidence == []
    assert report.unresolved_questions == SYNTHETIC_UNRESOLVED["unresolved_questions"]
    # Normalization must not mutate the caller's original arguments/audit.
    assert SYNTHETIC_UNRESOLVED["evidence"][0]["quote"] is None
    call = {"brief": {"completion_criteria": [{"id": "balance", "required": True}]}}
    conclusion = task_conclusion(call, report.model_dump(exclude_none=True), [])
    assert conclusion.outcome == "unresolved" and not conclusion.confirmed_facts
    assert conclusion.unresolved_questions == report.unresolved_questions


@pytest.mark.parametrize("outcome", ["completed", "declined"])
@pytest.mark.parametrize("evidence", [[], SYNTHETIC_UNRESOLVED["evidence"]])
def test_success_and_decline_still_require_cited_evidence(outcome, evidence):
    with pytest.raises(ValidationError):
        TaskReport.model_validate(
            {**SYNTHETIC_UNRESOLVED, "outcome": outcome, "evidence": evidence}
        )


@pytest.mark.parametrize("questions", [[], [""], ["   "], ["x" * 241], ["Why?"] * 9])
@pytest.mark.parametrize("evidence", [[], SYNTHETIC_UNRESOLVED["evidence"]])
def test_empty_or_uncited_unresolved_needs_bounded_meaningful_uncertainty(questions, evidence):
    with pytest.raises(ValidationError):
        TaskReport.model_validate(
            {"outcome": "unresolved", "evidence": evidence, "unresolved_questions": questions}
        )


def test_unresolved_does_not_drop_invalid_or_unobserved_citations():
    for citation in ({"quote": ""}, {"evidence_id": "not-a-real-id"}, {"unexpected": True}):
        report = copy.deepcopy(SYNTHETIC_UNRESOLVED)
        report["evidence"][0].update(citation)
        with pytest.raises(ValidationError):
            TaskReport.model_validate(report)
    report = copy.deepcopy(SYNTHETIC_UNRESOLVED)
    report["evidence"] *= 9
    with pytest.raises(ValidationError):
        TaskReport.model_validate(report)
    report["evidence"] = [{"criterion_id": "balance", "text": "Confirmed.", "quote": "Invented."}]
    call = {"brief": {"completion_criteria": [{"id": "balance", "required": True}]}}
    with pytest.raises(Refused, match="observed recipient"):
        task_conclusion(call, report, [])


async def test_synthetic_tool_arguments_record_honest_unresolved_through_production(
    service, brief, tmp_path
):
    pytest.importorskip("pipecat")
    cid, _, original = await reviewed_pending(service, brief, tmp_path)
    call = service.call("owner", cid)
    call["report_review_state"] = {**new_review_state(call, original), "conflict": True}
    service.save(call)
    service.local_context_worker.reasoner.advise = AsyncMock(
        side_effect=AssertionError("No new model call needed")
    )

    async def supervise(params, command):
        pending = service.pending_decision(cid, params.tool_call_id, command)
        await service.local_context_worker.answer(service.call("owner", cid), pending)

    llm = SimpleNamespace(
        supervisor=service,
        call_id=cid,
        dialog=service.transcripts[cid],
        request_supervision=supervise,
        return_tool_feedback=AsyncMock(),
        finalizing_calls=set(),
        request_stop=Mock(),
    )
    tool = next(tool for tool in task_tools(llm) if getattr(tool, "name", None) == "finish_call")
    assert "Use evidence []" in tool.description
    params = SimpleNamespace(
        arguments=copy.deepcopy(SYNTHETIC_UNRESOLVED), tool_call_id="unresolved-tool"
    )
    await tool.handler(params)
    llm.return_tool_feedback.assert_not_awaited()
    llm.request_stop.assert_called_once_with(after_result=True)
    service.local_context_worker.reasoner.advise.assert_not_awaited()
    call = service.call("owner", cid)
    assert call["conclusion"]["outcome"] == "unresolved"
    assert call["conclusion"]["confirmed_facts"] == []
    assert (
        SYNTHETIC_UNRESOLVED["unresolved_questions"][0]
        in call["conclusion"]["unresolved_questions"]
    )
    service.terminal(call, "completed", "completed")
    result = service.result("owner", cid)
    assert result["outcome"] == "unresolved" and result["confirmed_facts"] == []
    assert SYNTHETIC_UNRESOLVED["unresolved_questions"][0] in result["unresolved_questions"]


@pytest.mark.parametrize("scope_change", [None, "revision", "brief_hash"])
async def test_terminal_result_retains_only_current_scope_review_uncertainty(
    service, brief, tmp_path, scope_change
):
    cid, _, original = await reviewed_pending(service, brief, tmp_path)
    call = service.call("owner", cid)
    state = {**new_review_state(call, original), "conflict": True}
    if scope_change:
        state[scope_change] = "another-scope"
    call["report_review_state"] = state
    service.save(call)
    service.terminal(call, "failed", "result_not_verified")
    result = service.result("owner", cid)
    assert result["outcome"] == "unresolved" and not result["confirmed_facts"]
    assert result["termination_reason"] == "result_not_verified"
    assert result["unresolved_questions"][0] == "result_not_verified"
    if scope_change:
        assert len(result["unresolved_questions"]) == 1
    else:
        assert any("Hear available balance" in q for q in result["unresolved_questions"])
        assert any("unconfirmed" in q for q in result["unresolved_questions"])
        assert all("2,200" not in q for q in result["unresolved_questions"])


@pytest.mark.parametrize("change", ["asr", "revision", "pending"])
async def test_unresolved_partial_evidence_rechecks_recipient_and_scope_during_validation(change):
    entries = [observation("Value is 180."), observation("Date is October 14.")]
    service = Service(entries)
    service.value["report_review_state"] = {
        **new_review_state(service.value, proposal()),
        "conflict": True,
    }
    report = {
        "outcome": "unresolved",
        "evidence": [
            {"criterion_id": "date", "text": "Date is October 14.", "quote": "Date is October 14."}
        ],
        "unresolved_questions": ["What is the exact value?"],
    }

    async def interrupted(call, report, pending):
        result = await validate(service, call, report, pending)
        if change == "asr":
            service.transcripts["call"][-1]["text"] = "Date is October 15."
        elif change == "revision":
            service.value["revision"] += 1
        else:
            service.value["pending"]["request_id"] = "another"
        return result

    reasoner = SimpleNamespace(advise=AsyncMock())
    if change == "asr":
        feedback, conclusion = await evaluate(service, reasoner, report, interrupted)
        assert not feedback["result_recorded"] and conclusion is None
    else:
        with pytest.raises(Refused, match="stale"):
            await evaluate(service, reasoner, report, interrupted)
    reasoner.advise.assert_not_awaited()


async def test_unresolved_delivery_remains_bound_to_recipient_snapshot(service, brief, tmp_path):
    cid, _, original = await reviewed_pending(service, brief, tmp_path)
    call = service.call("owner", cid)
    call["report_review_state"] = {**new_review_state(call, original), "conflict": True}
    service.save(call)
    pending = service.pending_decision(
        cid,
        "unresolved",
        json.dumps({"operation": "complete", "arguments": {"report": SYNTHETIC_UNRESOLVED}}),
    )

    async def interrupted(call, delegation, answer):
        assert json.loads(answer)["outcome"] == "unresolved"
        new_observation(service, cid, "asr")

    service.provider.update = AsyncMock(side_effect=interrupted)
    with pytest.raises(Refused, match="recipient evidence changed"):
        await service.local_context_worker.answer(service.call("owner", cid), pending)
    assert not service.call("owner", cid).get("conclusion")
