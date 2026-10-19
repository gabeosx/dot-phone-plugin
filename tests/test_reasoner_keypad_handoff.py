"""Codex advises; the current native voice conversation chooses local controls."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dots_phone.call_reasoner import CallReasoner, KeypadAction, ReasoningAdvice
from dots_phone.models import now
from dots_phone.service import Refused
from tests.test_call_reasoner import active


def menu_advice():
    return ReasoningAdvice(
        status="ready",
        next_step="The balance option was key one; assess it against the current request.",
        answer="",
        supporting_quote="",
        report_supported=False,
        action=KeypadAction(kind="press_phone_key", digit="1"),
    )


def pending_reason(service, cid):
    return service.pending_decision(
        cid,
        "consultation",
        json.dumps({"operation": "reason", "arguments": {"question": "Which input fits?"}}),
    )


@pytest.mark.parametrize(
    "new_recipient_turn",
    [None, "Please enter the member number instead.", "Connecting you to a live agent now."],
)
async def test_direct_keypad_advice_is_a_proposal_even_when_recipient_context_changes(
    service, brief, tmp_path, new_recipient_turn
):
    service.settings.local_reasoner_response_policy = "direct_with_codex"
    prepared = await active(service, brief, tmp_path, keypad=True)
    cid = prepared["call_id"]
    service.provider.press_phone_key = AsyncMock()
    service.provider.enter_verification_fact = AsyncMock()
    call = service.call("owner", cid)
    call["recipient_audio_turn"] = 1
    service.save(call)
    service.observe_transcript(cid, [{"role": "user", "text": "For balance press one."}])
    started, release = asyncio.Event(), asyncio.Event()

    async def reason(*_):
        started.set()
        await release.wait()
        return menu_advice()

    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(advise=AsyncMock(side_effect=reason))
    pending = pending_reason(service, cid)
    task = asyncio.create_task(worker.answer(service.call("owner", cid), pending))
    await started.wait()
    if new_recipient_turn:
        call = service.call("owner", cid)
        call["recipient_audio_turn"] = 2
        service.save(call)
        service.observe_transcript(cid, [{"role": "user", "text": new_recipient_turn}])
    release.set()
    await task
    result = json.loads(service.provider.updates[-1][1])
    assert result["status"] == "ready"
    assert result["next_step"] == menu_advice().next_step
    assert result["proposed_action"] == {
        "tool_name": "press_phone_key",
        "arguments": {"digit": "1"},
    }
    assert result["action_status"] == "proposed_not_executed"
    assert "control_result" not in result
    service.provider.press_phone_key.assert_not_awaited()
    service.provider.enter_verification_fact.assert_not_awaited()
    assert service.call("owner", cid)["pending"] is None


async def test_mandatory_consult_keeps_legacy_scoped_keypad_execution(service, brief, tmp_path):
    service.settings.local_reasoner_response_policy = "always_consult"
    prepared = await active(service, brief, tmp_path, keypad=True)
    cid = prepared["call_id"]
    service.provider.press_phone_key = AsyncMock(
        return_value={"carrier_control_accepted": True, "audio_reconnected": True}
    )
    service.local_context_worker.reasoner = SimpleNamespace(
        advise=AsyncMock(return_value=menu_advice())
    )
    pending = pending_reason(service, cid)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    service.provider.press_phone_key.assert_awaited_once_with(cid, "1", "")
    result = json.loads(service.provider.updates[-1][1])
    assert result["control_result"]["carrier_control_accepted"] is True
    assert "proposed_action" not in result


def source_advice():
    return ReasoningAdvice(
        status="ready",
        next_step="The approved source gives the postal code; assess the requested input.",
        answer="",
        supporting_quote="The current postal code is 02139.",
        report_supported=False,
        action=KeypadAction(kind="enter_verification_fact", source_value="02139", terminator="#"),
    )


async def test_new_read_source_proposal_survives_decision_and_local_control_validates_its_reference(
    service, brief, tmp_path
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_voice import PipecatTelephony

    service.settings.local_reasoner_response_policy = "direct_with_codex"
    prepared = await active(service, brief, tmp_path, source_verification=True)
    cid = prepared["call_id"]
    original_provider = service.provider
    original_provider.enter_verification_fact = AsyncMock()
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(
                    output_parsed=source_advice(),
                    usage=None,
                    codex_transport=True,
                    tool_calls=1,
                    tool_quote_verified=True,
                )
            )
        )
    )
    service.local_context_worker.reasoner = CallReasoner(service, client)
    pending = pending_reason(service, cid)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    original_provider.enter_verification_fact.assert_not_awaited()
    result = json.loads(original_provider.updates[-1][1])
    action = result["proposed_action"]
    assert action["tool_name"] == "enter_verification_fact"
    source_id = action["arguments"]["source_id"]
    assert source_id == "lookup:" + pending["request_id"]
    assert action["arguments"]["source_value"] == "02139"
    call = service.call("owner", cid)
    assert call["pending"] is None and source_id in call["retrieved_sources"]
    assert "02139" not in [f["value"] for f in call["brief"]["facts"]]
    # Realtime can decide later to use the already-retained source through the
    # actual local control; no pending consultation or new disclosure is needed.
    service.settings.pipecat_account_dtmf = True
    carrier_client = MagicMock()
    carrier_client.calls.return_value.update.return_value = SimpleNamespace(
        sid=call["provider_sid"]
    )
    telephony = PipecatTelephony(service.settings, service, carrier_client)
    service.provider = telephony
    call["recipient_audio_turn"] = 3
    service.save(call)
    session = SimpleNamespace(
        carrier=SimpleNamespace(
            begin_change=lambda: "fixture-only-rebind",
            wait_change=AsyncMock(),
            close=AsyncMock(),
            stream_count=2,
        ),
        ending=False,
        llm=SimpleNamespace(dialog=[], clear_playback=AsyncMock()),
        menu_lock=asyncio.Lock(),
        used_menus=set(),
    )
    telephony.sessions[call["provider_sid"]] = session
    accepted = await telephony.enter_verification_fact(cid, **action["arguments"])
    assert accepted["carrier_control_accepted"] is True
    commands = service.call("owner", cid)["private_keypad_commands"]
    command = next(iter(commands.values()))
    assert command["source_id"] == source_id and command["digits"] == "02139#"
    assert command["recipient_audio_turn"] == 3


@pytest.mark.parametrize("invalid", ["missing", "expired", "revision", "hash", "digits"])
async def test_source_proposal_cannot_invent_or_reuse_invalid_source_evidence(
    service, brief, tmp_path, invalid
):
    service.settings.local_reasoner_response_policy = "direct_with_codex"
    prepared = await active(service, brief, tmp_path, source_verification=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    source = {
        "source_id": "retained-source",
        "provider": "plugin",
        "text": source_advice().supporting_quote,
        "truncated": True,
        "verified_at": (now() - timedelta(minutes=1)).isoformat(),
        "expires_at": call["brief"]["deadline"],
        "revision": call["revision"],
        "brief_hash": call["brief_hash"],
    }
    if invalid == "expired":
        source["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    elif invalid == "revision":
        source["revision"] += 1
    elif invalid == "hash":
        source["brief_hash"] = "stale"
    elif invalid == "digits":
        source["text"] = "An unrelated number is 1021390."
    if invalid != "missing":
        call["retrieved_sources"] = {source["source_id"]: source}
    service.save(call)
    service.local_context_worker.reasoner = SimpleNamespace(
        advise=AsyncMock(return_value=source_advice())
    )
    pending = pending_reason(service, cid)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    result = json.loads(service.provider.updates[-1][1])
    assert result["status"] == "unknown" and "proposed_action" not in result


@pytest.mark.parametrize("invalid", ["grant", "scope"])
async def test_changed_authority_discards_advice_before_delivery(service, brief, tmp_path, invalid):
    service.settings.local_reasoner_response_policy = "direct_with_codex"
    prepared = await active(service, brief, tmp_path, keypad=True)
    cid = prepared["call_id"]
    service.provider.press_phone_key = AsyncMock()

    async def reason(*_):
        call = service.call("owner", cid)
        if invalid == "grant":
            grant = deepcopy(service.store.get("approval", call["started_authorization"], "owner"))
            grant["consumed"] = False
            service.store.put("approval", grant["id"], "owner", grant)
        else:
            call["brief"]["purpose"] = "A different unapproved task"
            service.save(call)
        return menu_advice()

    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(advise=AsyncMock(side_effect=reason))
    pending = pending_reason(service, cid)
    with pytest.raises(Refused):
        await worker.answer(service.call("owner", cid), pending)
    assert service.provider.updates == []
    service.provider.press_phone_key.assert_not_awaited()


async def test_maximal_guidance_and_control_data_fit_the_decision_without_annotation_duplicates(
    service, brief, tmp_path
):
    service.settings.local_reasoner_response_policy = "direct_with_codex"
    prepared = await active(service, brief, tmp_path, source_verification=True)
    cid = prepared["call_id"]
    advice = ReasoningAdvice(
        status="ready",
        next_step="x" * 600,
        answer="y" * 240,
        supporting_quote="z" * 400,
        report_supported=False,
        action=KeypadAction(
            kind="enter_verification_fact",
            fact_name="f" * 80,
            prompt_quote="p" * 400,
            menu_quote="m" * 400,
            fact_value="1" * 240,
            terminator="#",
            digit_offset=0,
            digit_count=16,
        ),
    )
    service.local_context_worker.reasoner = SimpleNamespace(advise=AsyncMock(return_value=advice))
    pending = pending_reason(service, cid)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    raw = service.provider.updates[-1][1]
    result = json.loads(raw)
    assert len(raw) <= 1500
    assert result["next_step"] == advice.next_step
    assert result["proposed_action"]["arguments"]["fact_value"] == advice.action.fact_value
    assert result["proposed_action"]["arguments"]["fact_name"] == advice.action.fact_name
    assert "answer" not in result
    assert "prompt_quote" not in result["proposed_action"]["arguments"]
    assert "menu_quote" not in result["proposed_action"]["arguments"]
