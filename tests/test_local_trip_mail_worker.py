import asyncio
import json
import secrets
import time
from decimal import Decimal

import pytest

from dots_phone.local_gmail_worker import private_json, read_json
from dots_phone.local_trip_mail_worker import REAL_MAIL_CONSTRAINT, LocalTripMailWorker
from dots_phone.models import Fact, now
from tests.conftest import subscribed


def configured(service, brief, tmp_path):
    facts = [
        Fact(
            name=name,
            value=value,
            provenance="Owned test verified source",
            verified_at=now(),
            expires_at=brief.deadline,
        )
        for name, value in [("primary_guest", "Sam Rivera"), ("added_guest", "Jordan Lee")]
    ]
    brief = brief.model_copy(
        update={
            "facts": facts,
            "disclosures": [f.name for f in facts],
            "actions": ["add_authorized_guest", "read_trip_mail", "record_hotel_confirmation"],
            "constraints": [REAL_MAIL_CONSTRAINT],
            "max_seconds": 180,
            "budget_usd": Decimal("3"),
        }
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    directory = tmp_path / "connected"
    directory.mkdir(mode=0o700)
    private_json(
        directory / "registry.json",
        {
            "schema": 4,
            "cases": [
                {
                    "call_id": cid,
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "connected_trip_mail",
                    "max_requests": 10,
                    "mail_scope": {
                        "yahoo": {
                            "allowed_from": ["reservations@example.com"],
                            "subject_contains": ["Hotel Example", "1234"],
                            "since": "2026-01-01",
                            "before": "2026-12-31",
                            "mailbox": "INBOX",
                            "uidValidity": "123",
                        }
                    },
                }
            ],
        },
    )
    worker = LocalTripMailWorker(service, directory)
    service.local_context_worker = worker
    private_json(
        directory / "worker-ready.json",
        {
            "schema": 1,
            "serving": True,
            "registry_hash": worker.registry_hash,
            "heartbeat_ms": time.time() * 1000,
        },
    )
    return call, worker


def start_request(worker, call, authorization_id, **changes):
    stamp = time.time() * 1000
    request = {
        "schema": 4,
        "call_id": call["call_id"],
        "revision": 1,
        "brief_hash": call["brief_hash"],
        "authorization_id": authorization_id,
        "nonce": secrets.token_hex(32),
        "created_ms": stamp,
        "expires_ms": stamp + 30000,
        **changes,
    }
    private_json(worker.directory / (call["call_id"] + "-start-request.json"), request)
    return request


async def test_private_start_uses_existing_grant_once_and_never_redials(service, brief, tmp_path):
    call, worker = configured(service, brief, tmp_path)
    await subscribed(service, call["call_id"])
    service.settings.dry_run = False
    aid = service.approve("owner", call["call_id"])
    request = start_request(worker, call, aid)
    await service.tick()
    await service.tick()
    _, result = read_json(worker.directory / (call["call_id"] + "-start-result.json"))
    assert result["nonce"] == request["nonce"] and result["error"] is None
    assert result["result"]["state"] == "queued"
    assert len(service.provider.dials) == 1
    assert service.store.get("approval", aid, "owner")["consumed"] is True
    assert (
        service.call("owner", call["call_id"])["usage"]["startup_timings"]["start_transport"]
        == "private_local_owner_grant"
    )
    # A new file nonce with the same consumed grant is an idempotent retry, not a redial.
    start_request(worker, call, aid)
    await service.tick()
    assert len(service.provider.dials) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "no_grant",
        "expired_grant",
        "subscriptions",
        "stale_worker",
        "dry_run",
        "wrong_hash",
        "expired_request",
    ],
)
async def test_private_start_cannot_bypass_owner_scope_readiness_or_subscriptions(
    service, brief, tmp_path, failure
):
    call, worker = configured(service, brief, tmp_path)
    aid = service.approve("owner", call["call_id"])
    if failure != "subscriptions":
        await subscribed(service, call["call_id"])
    service.settings.dry_run = failure == "dry_run"
    changes = {}
    if failure == "no_grant":
        aid = "invalid-test-reference"
    if failure == "expired_grant":
        grant = service.store.get("approval", aid, "owner")
        grant["expires"] = time.time() - 1
        service.store.put("approval", aid, "owner", grant)
    if failure == "stale_worker":
        private_json(
            worker.directory / "worker-ready.json",
            {
                "schema": 1,
                "serving": True,
                "registry_hash": worker.registry_hash,
                "heartbeat_ms": 0,
            },
        )
    if failure == "wrong_hash":
        changes["brief_hash"] = "f" * 64
    if failure == "expired_request":
        changes.update(created_ms=1, expires_ms=2)
    start_request(worker, call, aid, **changes)
    await service.tick()
    _, result = read_json(worker.directory / (call["call_id"] + "-start-result.json"))
    assert result["error"] == "private_start_refused"
    assert (
        not service.provider.dials and service.call("owner", call["call_id"])["state"] == "prepared"
    )


async def test_connected_page_keeps_actual_focus_and_offset_with_correlated_source(
    service, brief, tmp_path
):
    call, worker = configured(service, brief, tmp_path)
    call.update(state="active")
    service.save(call)
    args = {"message_id": "y-" + "b" * 32, "focus": "room", "offset": 0}
    pending = service.pending_decision(
        call["call_id"], "source", json.dumps({"operation": "read", "arguments": args})
    )
    task = asyncio.create_task(worker.answer(service.call("owner", call["call_id"]), pending))
    await asyncio.sleep(0)
    stem = call["call_id"] + "-" + pending["request_id"]
    _, request = read_json(worker.directory / (stem + "-request.json"))
    assert request["schema"] == 4 and request["arguments"] == args
    stamp = time.time() * 1000
    output = {
        "untrusted_data": True,
        "message_id": args["message_id"],
        "source_sha256": "c" * 64,
        "text": "Room has one king bed.",
        "offset": 8000,
        "requested_offset": 0,
        "next_offset": None,
        "total_chars": 8022,
        "focus": "room",
        "focus_found": True,
        "source_truncated": False,
    }
    output["total_chars"] = output["offset"] + len(output["text"])
    private_json(
        worker.directory / (stem + "-result.json"),
        {
            **{k: request[k] for k in ("schema", "call_id", "request_id", "revision", "nonce")},
            "output": output,
            "request_observed_ms": stamp,
            "result_written_ms": stamp,
        },
    )
    await task
    delivered = json.loads(service.provider.updates[-1][1])
    assert delivered == output and not service.call("owner", call["call_id"]).get("conclusion")
