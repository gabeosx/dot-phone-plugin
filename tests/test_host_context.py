"""No-network coverage of unexpected memory/plugin questions during a call."""

import json
from datetime import timedelta

import pytest

from dots_phone.account_task import SOURCE_ACTION, verification_digits
from dots_phone.models import Decision, PreparedSource, Start, now
from dots_phone.prepared_evidence import normalize_preparation, prepared_instructions
from dots_phone.service import Refused
from tests.conftest import subscribed
from tests.test_account_task import account_call


async def pending_lookup(service, brief):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    service.settings.dry_run = False
    service.start(
        "owner", Start(call_id=cid, revision=1, authorization_id=service.approve("owner", cid))
    )
    await service.tick()
    pending = service.pending_decision(
        cid,
        "voice-lookup",
        json.dumps(
            {
                "operation": "lookup",
                "arguments": {"question": "What postal code does the owner use for this account?"},
            }
        ),
    )
    assert not worker.handles_pending(service.call("owner", cid))
    return cid, pending


def lookup_answer(cid, pending, *, provider="memory"):
    return Decision(
        call_id=cid,
        request_id=pending["request_id"],
        revision=1,
        answer=json.dumps(
            {
                "untrusted_data": True,
                "message_id": "lookup:" + pending["request_id"],
                "text": "Owner's account profile, retrieved through the authorized profile plugin: postal code 02139.",
                "source": {
                    "provider": provider,
                    "truncated": False,
                    "verified_at": now().isoformat(),
                    "expires_at": (now() + timedelta(seconds=30)).isoformat(),
                },
            }
        ),
        fact_names=[],
        action_names=[SOURCE_ACTION],
    )


@pytest.mark.parametrize("provider", ["memory", "plugin"])
async def test_live_host_evidence_is_bound_and_available_to_verification(service, brief, provider):
    cid, pending = await pending_lookup(service, brief)
    await service.decision("owner", lookup_answer(cid, pending, provider=provider))
    call = service.call("owner", cid)
    source_id = "lookup:" + pending["request_id"]
    assert call["retrieved_sources"][source_id]["provider"] == provider
    assert call["retrieved_sources"][source_id]["brief_hash"] == call["brief_hash"]
    quote = "Enter your postal code."
    assert (
        verification_digits(call, "", quote, quote, source_id=source_id, source_value="02139")
        == "02139"
    )
    with pytest.raises(Refused, match="stale"):
        await service.decision("owner", lookup_answer(cid, pending, provider=provider))
    assert len(service.provider.dials) == 1
    from dots_phone.source_search import source_activity

    assert source_activity(call)["successful_lookup_requests"] == 1


@pytest.mark.parametrize(
    "change", ["wrong_id", "expired", "wrong_provider", "no_scope", "future", "too_late"]
)
async def test_host_answer_cannot_bypass_pending_scope_or_freshness(service, brief, change):
    cid, pending = await pending_lookup(service, brief)
    params = lookup_answer(cid, pending)
    data = json.loads(params.answer)
    if change == "wrong_id":
        data["message_id"] = "lookup:some-other-request"
    elif change == "expired":
        data["source"]["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    elif change == "wrong_provider":
        data["source"]["provider"] = "gmail"
    elif change == "no_scope":
        params.action_names = []
    elif change == "future":
        data["source"]["verified_at"] = (now() + timedelta(seconds=20)).isoformat()
    elif change == "too_late":
        data["source"]["expires_at"] = (now() + timedelta(days=1)).isoformat()
    params.answer = json.dumps(data)
    with pytest.raises(Refused):
        await service.decision("owner", params)
    assert "retrieved_sources" not in service.call("owner", cid)


async def test_host_without_access_returns_unknown_instead_of_inventing_a_fact(service, brief):
    cid, pending = await pending_lookup(service, brief)
    params = lookup_answer(cid, pending)
    params.answer = json.dumps(
        {"untrusted_data": True, "error": "The host has no authorized source for this detail."}
    )
    await service.decision("owner", params)
    call = service.call("owner", cid)
    assert call["source_requests"][-1]["source_error"] is True
    assert not call.get("retrieved_sources")
    assert not call.get("conclusion")


@pytest.mark.parametrize("provider", ["memory", "plugin"])
def test_prepared_host_excerpts_are_approval_bound(service, brief, provider):
    from dots_phone.models import PrepareCall

    source = PreparedSource(
        provider=provider,
        source_id="fictional-profile",
        text="Fictional owner's relevant account profile.",
        truncated=False,
        verified_at=now(),
        expires_at=now() + timedelta(seconds=30),
    )
    request = PrepareCall(**brief.model_dump(), prepared_sources=[source])
    normalized, packet = normalize_preparation(request)
    prepared = service.prepare("owner", request)
    call = service.call("owner", prepared["call_id"])
    assert packet["sources"][0]["provider"] == provider
    assert provider in prepared_instructions(call)
    assert "prepared_evidence_digest" in normalized.disclosures


def test_host_lookup_tool_is_silent_and_preserves_the_question():
    import asyncio
    from types import SimpleNamespace

    pytest.importorskip("pipecat")
    from dots_phone.call_task import task_tools

    calls = []

    class Host:
        async def request_supervision(self, params, question, **kwargs):
            calls.append((json.loads(question), kwargs))

    tool = next(
        t
        for t in task_tools(Host(), host_sources=True)
        if getattr(t, "__name__", "") == "request_call_information"
    )
    asyncio.run(tool(SimpleNamespace(), question="Check the owner's authorized profile plugin."))
    assert calls == [
        (
            {
                "operation": "lookup",
                "arguments": {"question": "Check the owner's authorized profile plugin."},
            },
            {"defer_source_speech": True},
        )
    ]
