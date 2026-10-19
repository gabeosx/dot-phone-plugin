import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_reasoner import ReasoningAdvice
from dots_phone.models import Steer
from dots_phone.report_review import new_review_state, recipient_review_snapshot
from dots_phone.service import Refused, digest
from tests.test_call_reasoner import active


async def reviewed_pending(service, brief, tmp_path):
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    words = "Your available balance is 12,340 points."
    call = service.call("owner", cid)
    call["recipient_audio_turn"] = 1
    service.save(call)
    service.observe_transcript(
        cid,
        [
            {
                "role": "user",
                "text": words,
                "recipient_turn": 1,
                "recipient_item_id": "recipient-one",
            }
        ],
    )
    report = {
        "outcome": "completed",
        "evidence": [{"criterion_id": "balance", "text": words, "quote": words}],
        "unresolved_questions": [],
    }
    pending = service.pending_decision(
        cid,
        "finish-tool",
        json.dumps(
            {
                "operation": "complete",
                "arguments": {"report": report},
            }
        ),
    )
    service.local_context_worker.reasoner = SimpleNamespace(
        advise=AsyncMock(
            return_value=ReasoningAdvice(
                status="ready",
                next_step="Accept the report.",
                answer="",
                supporting_quote="",
                report_supported=True,
            ),
        )
    )
    return cid, pending, report


def bind_snapshot(service, cid):
    call = service.call("owner", cid)
    call["pending"]["report_review_snapshot_sha256"] = digest(
        recipient_review_snapshot(
            call,
            service.transcripts[cid],
        )
    )
    service.save(call)
    return call


def new_observation(service, cid, change):
    call = service.call("owner", cid)
    if change == "audio":
        call["recipient_audio_turn"] += 1
        service.save(call)
    else:
        service.observe_transcript(
            cid,
            [
                {
                    "role": "user",
                    "text": "Correction: your available balance is 12,300 points.",
                    "recipient_turn": 1,
                    "recipient_item_id": "recipient-one",
                }
            ],
        )


@pytest.mark.parametrize("change", ["audio", "asr"])
async def test_new_recipient_evidence_during_provider_delivery_cannot_commit_approved_report(
    service, brief, tmp_path, change
):
    cid, pending, _ = await reviewed_pending(service, brief, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def deliver(call, delegation, answer):
        assert call["pending"]["report_review_snapshot_sha256"]
        assert json.loads(answer)["result_recorded"] is True
        entered.set()
        await release.wait()

    service.provider.update = AsyncMock(side_effect=deliver)
    task = asyncio.create_task(
        service.local_context_worker.answer(
            service.call("owner", cid),
            pending,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        new_observation(service, cid, change)
        release.set()
        with pytest.raises(Refused, match="recipient evidence changed"):
            await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    fresh = service.call("owner", cid)
    assert not fresh.get("conclusion")
    assert fresh["state"] == "awaiting_decision"
    assert fresh["pending"]["request_id"] == pending["request_id"]


async def test_unchanged_approved_report_commits_through_production_service(
    service, brief, tmp_path
):
    cid, pending, report = await reviewed_pending(service, brief, tmp_path)
    observed = []

    async def deliver(call, delegation, answer):
        observed.append(call["pending"]["report_review_snapshot_sha256"])
        service.check_report_delivery(call)

    service.provider.update = AsyncMock(side_effect=deliver)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    fresh = service.call("owner", cid)
    assert len(observed) == 1
    assert fresh["state"] == "active" and fresh["pending"] is None
    assert fresh["conclusion"]["outcome"] == "completed"
    # Structural quote matching normalizes trailing punctuation, not its value.
    assert fresh["conclusion"]["confirmed_facts"][0]["quote"] == report["evidence"][0][
        "quote"
    ].removesuffix(".")


@pytest.mark.parametrize("change", ["unchanged", "audio", "asr", "during_playback_clear"])
async def test_pipecat_rechecks_report_snapshot_before_sending_final_tool_output(
    service, brief, tmp_path, change
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid, _, _ = await reviewed_pending(service, brief, tmp_path)
    reviewed = bind_snapshot(service, cid)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm._truncate_current_audio_response = AsyncMock()
    llm._send_tool_result = AsyncMock()
    llm.clear_playback = AsyncMock()
    params = SimpleNamespace(result_callback=AsyncMock())
    future = asyncio.get_running_loop().create_future()
    llm.decisions["finish-tool"] = (params, future)
    llm.finalizing_calls.add("finish-tool")
    if change == "during_playback_clear":

        async def changed(_):
            new_observation(service, cid, "asr")

        llm.clear_playback.side_effect = changed
    elif change != "unchanged":
        new_observation(service, cid, change)
    output = json.dumps({"result_recorded": True, "outcome": "completed"})
    if change == "unchanged":
        await llm.deliver_decision(reviewed, "finish-tool", output)
        llm._send_tool_result.assert_awaited_once()
        params.result_callback.assert_awaited_once()
        assert future.result() is True
    else:
        with pytest.raises(Refused, match="recipient evidence changed"):
            await llm.deliver_decision(reviewed, "finish-tool", output)
        llm._send_tool_result.assert_not_awaited()
        params.result_callback.assert_not_awaited()
        assert not future.done()


async def test_rejected_report_feedback_clears_previous_delivery_binding(service, brief, tmp_path):
    cid, pending, _ = await reviewed_pending(service, brief, tmp_path)
    bind_snapshot(service, cid)
    service.local_context_worker.reasoner.advise.return_value = ReasoningAdvice(
        status="unknown",
        next_step="Clarify the exact balance.",
        answer="",
        supporting_quote="",
        report_supported=False,
    )

    async def deliver(call, delegation, answer):
        assert "report_review_snapshot_sha256" not in call["pending"]
        assert json.loads(answer)["result_recorded"] is False

    service.provider.update = AsyncMock(side_effect=deliver)
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    fresh = service.call("owner", cid)
    assert not fresh.get("conclusion") and fresh["report_review_state"]["conflict"]
    service.provider.update.assert_awaited_once()


async def test_steering_clears_report_state_and_invalidates_its_delivery(service, brief, tmp_path):
    cid, _, report = await reviewed_pending(service, brief, tmp_path)
    call = bind_snapshot(service, cid)
    call["report_review_state"] = {**new_review_state(call, report), "conflict": True}
    service.save(call)
    await service.steer(
        "owner", Steer(call_id=cid, revision=call["revision"], instruction="Ask for clarification.")
    )
    fresh = service.call("owner", cid)
    assert fresh["revision"] == call["revision"] + 1
    assert fresh["state"] == "active" and fresh["pending"] is None
    assert "report_review_state" not in fresh
    assert not fresh.get("conclusion")
