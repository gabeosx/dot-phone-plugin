"""Uncited native finish proposals reach grounded review after the line closes."""

import asyncio
import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

pytest.importorskip("pipecat")

from dots_phone.call_reasoner import CodexReasonerClient
from dots_phone.call_task import task_tools
from dots_phone.models import ProposedReport, TaskReport
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.post_call_review import review_input, reviewed_conclusion
from dots_phone.service import Refused
from tests.test_post_call_review import active


@pytest.fixture(autouse=True)
def deny_sockets(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Report contract tests must never connect to a provider")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def native():
    path = Path(__file__).parent / "fixtures/synthetic_uncited_report.json"
    authored = json.loads(path.read_text())
    assert authored["synthetic"] is True
    from dots_phone.service import digest

    authored["recipient_catalog"] = []
    for index, entry in enumerate(authored["recipient_entries"]):
        observation = {
            "text": entry["text"],
            "origin": "realtime_transcription",
            "observation_index": index,
            "sequence_basis": "transcription_completion_order",
            "recipient_turn": entry["recipient_turn"],
            "recipient_item_id": entry["recipient_item_id"],
        }
        authored["recipient_catalog"].append({"id": digest(observation)[:16], **observation})
    return authored


async def native_voice(service, brief, tmp_path, native):
    cid = await active(service, brief, tmp_path)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.dialog = copy.deepcopy(native["recipient_entries"])
    service.observe_transcript(cid, llm.dialog)
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm.request_supervision = AsyncMock(side_effect=AssertionError("No live report repair"))
    tool = next(
        t for t in task_tools(llm, reasoner=True) if getattr(t, "name", "") == "finish_call"
    )
    return cid, llm, tool


async def submit(tool, proposal):
    callback = AsyncMock()
    await tool.handler(
        SimpleNamespace(arguments=proposal, result_callback=callback, tool_call_id="native-final")
    )
    assert callback.call_args.kwargs["properties"].run_llm is False
    return callback


def verdict(context, native):
    expected_quote = native["recipient_entries"][-1]["text"]
    actual = next(e for e in context["recipient_evidence"] if e["text"] == expected_quote)
    return {
        "verdicts": [
            {
                "claim_index": 0,
                "status": "supported",
                "criterion_met": True,
                "evidence_id": actual["id"],
                "reason": "The actual recipient statement supports this complete original claim.",
            }
        ]
    }


async def test_synthetic_uncited_tool_proposal_is_reviewed_only_after_voice_closes(
    service, brief, tmp_path, monkeypatch, native
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    proposal = native["proposal"]
    assert "quote" not in proposal["evidence"][0]
    assert "evidence_id" not in proposal["evidence"][0]
    waiting, release = asyncio.Event(), asyncio.Event()

    async def review(context):
        assert len(service.provider.stops) == 1
        assert service.call("owner", cid)["state"] == "completed"
        assert service.call("owner", cid)["usage"]["voice_closed_confirmed"] is True
        assert context["proposed_report"] == proposal
        assert context["recipient_evidence"] == native["recipient_catalog"]
        waiting.set()
        await release.wait()
        return verdict(context, native)

    reviewer = AsyncMock(side_effect=review)
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    await submit(tool, proposal)
    await llm.stop_task
    llm.request_supervision.assert_not_awaited()
    assert llm.stop_requested and len(service.provider.stops) == 1
    call = service.call("owner", cid)
    assert call["final_report_review"]["report"] == proposal
    assert service.result("owner", cid)["outcome"] == "unresolved"
    service.post_call_reviews.dispatch()
    assert not service.post_call_reviews.tasks
    reviewer.assert_not_awaited()
    service.confirm_voice_closed(cid, 1000)
    service.post_call_reviews.dispatch()
    review_task = service.post_call_reviews.tasks[cid]
    await asyncio.wait_for(waiting.wait(), 1)
    assert service.result("owner", cid)["outcome"] == "unresolved"
    release.set()
    await asyncio.wait_for(review_task, 1)
    final = service.result("owner", cid)
    assert final["outcome"] == "completed"
    stored = service.call("owner", cid)
    assert stored["conclusion"]["confirmed_facts"][0]["text"] == proposal["evidence"][0]["text"]
    assert final["confirmed_facts"][0]["text"] == native["recipient_entries"][-1]["text"]
    assert final["confirmed_facts"][0]["quote"] == native["recipient_entries"][-1]["text"]
    assert final["report_review_status"] == "reviewed"
    assert final["termination_reason"] == "conversation_finished"
    assert len(service.provider.dials) == len(service.provider.stops) == 1
    reviewer.assert_awaited_once()


@pytest.mark.parametrize(
    "hints",
    [
        {},
        {"quote": None, "evidence_id": None},
        {"quote": "Wrong words.", "evidence_id": "f" * 16},
        {"quote": "", "evidence_id": ""},
        {"evidence_id": "not-a-catalog-id"},
    ],
)
async def test_autonomous_citation_hints_do_not_replace_actual_catalog_grounding(
    service, brief, tmp_path, monkeypatch, native, hints
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    proposal = copy.deepcopy(native["proposal"])
    proposal["evidence"][0].update(hints)
    expected = ProposedReport.model_validate(proposal).model_dump(mode="json", exclude_none=True)
    reviewer = AsyncMock(side_effect=lambda context: verdict(context, native))
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    await submit(tool, proposal)
    await llm.stop_task
    assert service.call("owner", cid)["final_report_review"]["report"] == expected
    service.confirm_voice_closed(cid, 1000)
    await service.post_call_reviews.run("owner", cid)
    final = service.result("owner", cid)
    assert final["outcome"] == "completed"
    stored = service.call("owner", cid)
    assert stored["conclusion"]["confirmed_facts"][0]["text"] == proposal["evidence"][0]["text"]
    assert final["confirmed_facts"][0]["text"] == native["recipient_entries"][-1]["text"]
    assert final["confirmed_facts"][0]["quote"] == native["recipient_entries"][-1]["text"]
    reviewer.assert_awaited_once()


@pytest.mark.parametrize(
    "claim,status",
    [
        ("Available rewards balance is 4,900 points.", "conflict"),
        ("The recipient waived all future fees.", "unverified"),
    ],
)
async def test_uncited_claims_remain_unresolved_when_closed_review_does_not_support_them(
    service, brief, tmp_path, monkeypatch, native, claim, status
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    proposal = copy.deepcopy(native["proposal"])
    proposal["evidence"][0]["text"] = claim
    reviewer = AsyncMock(
        return_value={
            "verdicts": [
                {
                    "claim_index": 0,
                    "status": status,
                    "criterion_met": False,
                    "evidence_id": "",
                    "reason": "The actual recipient observations do not establish this original claim.",
                }
            ]
        }
    )
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    await submit(tool, proposal)
    await llm.stop_task
    service.confirm_voice_closed(cid, 1000)
    await service.post_call_reviews.run("owner", cid)
    final = service.result("owner", cid)
    assert final["outcome"] == "unresolved" and final["confirmed_facts"] == []
    assert (
        service.call("owner", cid)["final_report_review"]["report"]["evidence"][0]["text"] == claim
    )
    assert len(service.provider.dials) == len(service.provider.stops) == 1


async def test_uncited_proposal_cannot_confirm_an_unapproved_criterion(
    service, brief, tmp_path, monkeypatch, native
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    proposal = copy.deepcopy(native["proposal"])
    proposal["evidence"][0]["criterion_id"] = "purchase_authorized"
    reviewer = AsyncMock(side_effect=AssertionError("No authority for this criterion"))
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    await submit(tool, proposal)
    await llm.stop_task
    service.confirm_voice_closed(cid, 1000)
    await service.post_call_reviews.run("owner", cid)
    final = service.result("owner", cid)
    assert final["outcome"] == "unresolved" and final["confirmed_facts"] == []
    assert "unapproved" in final["unresolved_questions"][0]
    reviewer.assert_not_awaited()


@pytest.mark.parametrize("change", ["foreign_id", "proposal", "scope", "missing_verdict"])
async def test_uncited_proposal_retains_review_binding_and_catalog_guards(
    service, brief, tmp_path, native, change
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    await submit(tool, native["proposal"])
    await llm.stop_task
    call = service.call("owner", cid)
    decision = verdict(review_input(call), native)
    if change == "foreign_id":
        decision["verdicts"][0]["evidence_id"] = "f" * 16
    elif change == "proposal":
        call["final_report_review"]["report"]["evidence"][0]["text"] = "Changed factual claim."
    elif change == "scope":
        call["revision"] += 1
    else:
        decision["verdicts"] = []
    with pytest.raises(Refused):
        reviewed_conclusion(call, decision)


@pytest.mark.parametrize("hints", [{}, {"quote": "Some words.", "evidence_id": "f" * 16}])
def test_legacy_completed_reports_still_require_exactly_one_citation(native, hints):
    proposal = copy.deepcopy(native["proposal"])
    proposal["evidence"][0].update(hints)
    ProposedReport.model_validate(proposal)
    with pytest.raises(ValidationError):
        TaskReport.model_validate(proposal)


@pytest.mark.parametrize("hints", [{}, {"quote": "Some words.", "evidence_id": "f" * 16}])
async def test_legacy_finish_tool_requests_feedback_without_closing_an_uncited_report(
    service, brief, tmp_path, native, hints
):
    cid, llm, _ = await native_voice(service, brief, tmp_path, native)
    llm.return_tool_feedback = AsyncMock()
    legacy_tool = next(
        t for t in task_tools(llm, reasoner=False) if getattr(t, "name", "") == "finish_call"
    )
    proposal = copy.deepcopy(native["proposal"])
    proposal["evidence"][0].update(hints)
    await legacy_tool.handler(
        SimpleNamespace(
            arguments=proposal, result_callback=AsyncMock(), tool_call_id="legacy-final"
        )
    )
    llm.return_tool_feedback.assert_awaited_once()
    llm.request_supervision.assert_not_awaited()
    assert not llm.stop_requested and not service.provider.stops
    assert not service.call("owner", cid).get("final_report_review")


@pytest.mark.parametrize(
    "corruption",
    [
        "empty_text",
        "long_text",
        "missing_criterion",
        "bad_outcome",
        "empty_completed",
        "empty_declined",
        "too_many",
    ],
)
async def test_broken_factual_proposals_close_unresolved_without_inventing_claims(
    service, brief, tmp_path, monkeypatch, native, corruption
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    proposal = copy.deepcopy(native["proposal"])
    if corruption == "empty_text":
        proposal["evidence"][0]["text"] = ""
    elif corruption == "long_text":
        proposal["evidence"][0]["text"] = "x" * 241
    elif corruption == "missing_criterion":
        del proposal["evidence"][0]["criterion_id"]
    elif corruption == "bad_outcome":
        proposal["outcome"] = "success"
    elif corruption in {"empty_completed", "empty_declined"}:
        proposal.update(evidence=[], outcome=corruption.removeprefix("empty_"))
    else:
        proposal["evidence"] *= 9
    reviewer = AsyncMock(side_effect=AssertionError("There is no usable claim to review"))
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    await submit(tool, proposal)
    await llm.stop_task
    service.confirm_voice_closed(cid, 1000)
    await service.post_call_reviews.run("owner", cid)
    final = service.result("owner", cid)
    assert final["outcome"] == "unresolved" and final["confirmed_facts"] == []
    reviewer.assert_not_awaited()


async def test_pending_lookup_still_prevents_finish_with_uncited_proposal(
    service, brief, tmp_path, native
):
    cid, llm, tool = await native_voice(service, brief, tmp_path, native)
    llm.lookup_wait.pending["source"] = True
    callback = await submit(tool, native["proposal"])
    assert callback.call_args.args[0] == {"status": "pending", "result_recorded": False}
    assert not llm.stop_requested and not service.provider.stops
    assert not service.call("owner", cid).get("final_report_review")


def test_unresolved_proposal_retains_uncited_confirmable_claims_for_review(native):
    proposal = copy.deepcopy(native["proposal"])
    proposal.update(
        outcome="unresolved", unresolved_questions=["A separate criterion is unresolved."]
    )
    assert (
        ProposedReport.model_validate(proposal).evidence[0].text == proposal["evidence"][0]["text"]
    )
