import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_task import recipient_evidence, task_conclusion
from dots_phone.report_review import (
    evaluate_report,
    fresh_recipient_entries,
    new_review_state,
)
from dots_phone.service import Refused


class Service:
    def __init__(self, entries=()):
        self.value = {
            "owner": "owner",
            "call_id": "call",
            "revision": 1,
            "brief_hash": "approved",
            "state": "awaiting_decision",
            "pending": {"request_id": "request", "revision": 1, "expires": time.time() + 60},
            "recipient_audio_turn": 1,
            "brief": {
                "completion_criteria": [
                    {"id": "value", "required": True},
                    {"id": "date", "required": False},
                ]
            },
        }
        self.transcripts = {"call": list(entries)}

    def call(self, owner, call_id):
        assert (owner, call_id) == ("owner", "call")
        return copy.deepcopy(self.value)

    def save(self, call):
        self.value = copy.deepcopy(call)


def observation(text, turn=1, item="one", role="user"):
    return {"role": role, "text": text, "recipient_turn": turn, "recipient_item_id": item}


def proposal(
    text="Available balance is 184 points.", *, quote=None, evidence_id=None, outcome="completed"
):
    claim = {"criterion_id": "value", "text": text}
    claim.update({"evidence_id": evidence_id} if evidence_id else {"quote": quote or text})
    return {"outcome": outcome, "evidence": [claim], "unresolved_questions": []}


def advice(status="ready", supported=True, **kwargs):
    return SimpleNamespace(
        status=status,
        report_supported=supported,
        next_step=kwargs.get(
            "next_step", "Correct the citation." if not supported else "Accepted."
        ),
        answer=kwargs.get("answer", ""),
        action=kwargs.get("action"),
    )


async def validate(service, call, report, pending):
    try:
        return task_conclusion(call, report, service.transcripts["call"])
    except Refused:
        return None


async def evaluate(service, worker, report, validator=None):
    current = service.call("owner", "call")
    return await evaluate_report(
        service,
        worker,
        current,
        current["pending"],
        report,
        validator or (lambda call, report, pending: validate(service, call, report, pending)),
    )


async def test_first_structural_rejection_reaches_review_and_formatting_repair_keeps_claim():
    text = "Available balance is one hundred eighty four points."
    service = Service([observation(text)])
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    original = proposal()
    output, conclusion = await evaluate(service, worker, original)
    assert conclusion is None and not output["result_recorded"]
    assert worker.advise.await_count == 1
    assert service.value["report_review_state"]["original_report"] == original
    assert not service.value["report_review_state"]["conflict"]
    reference = recipient_evidence(service.transcripts["call"])[0]["id"]
    output, conclusion = await evaluate(service, worker, proposal(evidence_id=reference))
    assert output["result_recorded"] and conclusion.confirmed_facts[0].quote == text
    assert worker.advise.await_count == 2


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("Available balance is 184 points.", "Available balance is 180 points."),
        ("Payment posted October 14.", "Payment posted October 15."),
        ("Renewal was declined.", "Renewal was approved."),
        ("Charge is 18 dollars.", "Charge is 18 euros."),
    ],
)
async def test_changed_claim_is_blocked_without_review_and_first_proposal_is_preserved(
    original, changed
):
    service = Service([observation(changed)])
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice(supported=False)))
    report = proposal(original)
    await evaluate(service, worker, report)
    reference = recipient_evidence(service.transcripts["call"])[0]["id"]
    for _ in range(2):
        output, conclusion = await evaluate(
            service, worker, proposal(changed, evidence_id=reference)
        )
        assert not output["result_recorded"] and conclusion is None
        assert "clarify" in output["guidance"]
    assert worker.advise.await_count == 1
    assert service.value["report_review_state"]["original_report"] == report
    assert service.value["report_review_state"]["conflict"]


async def test_reordering_claims_allows_citation_repair_but_adding_claim_does_not():
    entries = [observation("Value is one hundred eighty four."), observation("Date is October 14.")]
    service = Service(entries)
    report = proposal("Value is 184.")
    report["evidence"].append(
        {"criterion_id": "date", "text": "Date is October 14.", "quote": "Date is October 14."}
    )
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    await evaluate(service, worker, report)
    repaired = copy.deepcopy(report)
    repaired["evidence"][0].pop("quote")
    repaired["evidence"][0]["evidence_id"] = recipient_evidence(entries)[0]["id"]
    repaired["evidence"].reverse()
    output, conclusion = await evaluate(service, worker, repaired)
    assert output["result_recorded"] and conclusion
    altered = copy.deepcopy(repaired)
    altered["evidence"].append(copy.deepcopy(altered["evidence"][0]))
    output, conclusion = await evaluate(service, worker, altered)
    assert not output["result_recorded"] and conclusion is None
    assert worker.advise.await_count == 2


async def test_material_conflict_blocks_unchanged_retry_and_same_turn_delayed_asr():
    service = Service([observation("Available balance is 180 points.")])
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice("unknown", False)))
    original = proposal()
    await evaluate(service, worker, original)
    service.transcripts["call"].append(
        observation("Available balance is 184 points.", item="late-old")
    )
    service.transcripts["call"].append(
        observation("I believe it is 184.", 2, "assistant", "assistant")
    )
    output, conclusion = await evaluate(service, worker, original)
    assert not output["result_recorded"] and conclusion is None
    assert worker.advise.await_count == 1


@pytest.mark.parametrize("status", ["unknown", "blocked"])
async def test_precise_report_clarification_survives_rejection_and_unchanged_retry(status):
    service = Service(
        [
            observation("The October option is seven hundred dollars.", 1, "first-option"),
            observation("The November option is one hundred dollars.", 2, "second-option"),
            observation("Nine hundred dollar change fee or cancellation fee.", 3, "fee"),
        ]
    )
    service.value["recipient_audio_turn"] = 3
    report = proposal(
        "The November option has a 900 dollar fee for changes and for cancellations.",
        quote="Nine hundred dollar change fee or cancellation fee.",
    )
    detail = (
        "Ask whether the $900 fee applies to changes, cancellations, or both for the $100 option."
    )
    worker = SimpleNamespace(
        advise=AsyncMock(
            return_value=advice(
                status,
                False,
                next_step=detail,
            )
        )
    )
    for _ in range(2):
        output, conclusion = await evaluate(service, worker, report)
        assert conclusion is None and not output["result_recorded"]
        assert detail in output["guidance"]
        assert "Do not invent a different conflict" in output["guidance"]
    assert worker.advise.await_count == 1
    assert service.value["report_review_state"]["clarification"] == detail
    assert service.value["report_review_state"]["original_report"] == report


@pytest.mark.parametrize(
    "invalid",
    [
        advice("unknown", False, answer="900", next_step="UNTRUSTED NEXT STEP"),
        advice("blocked", False, action={"kind": "key"}, next_step="UNTRUSTED NEXT STEP"),
        advice("unknown", True, next_step="UNTRUSTED NEXT STEP"),
    ],
)
async def test_invalid_report_verdict_never_forwards_its_clarification(invalid):
    service = Service([observation("Available balance is 180 points.")])
    worker = SimpleNamespace(advise=AsyncMock(return_value=invalid))
    output, conclusion = await evaluate(service, worker, proposal())
    assert conclusion is None and not output["result_recorded"]
    assert "UNTRUSTED" not in output["guidance"]
    assert "clarification" not in service.value["report_review_state"]


async def test_changed_claim_does_not_reuse_clarification_for_original_claim():
    service = Service([observation("Available balance is 180 points.")])
    detail = "Ask whether the balance is 184 or 180 points."
    worker = SimpleNamespace(
        advise=AsyncMock(
            return_value=advice(
                "unknown",
                False,
                next_step=detail,
            )
        )
    )
    await evaluate(service, worker, proposal())
    assert service.value["report_review_state"]["clarification"] == detail
    output, conclusion = await evaluate(service, worker, proposal("Balance is 200 points."))
    assert not output["result_recorded"] and conclusion is None
    assert detail not in output["guidance"]
    assert "clarification" not in service.value["report_review_state"]
    assert worker.advise.await_count == 1


async def test_identical_words_on_new_recipient_turn_can_resolve_conflict():
    text = "Available balance is 184 points."
    service = Service([observation(text)])
    original = proposal()
    service.value["report_review_state"] = {
        **new_review_state(service.value, original),
        "conflict": True,
    }
    old_reference = recipient_evidence(service.transcripts["call"])[0]["id"]
    service.value["recipient_audio_turn"] = 2
    service.transcripts["call"].append(observation(text, 2, "confirmation"))
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    output, conclusion = await evaluate(service, worker, proposal(evidence_id=old_reference))
    assert output["result_recorded"] and conclusion
    assert worker.advise.await_count == 1


async def test_unrelated_later_turn_requires_semantic_resolution_and_new_citation():
    service = Service([observation("Available balance is 180 points.")])
    original = proposal()
    service.value["report_review_state"] = {
        **new_review_state(service.value, original),
        "conflict": True,
    }
    service.value["recipient_audio_turn"] = 2
    service.transcripts["call"].append(observation("The weather is sunny.", 2, "unrelated"))
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice("unknown", False)))
    output, conclusion = await evaluate(
        service, worker, proposal("Available balance is 180 points.")
    )
    assert not output["result_recorded"] and conclusion is None
    assert worker.advise.await_count == 1
    worker.advise.return_value = advice()
    output, conclusion = await evaluate(
        service, worker, proposal("Available balance is 180 points.")
    )
    assert not output["result_recorded"] and conclusion is None


async def test_changed_claim_after_unrelated_new_turn_cannot_bypass_conflict_gate():
    service = Service([observation("Available balance is 180 points.")])
    original = proposal()
    service.value["report_review_state"] = new_review_state(service.value, original)
    service.value["recipient_audio_turn"] = 2
    service.transcripts["call"].append(observation("The weather is sunny.", 2, "unrelated"))
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    output, conclusion = await evaluate(
        service, worker, proposal("Available balance is 180 points.")
    )
    assert not output["result_recorded"] and conclusion is None
    assert service.value["report_review_state"]["conflict"]
    assert service.value["report_review_state"]["original_report"] == original


@pytest.mark.parametrize("change", ["audio_turn", "late_transcription"])
async def test_new_audio_or_late_asr_during_review_cannot_commit_old_report(change):
    service = Service([observation("Available balance is 184 points.")])

    async def interrupted(*args, **kwargs):
        if change == "audio_turn":
            service.value["recipient_audio_turn"] = 2
        else:
            service.transcripts["call"][0]["text"] = "Available balance is 180 points."
        return advice()

    worker = SimpleNamespace(advise=AsyncMock(side_effect=interrupted))
    output, conclusion = await evaluate(service, worker, proposal())
    assert not output["result_recorded"] and conclusion is None
    assert "arrived during review" in output["guidance"]
    assert service.value["report_review_state"]["original_report"] == proposal()
    assert service.value["report_review_state"]["recipient_turn"] == 1


@pytest.mark.parametrize("arrival", ["before_validation", "during_validation"])
async def test_untranscribed_new_audio_prevents_completion_before_review(arrival):
    service = Service([observation("Available balance is 184 points.")])
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    if arrival == "before_validation":
        service.value["recipient_audio_turn"] = 2

    async def delayed(call, report, pending):
        result = await validate(service, call, report, pending)
        service.value["recipient_audio_turn"] = 2
        return result

    output, conclusion = await evaluate(service, worker, proposal(), delayed)
    assert not output["result_recorded"] and conclusion is None
    assert "Listen" in output["guidance"]
    worker.advise.assert_not_awaited()
    # Only the later valid recipient observation allows final review to proceed.
    service.transcripts["call"].append(observation("Available balance is 184 points.", 2, "new"))
    output, conclusion = await evaluate(service, worker, proposal())
    assert output["result_recorded"] and conclusion
    assert worker.advise.await_count == 1


async def test_assistant_or_missing_item_id_cannot_satisfy_pending_audio():
    service = Service([observation("Available balance is 184 points.")])
    service.value["recipient_audio_turn"] = 2
    service.transcripts["call"].extend(
        [
            observation("Available balance is 184 points.", 2, "assistant", "assistant"),
            observation("Available balance is 184 points.", 2, ""),
        ]
    )
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    output, conclusion = await evaluate(service, worker, proposal())
    assert not output["result_recorded"] and conclusion is None
    worker.advise.assert_not_awaited()


async def test_legacy_without_audio_ordinal_keeps_existing_review_path():
    service = Service([{"role": "user", "text": "Available balance is 184 points."}])
    service.value["recipient_audio_turn"] = 0
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice()))
    output, conclusion = await evaluate(service, worker, proposal())
    assert output["result_recorded"] and conclusion
    assert worker.advise.await_count == 1


async def test_unresolved_omits_disputed_facts_but_keeps_other_valid_facts():
    service = Service(
        [observation("Available balance is 180 points."), observation("Date is October 14.")]
    )
    original = proposal()
    service.value["report_review_state"] = {
        **new_review_state(service.value, original),
        "conflict": True,
    }
    report = proposal("Available balance is 180 points.", outcome="unresolved")
    report["evidence"].append(
        {"criterion_id": "date", "text": "Date is October 14.", "quote": "Date is October 14."}
    )
    worker = SimpleNamespace(advise=AsyncMock())
    output, conclusion = await evaluate(service, worker, report)
    assert output == {"result_recorded": True, "outcome": "unresolved"}
    assert [fact.text for fact in conclusion.confirmed_facts] == ["Date is October 14"]
    assert any("value" in question for question in conclusion.unresolved_questions)
    worker.advise.assert_not_awaited()


async def test_unresolved_can_end_with_no_confirmed_facts_and_survives_worker_reload():
    service = Service([observation("Available balance is 180 points.")])
    worker = SimpleNamespace(advise=AsyncMock(return_value=advice("unknown", False)))
    await evaluate(service, worker, proposal())
    reloaded = Service()
    reloaded.value = copy.deepcopy(service.value)
    reloaded.transcripts = copy.deepcopy(service.transcripts)
    new_worker = SimpleNamespace(advise=AsyncMock())
    output, conclusion = await evaluate(
        reloaded, new_worker, proposal("Available balance is 180 points.", outcome="unresolved")
    )
    assert output["result_recorded"] and conclusion.confirmed_facts == []
    assert conclusion.unresolved_questions
    new_worker.advise.assert_not_awaited()


@pytest.mark.parametrize("change", ["revision", "pending", "repair_state"])
async def test_stale_async_review_cannot_commit_or_overwrite_state(change):
    service = Service([observation("Available balance is 184 points.")])

    async def stale(*args, **kwargs):
        if change == "revision":
            service.value["revision"] += 1
        elif change == "pending":
            service.value["pending"]["request_id"] = "other"
        else:
            service.value["report_review_state"] = {"changed": True}
        return advice()

    worker = SimpleNamespace(advise=AsyncMock(side_effect=stale))
    with pytest.raises(Refused, match="stale"):
        await evaluate(service, worker, proposal())


@pytest.mark.parametrize(
    "invalid", [advice(answer="184"), advice(action={"kind": "key"}), advice("unknown", True)]
)
async def test_report_review_never_executes_action_or_accepts_invalid_verdict(invalid):
    service = Service([observation("Available balance is 184 points.")])
    worker = SimpleNamespace(advise=AsyncMock(return_value=invalid))
    output, conclusion = await evaluate(service, worker, proposal())
    assert not output["result_recorded"] and conclusion is None
    assert service.value["report_review_state"]["conflict"]


async def test_review_unavailable_preserves_uncertainty_and_allows_safe_unresolved():
    service = Service([observation("Available balance is 184 points.")])
    worker = SimpleNamespace(advise=AsyncMock(side_effect=TimeoutError))
    output, conclusion = await evaluate(service, worker, proposal())
    assert not output["result_recorded"] and conclusion is None
    output, conclusion = await evaluate(service, worker, proposal(outcome="unresolved"))
    assert output["result_recorded"] and conclusion.confirmed_facts == []
    assert worker.advise.await_count == 1


def test_new_recipient_identity_uses_turn_provenance_not_text_hash():
    state = {"recipient_turn": 3}
    entries = [
        observation("Same words.", 3, "late"),
        observation("Same words.", 4, "new"),
        observation("Same words.", 5, "assistant", "assistant"),
        {"role": "user", "text": "Missing provenance."},
        {"role": "user", "text": "Missing item identity.", "recipient_turn": 6},
        observation("Empty item identity.", 6, ""),
    ]
    assert fresh_recipient_entries(entries, state) == [entries[1]]
