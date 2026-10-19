from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import pytest

from dots_phone.models import Decision, Start, Steer, now
from dots_phone.service import Refused
from dots_phone.store import Store
from tests.conftest import started, subscribed


def test_prepare_never_dials_and_owner_isolation(service, brief):
    call = service.prepare("owner", brief)
    assert not service.provider.dials
    with pytest.raises(Refused):
        service.get("someone_else", call["call_id"])
    assert (
        service.call("owner", call["call_id"])["brief"]["facts"][0]["provenance"]
        == "synthetic fixture"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("destination", "+12125550199"),
        ("disclosures", ["unknown"]),
        ("budget_usd", Decimal("0.01")),
    ],
)
def test_bad_brief_refused(service, brief, field, value):
    with pytest.raises(Refused):
        service.prepare("owner", brief.model_copy(update={field: value}))


def test_stale_fact(service, brief):
    brief.facts[0].expires_at = now() - timedelta(seconds=1)
    with pytest.raises(Refused, match="stale"):
        service.prepare("owner", brief)


async def test_auth_flag_and_subscriptions_required(service, brief):
    call_id = service.prepare("owner", brief)["call_id"]
    with pytest.raises(ValueError):
        Start.model_validate(
            {"call_id": call_id, "revision": 1, "authorization_id": "x", "approved": True}
        )
    params = Start(call_id=call_id, revision=1, authorization_id=service.approve("owner", call_id))
    with pytest.raises(Refused, match="dry_run"):
        service.start("owner", params)
    service.settings.dry_run = False
    with pytest.raises(Refused, match="subscribe"):
        service.start("owner", params)
    assert service.provider.dials == []


async def test_idempotent_start_and_end(service, brief):
    id, params = await started(service, brief)
    service.start("owner", params)
    await service.tick()
    assert len(service.provider.dials) == 1
    await service.end("owner", id)
    await service.end("owner", id)
    assert len(service.provider.stops) == 1
    assert service.result("owner", id)["termination_reason"] == "owner_cancelled"


async def test_concurrency_blocks_second(service, brief):
    await started(service, brief)
    second = service.prepare("owner", brief)["call_id"]
    await subscribed(service, second)
    with pytest.raises(Refused, match="concurrency"):
        service.start(
            "owner",
            Start(call_id=second, revision=1, authorization_id=service.approve("owner", second)),
        )


async def test_ambiguous_dial_never_retried(service, brief):
    service.provider.fail_dial = True
    id, params = await started(service, brief)
    await service.tick()
    service.start("owner", params)
    assert service.get("owner", id)["state"] == "reconcile_required"
    assert len(service.provider.dials) == 1
    await service.callback(id, "CA" + "1" * 32, "in-progress")
    assert len(service.provider.stops) == 1


async def test_cancel_during_dial_stops_returned_sid(service, brief):
    async def cancel(call):
        await service.end("owner", call["call_id"])

    service.provider.on_dial = cancel
    id, _ = await started(service, brief)
    assert service.get("owner", id)["state"] == "cancelled"
    assert service.provider.stops


async def test_failed_teardown_remains_reserved_until_confirmed(service, brief):
    id, _ = await started(service, brief)
    service.provider.fail_stop = True
    await service.end("owner", id)
    assert service.get("owner", id)["state"] == "stopping"
    service.provider.fail_stop = False
    await service.tick()
    assert len(service.provider.stops) == 1  # Respect backoff even after provider recovery.
    call = service.call("owner", id)
    call["stop_retry_after"] = time.time() - 1
    service.save(call)
    await service.tick()
    assert service.get("owner", id)["state"] == "cancelled"


async def test_decision_correct_scope_stale_and_cancelled(service, brief):
    id, _ = await started(service, brief)
    pending = service.pending_decision(id, "delegation1", "Unexpected fee; do you permit payment?")
    params = Decision(
        call_id=id,
        request_id=pending["request_id"],
        revision=1,
        answer="Payment is not authorized.",
        action_names=["pay"],
    )
    with pytest.raises(Refused, match="scope"):
        await service.decision("owner", params)
    params.action_names = []
    await service.decision("owner", params)
    with pytest.raises(Refused, match="stale"):
        await service.decision("owner", params)
    service.pending_decision(id, "delegation2", "Still pay?")
    await service.end("owner", id)
    with pytest.raises(Refused):
        await service.decision("owner", params)


async def test_timeout_and_steering_revision(service, brief):
    id, _ = await started(service, brief)
    service.pending_decision(id, "d1", "Question")
    await service.steer("owner", Steer(call_id=id, revision=1, instruction="Do not pay fees."))
    assert service.get("owner", id)["revision"] == 2
    service.pending_decision(id, "d2", "Another question")
    call = service.call("owner", id)
    call["pending"]["expires"] = time.time() - 1
    service.save(call)
    await service.tick()
    assert service.result("owner", id)["termination_reason"] == "decision_timeout"


async def test_duplicate_out_of_order_callbacks(service, brief):
    id, _ = await started(service, brief)
    service.pending_decision(id, "d1", "Question")
    await service.callback(id, "CA" + "1" * 32, "ringing")
    assert service.get("owner", id)["state"] == "awaiting_decision"
    await service.callback(id, "CA" + "1" * 32, "completed", 17)
    await service.callback(id, "CA" + "1" * 32, "completed", 17)
    assert service.get("owner", id)["usage"]["provider_seconds"] == 17
    assert (
        len(service.store.db.execute("SELECT * FROM deliveries").fetchall()) == 2
    )  # Decision + terminal.


async def test_late_carrier_duration_reconciles_usage_without_reopening_or_reemitting(
    service, brief
):
    id, _ = await started(service, brief)
    await service.end("owner", id, "voice_disconnected")
    before = service.result("owner", id)
    deliveries = service.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
    await service.callback(id, "CA" + "1" * 32, "completed", 122)
    await service.callback(id, "CA" + "1" * 32, "completed", 100)
    after = service.result("owner", id)
    assert after["usage"]["provider_seconds"] == 122
    assert after["state"] == before["state"] == "failed"
    assert after["termination_reason"] == before["termination_reason"] == "voice_disconnected"
    assert not after["usage"]["billing_confirmed"]
    assert service.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == deliveries


async def test_late_carrier_duration_still_rejects_another_call_sid(service, brief):
    id, _ = await started(service, brief)
    await service.end("owner", id, "voice_disconnected")
    with pytest.raises(Refused, match="SID mismatch"):
        await service.callback(id, "CA" + "2" * 32, "completed", 122)
    assert service.result("owner", id)["usage"]["provider_seconds"] == 0


@pytest.mark.parametrize(
    "reason,state",
    [
        ("recipient_stop", "completed"),
        ("owner_cancelled", "cancelled"),
        ("voice_disconnected", "failed"),
    ],
)
async def test_completed_callback_preserves_requested_stop_reason(service, brief, reason, state):
    id, _ = await started(service, brief)
    call = service.call("owner", id)
    call.update(state="stopping", stop_reason=reason)
    service.save(call)
    await service.callback(id, "CA" + "1" * 32, "completed", 17)
    assert service.get("owner", id)["state"] == state
    assert service.result("owner", id)["termination_reason"] == reason
    assert service.get("owner", id)["usage"]["provider_seconds"] == 17


async def test_clean_recipient_stop_does_not_claim_the_task_succeeded(service, brief):
    id, _ = await started(service, brief)
    await service.end("owner", id, "recipient_stop")
    assert service.get("owner", id)["state"] == "completed"
    result = service.result("owner", id)
    assert result["termination_reason"] == "recipient_stop"
    assert result["outcome"] == "unresolved" and result["confirmed_facts"] == []


async def test_call_watch_wakes_for_current_decision_and_checks_owner(service, brief):
    import asyncio

    id, _ = await started(service, brief)
    service.settings.call_state_wait_seconds = 1
    waiting = asyncio.create_task(service.watch("owner", id))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    pending = service.pending_decision(id, "wait-fixture", "Which approved date?")
    current = await asyncio.wait_for(waiting, timeout=0.5)
    assert (
        current["state"] == "awaiting_decision"
        and current["pending"]["request_id"] == pending["request_id"]
    )
    with pytest.raises(Refused):
        await service.watch("other-owner", id)


async def test_call_watch_is_bounded_and_approval_terminal_reads_are_immediate(service, brief):
    import asyncio

    prepared = service.prepare("owner", brief)["call_id"]
    service.settings.call_state_wait_seconds = 1
    assert (await asyncio.wait_for(service.watch("owner", prepared), timeout=0.1))[
        "state"
    ] == "prepared"
    id, _ = await started(service, brief)
    before = time.monotonic()
    assert (await service.watch("owner", id))["state"] == "active"
    assert 0.9 <= time.monotonic() - before < 1.3
    await service.end("owner", id, "recipient_stop")
    assert (await asyncio.wait_for(service.watch("owner", id), timeout=0.1))["state"] == "completed"


async def test_local_pending_watch_waits_for_answer_but_remote_pending_is_immediate(service, brief):
    import asyncio
    from types import SimpleNamespace

    id, _ = await started(service, brief)
    service.settings.call_state_wait_seconds = 1
    pending = service.pending_decision(id, "local-fixture", "Which approved date?")
    assert (await asyncio.wait_for(service.watch("owner", id), timeout=0.1))["pending"]
    service.local_mail_worker = SimpleNamespace(registered=lambda call: call["call_id"] == id)
    waiting = asyncio.create_task(service.watch("owner", id))
    await asyncio.sleep(0.02)
    assert not waiting.done()
    await service.decision(
        "owner",
        Decision(
            call_id=id,
            request_id=pending["request_id"],
            revision=1,
            answer="The approved date.",
            fact_names=["date"],
        ),
    )
    assert (await asyncio.wait_for(waiting, timeout=0.5))["state"] == "active"


async def test_restart_ends_known_call(service, brief):
    id, _ = await started(service, brief)
    await service.recover()
    assert service.result("owner", id)["termination_reason"] == "server_restart"


def test_encrypted_storage_wrong_key(service, brief):
    service.prepare("owner", brief)
    contents = service.settings.database.read_bytes()
    assert b"synthetic fixture" not in contents
    from cryptography.fernet import InvalidToken

    service.store.close()
    with pytest.raises(InvalidToken):
        Store(service.settings.database, "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=")
    service.store = Store(
        service.settings.database, service.settings.encryption_key.get_secret_value()
    )


async def test_campaign_counts_both_sides_and_idempotency(service, brief):
    campaign = service.approve_campaign(
        "owner",
        {
            "destinations": [brief.destination],
            "expires": time.time() + 300,
            "max_calls": 1,
            "max_seconds": 660,
            "budget_usd": "2",
            "scenarios": ["hotel"],
            "providers": ["openai", "twilio"],
        },
    )
    first = service.prepare("owner", brief)["call_id"]
    auth = service.campaign_authorization("owner", campaign, first, "hotel")
    assert service.campaign_authorization("owner", campaign, first, "hotel") == auth
    assert service.store.get("campaign", campaign, "owner")["seconds"] == 660
    second = service.prepare("owner", brief)["call_id"]
    with pytest.raises(Refused, match="exhausted"):
        service.campaign_authorization("owner", campaign, second, "hotel")
