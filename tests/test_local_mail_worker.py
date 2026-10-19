import asyncio
import json
import time

import pytest

from dots_phone.local_gmail_worker import private_json, read_json
from dots_phone.local_mail_worker import ACTION, LocalMailWorker, mail_instructions
from dots_phone.service import Refused


def configure(service, brief, tmp_path):
    brief = brief.model_copy(
        update={
            "facts": [],
            "disclosures": [],
            "actions": ["tell_fictional_story", ACTION],
            "constraints": [
                "Read-only Yahoo scope: Test folder; sender store-news@woot.com; public promotional content only"
            ],
            "max_seconds": 90,
        }
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o700)
    entry = {
        "call_id": cid,
        "revision": 1,
        "brief_hash": call["brief_hash"],
        "mailbox": "Test folder",
        "allowed_from": "store-news@woot.com",
        "max_requests": 3,
        "max_searches": 6,
        "max_reads": 4,
    }
    private_json(directory / "registry.json", {"schema": 2, "cases": [entry]})
    worker = LocalMailWorker(service, directory)
    service.local_mail_worker = worker
    private_json(
        directory / "worker-ready.json",
        {
            "schema": 1,
            "serving": True,
            "heartbeat_ms": time.time() * 1000,
            "registry_hash": worker.registry_hash,
        },
    )
    call.update(state="active")
    service.save(call)
    return call, worker


def answer(request, previous=0):
    stamp = time.time() * 1000
    return {
        **{k: request[k] for k in ("schema", "call_id", "request_id", "revision", "nonce")},
        "status": "answered",
        "answer": "The hypothetical total is 42 dollars.",
        "evidence": [
            {
                "mailbox": "Test folder",
                "uid": 1,
                "uidValidity": "123",
                "from": "store-news@woot.com",
                "quote": "A retrieved public price.",
            }
        ],
        "search_count": 1,
        "read_count": 1,
        "request_observed_ms": stamp,
        "result_written_ms": stamp,
    }


async def test_question_not_query_is_published_and_followup_uses_new_correlated_request(
    service, brief, tmp_path
):
    call, worker = configure(service, brief, tmp_path)
    for index, question in enumerate(
        ("Which deal was cheapest?", "What would that cost with tax?")
    ):
        call = service.call("owner", call["call_id"])
        pending = service.pending_decision(call["call_id"], f"tool-{index}", question)
        task = asyncio.create_task(worker.answer(call, pending))
        await asyncio.sleep(0)
        p = worker.directory / (call["call_id"] + "-" + pending["request_id"] + "-request.json")
        _, request = read_json(p)
        assert request["question"] == question and "query" not in request
        result = answer(request)
        private_json(p.with_name(p.name.replace("-request.json", "-result.json")), result)
        await task
        assert not p.exists()
    fresh = service.call("owner", call["call_id"])
    usage = fresh["usage"]["local_mail_worker"]
    assert len(usage["requests"]) == 2 and len({r["request_id"] for r in usage["requests"]}) == 2
    assert (
        len(service.provider.updates) == 2
        and "question" not in json.dumps(usage)
        and "A retrieved" not in json.dumps(usage)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "wrong"},
        {"revision": 2},
        {"schema": 1},
        {"search_count": 7},
        {"read_count": True},
        {"evidence": []},
        {"result_written_ms": float("nan")},
        {"extra": "private"},
    ],
)
def test_mail_results_refuse_forgery_limits_and_unread_answers(service, brief, tmp_path, change):
    call, worker = configure(service, brief, tmp_path)
    request = {
        "schema": 2,
        "call_id": call["call_id"],
        "request_id": "2" * 32,
        "revision": 1,
        "nonce": "n",
        "created_ms": time.time() * 1000,
        "expires_ms": time.time() * 1000 + 60000,
    }
    with pytest.raises(Refused):
        worker.validate_result({**answer(request), **change}, request, {})


async def test_mail_request_is_removed_on_cancellation(service, brief, tmp_path):
    call, worker = configure(service, brief, tmp_path)
    pending = service.pending_decision(call["call_id"], "test", "A spontaneous mail question")
    task = asyncio.create_task(worker.answer(call, pending))
    await asyncio.sleep(0)
    assert list(worker.directory.glob("*-request.json"))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not list(worker.directory.glob("*-request.json"))


def test_mail_prompt_has_no_prepared_queries_or_answers(service, brief, tmp_path):
    call, worker = configure(service, brief, tmp_path)
    text = mail_instructions(call)
    assert "209.99" not in text and "July" not in text and "query" in text and "follow-up" in text
