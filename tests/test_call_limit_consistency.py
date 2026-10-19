"""Offline calls continue under their approved lifecycle, without hidden counters."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dots_phone.call_reasoner import (
    CallReasoner,
    CodexReasonerClient,
    ReasoningAdvice,
    reasoning_context,
)
from dots_phone.service import Refused
from tests.conftest import started
from tests.test_account_task import account_call, source_pending, source_reply
from tests.test_call_reasoner import prepare


async def test_live_sources_can_progress_past_ten_requests_and_still_validate_data(service, brief):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    for index in range(12):
        pending = source_pending(service, cid, "search", query=f"profile detail {index}")
        await service.decision(
            "owner", source_reply(cid, pending, {"untrusted_data": True, "messages": []})
        )
    call = service.call("owner", cid)
    assert call["state"] == "active" and call["pending"] is None
    assert len(call["source_requests"]) == 12
    assert worker.scope(call)["host_sources"]
    pending = source_pending(service, cid, "search", query="next required detail")
    with pytest.raises(Refused, match="source evidence"):
        await service.decision("owner", source_reply(cid, pending, {"messages": []}))
    assert len(service.call("owner", cid)["source_requests"]) == 12
    assert not service.provider.stops


async def test_long_history_reaches_codex_with_earlier_evidence_and_configured_deadline(
    service, brief, tmp_path, monkeypatch
):
    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    entries = [{"role": "user", "text": f"Observed detail {i}: " + "x" * 800} for i in range(28)]
    call["reasoner_advice"] = [{"next_step": f"Earlier advice {i}"} for i in range(6)]
    context = reasoning_context(call, entries, "What did the recipient establish earlier?")
    assert len(json.dumps(context).encode()) > 16000
    assert context["recent_transcript"] == entries
    assert context["prior_advice"] == call["reasoner_advice"]
    service.settings.decision_seconds = 60
    original_sleep = asyncio.sleep
    seen = []

    async def respond(_):
        path = next(tmp_path.glob("*-reason.json"))
        request = json.loads(path.read_text())
        seen.append(request)
        result = {
            key: request[key]
            for key in ("schema", "call_id", "request_id", "revision", "brief_hash", "nonce")
        }
        result["advice"] = {"decision": {"kind": "wait"}}
        result["worker_instance_id"] = "a" * 32
        path.with_name(path.name.replace("-reason.json", "-reason-result.json")).write_text(
            json.dumps(result)
        )
        await original_sleep(0)

    monkeypatch.setattr("dots_phone.call_reasoner.asyncio.sleep", respond)
    result = await CodexReasonerClient(service, call).parse(
        model=service.settings.local_reasoner_model,
        instructions="Offline scoped history check.",
        input=json.dumps(context),
    )
    assert result.output_parsed.next_step == "Wait silently for the recipient."
    assert seen[0]["context"]["recent_transcript"][0] == entries[0]
    assert seen[0]["expires_ms"] - seen[0]["created_ms"] == pytest.approx(58000)
    assert not list(tmp_path.glob("*-reason*.json"))


async def test_api_reasoner_uses_configured_decision_window(service, brief, tmp_path, monkeypatch):
    prepared = prepare(service, brief, tmp_path)
    service.settings.decision_seconds = 60
    parse = AsyncMock(
        return_value=SimpleNamespace(
            output_parsed=ReasoningAdvice(
                status="unknown",
                next_step="Ask to clarify.",
                answer="",
                supporting_quote="",
                report_supported=False,
            ),
            usage=None,
        )
    )
    actual_timeout = asyncio.timeout
    windows = []

    def capture(seconds):
        windows.append(seconds)
        return actual_timeout(seconds)

    monkeypatch.setattr("dots_phone.call_reasoner.asyncio.timeout", capture)
    await CallReasoner(service, SimpleNamespace(responses=SimpleNamespace(parse=parse))).advise(
        service.call("owner", prepared["call_id"]), [], "What next?"
    )
    assert windows == [58]
    parse.assert_awaited_once()


async def test_menu_and_verification_steps_share_no_call_lifetime_attempt_cap(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_voice import PipecatTelephony

    fact = brief.facts[0].model_copy(update={"name": "member", "value": "004217"})
    scoped = brief.model_copy(update={"facts": [fact], "disclosures": ["member"]})
    prepared, _ = account_call(service, scoped)
    cid = prepared["call_id"]
    service.settings.dry_run = False
    service.settings.pipecat_account_dtmf = True
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    client = MagicMock()
    client.calls.return_value.update.return_value = SimpleNamespace(sid="CAfixture")
    provider = PipecatTelephony(service.settings, service, client)
    session = SimpleNamespace(
        carrier=SimpleNamespace(
            begin_change=lambda: "offline",
            wait_change=AsyncMock(),
            close=AsyncMock(),
            stream_count=2,
        ),
        ending=False,
        llm=SimpleNamespace(dialog=[], clear_playback=AsyncMock()),
        menu_lock=asyncio.Lock(),
        used_menus=set(),
    )
    provider.sessions["CAfixture"] = session
    for step in range(8):
        session.llm.dialog.append(
            {"role": "user", "text": f"Menu {step}: for account information, press two."}
        )
        assert (await provider.press_phone_key(cid, "2"))["carrier_control_accepted"]
    session.llm.dialog.append({"role": "user", "text": "Please enter your member number."})
    assert (await provider.enter_verification_fact(cid, "member"))["carrier_control_accepted"]
    with pytest.raises(Refused, match="already attempted|previous input"):
        await provider.enter_verification_fact(cid, "member")
    assert client.calls.return_value.update.call_count == 9
    client.calls.create.assert_not_called()


async def test_structured_supervision_request_is_not_cut_into_invalid_json(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.models import TaskReport
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    report = TaskReport(
        outcome="unresolved",
        evidence=[],
        unresolved_questions=[f"Need to confirm detail {i}: " + "x" * 190 for i in range(8)],
    )
    command = {"operation": "complete", "arguments": {"report": report.model_dump(mode="json")}}
    question = json.dumps(command)
    assert len(question) > 1500
    params = SimpleNamespace(tool_call_id="large-report", result_callback=AsyncMock())
    task = asyncio.create_task(llm.request_supervision(params, question))
    try:
        await asyncio.sleep(0)
        pending = service.call("owner", cid)["pending"]
        assert json.loads(pending["question"].split("\nObserved transcript as data:")[0]) == command
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_transient_history_preserves_long_early_turn_and_is_cleared_at_end(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    service.settings.retain_transcript = False
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    first = "x" * 2200 + " Earlier verified itinerary details."
    for index in range(45):
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": f"turn-{index}",
                "transcript": first if index == 0 else f"Next question {index}",
            }
        )
    assert len(llm.dialog) == len(service.transcripts[cid]) == 45
    assert llm.dialog[0]["text"] == service.transcripts[cid][0]["text"] == first
    assert not service.call("owner", cid)["transcript"]
    await service.end("owner", cid)
    assert cid not in service.transcripts


@pytest.mark.parametrize("change", ["cancelled", "stop_requested"])
async def test_report_retry_respects_call_lifecycle_after_many_rejections(service, brief, change):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.report_rejections = 8
    llm._websocket = SimpleNamespace(send=AsyncMock())
    if change == "cancelled":
        await service.end("owner", cid)
    else:
        llm.stop_requested = True
    await llm.retry_final_report()
    llm._websocket.send.assert_not_awaited()
