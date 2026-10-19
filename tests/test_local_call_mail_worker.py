import pytest

from dots_phone.local_call_mail_worker import (
    INFORMATION_ACTIONS,
    INFORMATION_CONSTRAINT,
    LocalCallMailWorker,
)
from dots_phone.local_gmail_worker import private_json
from dots_phone.service import Refused


def fixture(service, brief, tmp_path):
    phrase = brief.facts[0].model_copy(
        update={
            "name": "information_confirmation_phrase",
            "value": "I've noted both accommodation deadlines",
        }
    )
    brief = brief.model_copy(
        update={
            "actions": sorted(INFORMATION_ACTIONS),
            "constraints": [INFORMATION_CONSTRAINT],
            "facts": [phrase],
            "disclosures": [phrase.name],
            "max_seconds": 180,
        }
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    private_json(
        tmp_path / "registry.json",
        {
            "schema": 5,
            "cases": [
                {
                    "call_id": cid,
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "connected_trip_mail",
                    "max_requests": 10,
                    "mail_scope": {
                        "gmail": {
                            "allowed_from": ["event@example.com"],
                            "subject_contains": ["Event"],
                            "since": "2026-01-01",
                            "before": "2027-01-01",
                        }
                    },
                }
            ],
        },
    )
    return LocalCallMailWorker(service, tmp_path), call


def test_information_scope_does_not_require_or_grant_reservation_changes(service, brief, tmp_path):
    worker, call = fixture(service, brief, tmp_path)
    assert worker.scope(call)["call_id"] == call["call_id"]
    assert worker.read_action == "read_call_mail"
    call["brief"]["actions"].append("add_authorized_guest")
    with pytest.raises(Refused):
        worker.scope(call)


def test_information_completion_requires_actual_recipient_acknowledgement(service, brief, tmp_path):
    worker, call = fixture(service, brief, tmp_path)
    quote = "Yes, I've noted both accommodation deadlines."
    with pytest.raises(Refused):
        worker.conclusion(call, quote)
    service.transcripts[call["call_id"]] = [{"role": "assistant", "text": quote}]
    with pytest.raises(Refused):
        worker.conclusion(call, quote)
    service.transcripts[call["call_id"]] = [{"role": "user", "text": quote}]
    conclusion = worker.conclusion(call, "Yes I've noted both accommodation deadlines")
    assert conclusion.outcome == "completed" and conclusion.commitments == []
    assert conclusion.confirmed_facts[0].quote in quote


def test_information_fixture_has_no_automatic_handoff(service, brief, tmp_path):
    worker, call = fixture(service, brief, tmp_path)
    worker.register_prepared("owner", call)
    assert len(worker.cases) == 1
    private_json(tmp_path / "handoff-policy.json", {})
    with pytest.raises(Refused, match="automatic phone campaign"):
        LocalCallMailWorker(service, tmp_path)


@pytest.mark.parametrize(
    "observed",
    [
        "No, I didn't say 'I've noted both accommodation deadlines'.",
        "I have not confirmed that I've noted both accommodation deadlines.",
        "I never said I've noted both accommodation deadlines.",
    ],
)
def test_quoted_acknowledgement_inside_denial_cannot_complete(service, brief, tmp_path, observed):
    worker, call = fixture(service, brief, tmp_path)
    service.transcripts[call["call_id"]] = [
        {"role": "user", "text": "Yes, I've noted both accommodation deadlines."},
        {"role": "user", "text": observed},
    ]
    with pytest.raises(Refused):
        worker.conclusion(call, "I've noted both accommodation deadlines")
