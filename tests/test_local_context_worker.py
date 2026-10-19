import asyncio
import json
import time
from decimal import Decimal

import pytest

from dots_phone.local_context_worker import ACTIONS, LocalContextWorker, hotel_instructions
from dots_phone.local_gmail_worker import private_json, read_json
from dots_phone.models import Fact, now
from dots_phone.service import Refused


def configured(service, brief, tmp_path):
    guest = Fact(
        name="added_guest",
        value="Jordan Lee",
        provenance="Owned synthetic owner request",
        verified_at=now(),
        expires_at=brief.deadline,
    )
    brief = brief.model_copy(
        update={
            "facts": [
                guest,
                guest.model_copy(update={"name": "primary_guest", "value": "Sam Rivera"}),
            ],
            "disclosures": [guest.name, "primary_guest"],
            "actions": sorted(ACTIONS),
            "constraints": ["Owned synthetic hotel and reservation fixtures only"],
            "max_seconds": 180,
            "budget_usd": Decimal("3"),
        }
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700)
    private_json(
        directory / "registry.json",
        {
            "schema": 3,
            "cases": [
                {
                    "call_id": cid,
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "synthetic_trip_mail",
                    "max_requests": 6,
                }
            ],
        },
    )
    worker = LocalContextWorker(service, directory)
    service.local_context_worker = worker
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


async def test_actual_voice_query_and_source_data_are_relayed_without_worker_reasoning(
    service, brief, tmp_path
):
    call, worker = configured(service, brief, tmp_path)
    for index, (operation, arguments, output) in enumerate(
        [
            (
                "search",
                {"query": "Jordan arrival"},
                {
                    "untrusted_data": True,
                    "messages": [
                        {
                            "message_id": "new-note",
                            "subject": "A new trip note",
                            "snippet": "Source metadata only",
                        }
                    ],
                },
            ),
            (
                "read",
                {"message_id": "new-note"},
                {
                    "untrusted_data": True,
                    "message_id": "new-note",
                    "source_sha256": "a" * 64,
                    "text": "Train arrives at 8:40 PM, then 35 minutes to the hotel.",
                },
            ),
        ]
    ):
        command = json.dumps({"operation": operation, "arguments": arguments})
        pending = service.pending_decision(call["call_id"], f"tool-{index}", command)
        task = asyncio.create_task(worker.answer(service.call("owner", call["call_id"]), pending))
        await asyncio.sleep(0)
        path = worker.directory / (call["call_id"] + "-" + pending["request_id"] + "-request.json")
        _, request = read_json(path)
        assert request["arguments"] == arguments
        stamp = time.time() * 1000
        private_json(
            path.with_name(path.name.replace("-request", "-result")),
            {
                **{k: request[k] for k in ("schema", "call_id", "request_id", "revision", "nonce")},
                "output": output,
                "request_observed_ms": stamp,
                "result_written_ms": stamp,
            },
        )
        await task
        assert not path.exists()
    assert (
        "8:40" in service.provider.updates[-1][1] and "9:15" not in service.provider.updates[-1][1]
    )
    assert [
        r["operation"]
        for r in service.call("owner", call["call_id"])["usage"]["local_context_worker"]["requests"]
    ] == ["search", "read"]


async def test_completion_records_actual_recipient_confirmation_and_survives_hangup(
    service, brief, tmp_path
):
    call, worker = configured(service, brief, tmp_path)
    actual = "I have added Jordan Lee. Jordan Lee can check in without Sam Rivera and must bring photo ID."
    service.transcripts[call["call_id"]] = [{"role": "user", "text": actual}]
    pending = service.pending_decision(
        call["call_id"],
        "completion",
        json.dumps(
            {
                "operation": "complete",
                "arguments": {
                    "confirmation_quote": "Jordan Lee can check in without Sam Rivera and must bring photo ID"
                },
            }
        ),
    )
    await worker.answer(service.call("owner", call["call_id"]), pending)
    c = service.call("owner", call["call_id"])
    assert c["conclusion"]["outcome"] == "completed"
    assert c["conclusion"]["confirmed_facts"][0]["quote"] in actual
    service.terminal(c, "completed", "recipient_stop")
    assert service.result("owner", call["call_id"])["outcome"] == "completed"


async def test_completion_waits_for_independent_transcription(service, brief, tmp_path):
    call, worker = configured(service, brief, tmp_path)
    actual = "Jordan Lee can check in without Sam Rivera and must bring photo ID."
    pending = service.pending_decision(
        call["call_id"],
        "delayed-stt",
        json.dumps({"operation": "complete", "arguments": {"confirmation_quote": actual}}),
    )
    task = asyncio.create_task(worker.answer(service.call("owner", call["call_id"]), pending))
    await asyncio.sleep(0.03)
    assert not task.done() and not service.provider.updates
    service.transcripts[call["call_id"]] = [{"role": "user", "text": actual}]
    await task
    assert service.call("owner", call["call_id"])["conclusion"]["outcome"] == "completed"


async def test_unverified_quote_returns_observed_data_for_retry_without_success(
    service, brief, tmp_path
):
    call, worker = configured(service, brief, tmp_path)
    worker.confirmation_wait_seconds = 0.02
    actual = "Jordan Lee can check in without Sam Rivera and must bring photo ID."
    service.transcripts[call["call_id"]] = [{"role": "user", "text": actual}]
    for quote in ("Jordan Lee may check in without Sam.", actual):
        pending = service.pending_decision(
            call["call_id"],
            quote,
            json.dumps({"operation": "complete", "arguments": {"confirmation_quote": quote}}),
        )
        await worker.answer(service.call("owner", call["call_id"]), pending)
        output = json.loads(service.provider.updates[-1][1])
        current = service.call("owner", call["call_id"])
        if quote != actual:
            assert output["completion_recorded"] is False
            assert output["observed_recipient_text"] == actual
            assert current["state"] == "active" and current.get("conclusion") is None
        else:
            assert output["completion_recorded"] is True
            assert current["conclusion"]["outcome"] == "completed"


@pytest.mark.parametrize(
    "role,text,quote",
    [
        ("assistant", "Jordan Lee can check in.", "Jordan Lee can check in."),
        ("user", "What name should I add?", "Jordan Lee can check in."),
        ("user", "I have added Jordan Lee.", "I have added Jordan Lee."),
        ("user", "Jordan Lee can check in.", "Jordan Lee can check in."),
        (
            "user",
            "Jordan Lee can check in with Sam Rivera.",
            "Jordan Lee can check in with Sam Rivera.",
        ),
        ("user", "Jordan Lee cannot check in.", "Jordan Lee cannot check in."),
        ("user", "Jordan Lee can check in.", "!!!"),
    ],
)
def test_unsupported_completion_is_unresolved(service, brief, tmp_path, role, text, quote):
    call, worker = configured(service, brief, tmp_path)
    service.transcripts[call["call_id"]] = [{"role": role, "text": text}]
    with pytest.raises(Refused):
        worker.conclusion(call, quote)
    assert not service.call("owner", call["call_id"]).get("conclusion")


def test_voice_prompt_uses_prepared_facts_and_chooses_missing_fact_queries(
    service, brief, tmp_path
):
    call, _ = configured(service, brief, tmp_path)
    prompt = hotel_instructions(call)
    assert "Jordan Lee" in prompt and "choose your own search_trip_mail query" in prompt
    assert "calculate nights" in prompt and "read_trip_mail" in prompt
    # Verification/deadline metadata can legitimately contain these clock
    # substrings (for example 19:20 UTC); only task content must omit them.
    timestamps = [call["brief"]["deadline"]]
    for fact in call["brief"]["facts"]:
        timestamps.extend((fact["verified_at"], fact["expires_at"]))
    for timestamp in timestamps:
        prompt = prompt.replace(timestamp, "<timestamp>")
    assert "9:20" not in prompt and "9:15" not in prompt
