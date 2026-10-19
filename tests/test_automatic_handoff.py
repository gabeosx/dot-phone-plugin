import time
from decimal import Decimal

import pytest

from dots_phone.local_gmail_worker import private_json, read_json
from dots_phone.models import Brief, Start
from dots_phone.phone_test_campaign import brief_scope
from dots_phone.service import Refused, digest
from tests.conftest import subscribed
from tests.test_local_trip_mail_worker import configured


def enable_profile(service, call, worker):
    scope = brief_scope(call["brief"])
    campaign = service.approve_campaign(
        "owner",
        {
            "destinations": [call["brief"]["destination"]],
            "expires": time.time() + 3600,
            "max_calls": 2,
            "max_seconds": 840,
            "budget_usd": "6",
            "scenarios": [digest(scope)],
            "providers": ["openai", "twilio"],
        },
    )
    private_json(
        worker.directory / "handoff-policy.json",
        {
            "schema": 1,
            "brief_scope": scope,
            "mail_scope": worker.cases[call["call_id"]]["mail_scope"],
            "campaign_id": campaign,
            "expires": time.time() + 3600,
            "gmail_account": "owner@example.com",
        },
    )
    worker.automatic_handoff = True
    return campaign


async def test_prepare_automatically_binds_owned_profile_and_handoff_does_not_dial(
    service, brief, tmp_path
):
    original, worker = configured(service, brief, tmp_path)
    campaign = enable_profile(service, original, worker)
    prepared = service.prepare("owner", Brief.model_validate(original["brief"]))
    cid = prepared["call_id"]
    assert cid in worker.cases and prepared["authorization_id"]
    assert prepared["calling_worker_mode"] == "automatic_owned"
    assert service.store.get("campaign", campaign, "owner")["calls"] == 1
    await subscribed(service, cid)
    service.settings.dry_run = False
    private_json(
        worker.directory / "launcher-ready.json",
        {"schema": 1, "serving": True, "heartbeat_ms": time.time() * 1000},
    )
    params = Start(call_id=cid, revision=1, authorization_id=prepared["authorization_id"])
    state = worker.request_handoff("owner", params)
    _, ticket = read_json(worker.directory / (cid + "-handoff.json"))
    assert state["state"] == "prepared" and state["worker_requested"]
    assert "authorization_id" not in ticket and ticket["brief_hash"] == prepared["brief_hash"]
    assert not service.provider.dials
    assert not service.store.get("approval", prepared["authorization_id"], "owner")["consumed"]
    assert worker.request_handoff("owner", params)["worker_requested"]
    assert read_json(worker.directory / (cid + "-handoff.json"))[1] == ticket


@pytest.mark.parametrize(
    "failure", ["dry_run", "no_subscriptions", "wrong_grant", "offline", "stale", "expired_profile"]
)
async def test_handoff_cannot_bypass_existing_checks(service, brief, tmp_path, failure):
    call, worker = configured(service, brief, tmp_path)
    enable_profile(service, call, worker)
    aid = service.approve("owner", call["call_id"])
    if failure != "no_subscriptions":
        await subscribed(service, call["call_id"])
    service.settings.dry_run = failure == "dry_run"
    private_json(
        worker.directory / "launcher-ready.json",
        {
            "schema": 1,
            "serving": failure != "offline",
            "heartbeat_ms": (time.time() - (20 if failure == "stale" else 0)) * 1000,
        },
    )
    if failure == "expired_profile":
        _, policy = read_json(worker.directory / "handoff-policy.json")
        private_json(
            worker.directory / "handoff-policy.json", {**policy, "expires": time.time() - 1}
        )
    with pytest.raises(Refused):
        worker.request_handoff(
            "owner",
            Start(
                call_id=call["call_id"],
                revision=1,
                authorization_id="wrong" if failure == "wrong_grant" else aid,
            ),
        )
    assert (
        not service.provider.dials
        and not (worker.directory / (call["call_id"] + "-handoff.json")).exists()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"destination": "+12125550101"},
        {"budget_usd": Decimal("4")},
        {"purpose": "Different task"},
        {"synthetic": False},
    ],
)
def test_automatic_profile_rejects_scope_expansion(service, brief, tmp_path, change):
    call, worker = configured(service, brief, tmp_path)
    campaign = enable_profile(service, call, worker)
    with pytest.raises(Refused):
        service.prepare("owner", Brief.model_validate(call["brief"]).model_copy(update=change))
    assert service.store.get("campaign", campaign, "owner")["calls"] == 0


@pytest.mark.parametrize("failure", ["preflight", "timeout", "foreign_report", "unrequested"])
async def test_worker_preflight_failure_is_native_terminal_and_never_dials(
    service, brief, tmp_path, failure
):
    call, worker = configured(service, brief, tmp_path)
    enable_profile(service, call, worker)
    aid = service.approve("owner", call["call_id"])
    await subscribed(service, call["call_id"])
    service.settings.dry_run = False
    private_json(
        worker.directory / "launcher-ready.json",
        {"schema": 1, "serving": True, "heartbeat_ms": time.time() * 1000},
    )
    if failure != "unrequested":
        worker.request_handoff(
            "owner", Start(call_id=call["call_id"], revision=1, authorization_id=aid)
        )
    current = service.call("owner", call["call_id"])
    if failure == "timeout":
        current["worker_handoff"]["requested_ms"] -= 91000
        service.save(current)
    elif failure in {"preflight", "foreign_report"}:
        private_json(
            worker.directory / (call["call_id"] + "-automatic-result.json"),
            {
                "call_id": call["call_id"],
                "revision": 1,
                "brief_hash": call["brief_hash"],
                "handoff_nonce": current["worker_handoff"]["nonce"]
                if failure == "preflight"
                else "wrong",
                "failure": "gmail_account_mismatch",
            },
        )
    await service.tick()
    current = service.call("owner", call["call_id"])
    assert current["state"] == ("failed" if failure in {"preflight", "timeout"} else "prepared")
    assert (
        not service.provider.dials and not service.store.get("approval", aid, "owner")["consumed"]
    )
    if current["state"] == "failed":
        assert current["result"]["outcome"] == "unresolved"
        deliveries = service.store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        assert deliveries > 0
