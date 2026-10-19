"""Offline transport checks: unresolved consultations preserve a usable call.

These inject advice to test runtime behavior; they do not evaluate a live model's
choice of conversational reply or whether it decides to invoke a stop tool.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_reasoner import ReasoningAdvice
from tests.test_call_reasoner import active


def guidance(status, text):
    return ReasoningAdvice(
        status=status, next_step=text, answer="", supporting_quote="", report_supported=False
    )


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
@pytest.mark.parametrize(
    "question,advice",
    [
        (
            "Do you have a membership account with us?",
            guidance("unknown", "Say I don't know; ask to continue without it."),
        ),
        (
            "What is the booking reference?",
            guidance("unknown", "Say I don't have that; ask for another way."),
        ),
        (
            "Would you like to use a discount code?",
            guidance("ready", "Decline the optional discount with No."),
        ),
        ("What is the account ZIP code?", TimeoutError),
    ],
)
async def test_unresolved_lookup_allows_next_turn_without_claim_or_hangup(
    service, brief, tmp_path, policy, question, advice
):
    service.settings.local_reasoner_response_policy = policy
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    before = service.call("owner", cid)
    binding = (before["brief_hash"], before["revision"], before["started_authorization"])
    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(
        advise=AsyncMock(
            **({"side_effect": advice} if advice is TimeoutError else {"return_value": advice})
        )
    )

    async def consult(text, ident):
        pending = service.pending_decision(
            cid,
            ident,
            json.dumps({"operation": "reason", "arguments": {"question": text}}),
        )
        await worker.answer(service.call("owner", cid), pending)
        return json.loads(service.provider.updates[-1][1])

    answer = await consult(question, "unknown-question")
    assert answer.get("answer", "") == ""  # No unsupported fact or invented negative.
    assert answer["status"] == (
        "ready" if isinstance(advice, ReasoningAdvice) and advice.status == "ready" else "unknown"
    )
    assert answer.get("proposed_action") is None and answer.get("control_result") is None
    assert not service.provider.stops
    assert service.call("owner", cid)["pending"] is None

    # The next recipient question can still be handled in the same call.
    worker.reasoner.advise = AsyncMock(return_value=guidance("ready", "Ask for the economy fare."))
    following = await consult("Which cabin would you like?", "next-question")
    assert following["next_step"] == "Ask for the economy fare."
    after = service.call("owner", cid)
    assert after["state"] == "active" and after["pending"] is None
    assert (after["brief_hash"], after["revision"], after["started_authorization"]) == binding
    assert not after.get("conclusion") and not service.provider.stops
    assert len(service.provider.dials) == 1
