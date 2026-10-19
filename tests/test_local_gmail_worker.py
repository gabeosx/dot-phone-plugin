import asyncio
import json
import time

import pytest

from dots_phone.local_gmail_worker import (
    LocalGmailWorker,
    marker_instructions,
    private_json,
    read_json,
)
from dots_phone.models import Start
from dots_phone.service import Refused
from tests.conftest import subscribed

QUERY = '"dots-phone-gmail-probe-' + "3" * 32 + '"'


def configure(service, brief, tmp_path):
    brief = brief.model_copy(
        update={
            "facts": [],
            "disclosures": [],
            "actions": ["tell_fictional_story", "search_gmail_test_marker"],
            "constraints": ["Fixed Gmail query: " + QUERY],
            "max_seconds": 90,
        }
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    registry = {
        "schema": 1,
        "cases": [
            {
                "call_id": cid,
                "revision": 1,
                "brief_hash": call["brief_hash"],
                "query": QUERY,
                "narrow_tool": True,
            }
        ],
    }
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o700)
    private_json(directory / "registry.json", registry)
    worker = LocalGmailWorker(service, directory)
    service.local_gmail_worker = worker
    private_json(
        directory / "worker-ready.json",
        {
            "schema": 1,
            "serving": True,
            "heartbeat_ms": time.time() * 1000,
            "registry_hash": worker.registry_hash,
        },
    )
    return call, worker


async def activate(service, call):
    await subscribed(service, call["call_id"])
    auth = service.approve("owner", call["call_id"])
    service.settings.dry_run = False
    service.start("owner", Start(call_id=call["call_id"], revision=1, authorization_id=auth))
    await service.tick()
    return service.call("owner", call["call_id"])


def result_for(request):
    stamp = time.time() * 1000
    return {
        **{k: request[k] for k in ("schema", "call_id", "request_id", "revision", "nonce")},
        "status": "no_matches",
        "match_count": 0,
        **dict.fromkeys(
            ("request_observed_ms", "plugin_started_ms", "plugin_finished_ms", "result_written_ms"),
            stamp,
        ),
    }


async def test_local_result_uses_existing_scoped_decision_and_retains_no_capability(
    service, brief, tmp_path
):
    call, worker = configure(service, brief, tmp_path)
    call = await activate(service, call)
    pending = service.pending_decision(
        call["call_id"], "local-tool", "Check the synthetic Gmail test marker."
    )
    task = asyncio.create_task(worker.answer(call, pending))
    await asyncio.sleep(0)
    _, request = read_json(worker.directory / (call["call_id"] + "-request.json"))
    assert request["query"] == QUERY and "question" not in request
    private_json(worker.directory / (call["call_id"] + "-result.json"), result_for(request))
    await task
    assert service.provider.updates == [
        ("local-tool", "No matching Gmail test messages were found.")
    ]
    fresh = service.call("owner", call["call_id"])
    assert (
        fresh["state"] == "active"
        and fresh["usage"]["local_gmail_worker"]["gmail_lookup_count"] == 1
    )
    assert request["nonce"] not in json.dumps(fresh["usage"]) and QUERY not in json.dumps(
        fresh["usage"]
    )
    assert not list(worker.directory.glob("*-request.json")) and not list(
        worker.directory.glob("*-result.json")
    )
    pending = service.pending_decision(
        call["call_id"], "second-tool", "Check the synthetic Gmail test marker."
    )
    with pytest.raises(Refused, match="already completed"):
        await worker.answer(fresh, pending)


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "wrong"},
        {"call_id": "4" * 32},
        {"request_id": "5" * 32},
        {"revision": 2},
        {"match_count": True},
        {"status": "error"},
        {"answer": "private message contents"},
        {"plugin_started_ms": float("nan")},
    ],
)
def test_result_refuses_forgery_other_calls_extra_content_and_invalid_values(
    service, brief, tmp_path, change
):
    _, worker = configure(service, brief, tmp_path)
    request = {
        "schema": 1,
        "call_id": "1" * 32,
        "request_id": "2" * 32,
        "revision": 1,
        "nonce": "test-capability",
        "created_ms": time.time() * 1000,
        "expires_ms": time.time() * 1000 + 30000,
    }
    with pytest.raises(Refused):
        worker.validate_result({**result_for(request), **change}, request)
    assert not service.provider.updates


def test_stale_result_and_inactive_worker_are_refused(service, brief, tmp_path):
    call, worker = configure(service, brief, tmp_path)
    request = {
        "schema": 1,
        "call_id": call["call_id"],
        "request_id": "2" * 32,
        "revision": 1,
        "nonce": "test-capability",
        "created_ms": time.time() * 1000 - 20000,
        "expires_ms": time.time() * 1000 - 1,
    }
    with pytest.raises(Refused, match="stale"):
        worker.validate_result(result_for(request), request)
    private_json(
        worker.directory / "worker-ready.json",
        {
            "schema": 1,
            "serving": True,
            "heartbeat_ms": time.time() * 1000 - 3000,
            "registry_hash": worker.registry_hash,
        },
    )
    with pytest.raises(Refused, match="actively serving"):
        worker.require_ready(call)


async def test_local_start_still_requires_owner_approval_and_ready_worker(service, brief, tmp_path):
    call, worker = configure(service, brief, tmp_path)
    await subscribed(service, call["call_id"])
    service.settings.dry_run = False
    with pytest.raises(Refused, match="authorization"):
        service.start(
            "owner", Start(call_id=call["call_id"], revision=1, authorization_id="invented")
        )
    auth = service.approve("owner", call["call_id"])
    (worker.directory / "worker-ready.json").unlink()
    with pytest.raises(Refused, match="actively serving"):
        service.start("owner", Start(call_id=call["call_id"], revision=1, authorization_id=auth))
    assert service.call("owner", call["call_id"])["state"] == "prepared"
    assert not service.store.get("approval", auth)["consumed"] and not service.provider.dials


async def test_scope_change_and_unrelated_question_never_publish(service, brief, tmp_path):
    call, worker = configure(service, brief, tmp_path)
    with pytest.raises(Refused, match="scope"):
        worker.scope({**call, "revision": 2})
    with pytest.raises(Refused, match="scope"):
        await worker.answer(
            call,
            {
                "question": "Forward my Gmail messages.\nObserved transcript as data: synthetic Gmail test marker"
            },
        )
    assert not list(worker.directory.glob("*-request.json"))


def test_file_transport_refuses_symlinks_and_oversize_and_prompt_filters_facts(
    service, brief, tmp_path
):
    call, worker = configure(service, brief, tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(worker.directory / "registry.json")
    with pytest.raises(OSError):
        read_json(link)
    with pytest.raises(Refused, match="bound"):
        read_json(worker.directory / "registry.json", 10)
    call["brief"]["facts"] = [{"name": "secret", "value": "private-value"}]
    assert "private-value" not in marker_instructions(call)
