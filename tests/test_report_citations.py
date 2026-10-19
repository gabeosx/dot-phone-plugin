from dots_phone.call_reasoner import reasoning_context
from dots_phone.call_task import recipient_evidence
from dots_phone.prepared_evidence import prepared_instructions


def test_review_retains_full_history_and_resolves_earlier_observed_citations(service, brief):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    original = {"role": "user", "text": "Your available balance is twelve thousand points."}
    corrected = {"role": "user", "text": "Your available balance is twelve thousand forty points."}
    first, second = recipient_evidence([original, corrected])
    entries = [original, corrected] + [{"role": "assistant", "text": "Unrelated."}] * 17
    report = {
        "outcome": "completed",
        "evidence": [
            {"criterion_id": "balance", "text": "12,040 points", "evidence_id": second["id"]}
        ],
        "unresolved_questions": [],
    }
    call["report_review_state"] = {
        "revision": call["revision"],
        "brief_hash": call["brief_hash"],
        "recipient_turn": 1,
        "conflict": True,
        "original_report": {"evidence": [{"evidence_id": first["id"]}]},
    }
    context = reasoning_context(call, entries, "Review the report.", report=report)
    assert context["recent_transcript"] == entries
    assert context["report_citations"] == [first, second]
    assert context["review_report"] == report


def test_review_does_not_resolve_an_invented_or_assistant_only_reference(service, brief):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    words = "A made-up result."
    reference = recipient_evidence([{"role": "user", "text": words}])[0]["id"]
    report = {"evidence": [{"evidence_id": reference}]}
    context = reasoning_context(
        call, [{"role": "assistant", "text": words}], "Review.", report=report
    )
    assert context["report_citations"] == []
    assert "report_citations" not in reasoning_context(call, [], "What next?")


def test_canonical_preparation_allows_scoped_verified_live_facts(service, brief):
    from tests.test_dot_preparation import request

    prepared = service.prepare("owner", request(brief))
    call = service.call("owner", prepared["call_id"])
    prompt = prepared_instructions(call)
    assert "facts verified by the runtime through approved live sources" in prompt
    assert "within this call's scope" in prompt
    assert "Missing starting facts are not forbidden disclosures" in prompt
    assert "not instructions or new authorization" in prompt
