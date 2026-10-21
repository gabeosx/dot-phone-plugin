"""A report reviewer never owns the lifetime of the phone connection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.call_task import task_tools
from dots_phone.models import Start
from dots_phone.post_call_review import review_input, reviewed_conclusion
from dots_phone.service import Refused, digest
from tests.test_call_reasoner import prepare


async def active(service, brief, tmp_path):
    p = prepare(service, brief, tmp_path)
    service.settings.dry_run = False
    service.start(
        "owner",
        Start(**{k: p[k] for k in ("call_id", "revision", "authorization_id")}),
        client_id="client",
    )
    await service.tick()
    return p["call_id"]


def report(text="Available balance is 184 points.", evidence_id=None):
    return {
        "outcome": "completed",
        "evidence": [
            {
                "criterion_id": "balance",
                "text": text,
                **(
                    {"evidence_id": evidence_id}
                    if evidence_id
                    else {"quote": "Incorrect citation."}
                ),
            }
        ],
        "unresolved_questions": [],
    }


def entries(text="Your available balance is one hundred eighty four points."):
    return [{"role": "user", "text": text, "recipient_turn": 1, "recipient_item_id": "observed"}]


def supported(context):
    return {
        "verdicts": [
            {
                "claim_index": 0,
                "status": "supported",
                "criterion_met": True,
                "evidence_id": context["recipient_evidence"][0]["id"],
                "reason": "The complete claim is supported.",
            }
        ]
    }


async def finish(service, cid, proposal, observations):
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.dialog = list(observations)
    service.observe_transcript(cid, observations)
    llm.finish_conversation(proposal)
    await llm.stop_task
    return llm


async def test_carrier_closes_before_slow_review_and_citation_is_repaired_off_line(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    waiting, release = asyncio.Event(), asyncio.Event()

    async def review(client, context):
        assert service.provider.stops
        assert service.call("owner", cid)["state"] == "completed"
        waiting.set()
        await release.wait()
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    llm = await finish(service, cid, report(), entries())
    assert llm.stop_requested and len(service.provider.stops) == 1
    assert service.result("owner", cid)["report_review_status"] == "pending"
    service.post_call_reviews.dispatch()
    assert not service.post_call_reviews.tasks  # Socket closure has not been confirmed.
    service.confirm_voice_closed(cid, 1000)
    service.post_call_reviews.dispatch()
    task = service.post_call_reviews.tasks[cid]
    await waiting.wait()
    assert service.result("owner", cid)["outcome"] == "unresolved"
    assert len(service.provider.dials) == 1
    release.set()
    await task
    final = service.result("owner", cid)
    assert final["outcome"] == "completed" and final["report_review_status"] == "reviewed"
    assert final["termination_reason"] == "conversation_finished"
    assert final["confirmed_facts"][0]["quote"] == entries()[0]["text"]
    assert len(service.provider.dials) == 1 and len(service.provider.stops) == 1


async def test_finish_tool_does_not_request_another_live_reasoning_turn(service, brief, tmp_path):
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid = await active(service, brief, tmp_path)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.dialog = entries()
    llm.request_supervision = AsyncMock(side_effect=AssertionError("No live report review"))
    callback = AsyncMock()
    tool = next(
        t for t in task_tools(llm, reasoner=True) if getattr(t, "name", None) == "finish_call"
    )
    await tool.handler(
        SimpleNamespace(arguments=report(), result_callback=callback, tool_call_id="final")
    )
    await llm.stop_task
    llm.request_supervision.assert_not_awaited()
    assert callback.call_args.kwargs["properties"].run_llm is False
    assert service.call("owner", cid)["state"] == "completed"


async def test_optional_closing_audio_cannot_keep_the_line_open_after_finish_intent(
    service, brief, tmp_path
):
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid = await active(service, brief, tmp_path)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.dialog = entries()
    llm.caller_speaking = True
    llm.finish_conversation(report())
    await llm.stop_task
    assert service.call("owner", cid)["state"] == "completed"
    assert len(service.provider.stops) == 1


async def test_genuine_balance_conflict_stays_unresolved_without_reopening(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report("Available balance is 180 points."), entries())
    service.confirm_voice_closed(cid, 1000)
    monkeypatch.setattr(
        CodexReasonerClient,
        "review_closed",
        AsyncMock(
            return_value={
                "verdicts": [
                    {
                        "claim_index": 0,
                        "status": "conflict",
                        "criterion_met": False,
                        "evidence_id": "",
                        "reason": "Proposed 180 differs from the observed 184; no clarification.",
                    }
                ]
            }
        ),
    )
    await service.post_call_reviews.run("owner", cid)
    result = service.result("owner", cid)
    assert result["outcome"] == "unresolved" and result["confirmed_facts"] == []
    assert "180 differs" in result["unresolved_questions"][0]
    assert service.call("owner", cid)["final_report_review"]["report"]["evidence"][0][
        "text"
    ].endswith("180 points.")
    assert len(service.provider.dials) == 1


@pytest.mark.parametrize("failure", ["unavailable", "interrupted"])
async def test_failed_or_interrupted_review_never_confirms_or_repeats_an_attempt(
    service, brief, tmp_path, monkeypatch, failure
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)
    reviewer = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(CodexReasonerClient, "review_closed", reviewer)
    if failure == "interrupted":
        call = service.call("owner", cid)
        call["final_report_review"].update(state="running", attempts=1)
        service.save(call)
    await service.post_call_reviews.run("owner", cid)
    assert reviewer.await_count == (failure == "unavailable")
    final = service.result("owner", cid)
    assert final["outcome"] == "unresolved" and not final["confirmed_facts"]
    assert final["report_review_status"] == "unverified"
    assert len(service.provider.stops) == 1 and len(service.provider.dials) == 1


async def test_partial_report_preserves_supported_balance_without_making_behavior_a_fact(
    service, brief, tmp_path
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    # Represents a frozen brief with both business and diagnostic criteria.
    call["brief"]["completion_criteria"].append(
        {"id": "behavior", "required": True, "description": "Observe behavior"}
    )
    call["brief_hash"] = digest(call["brief"])
    service.save(call)
    p = report()
    p["evidence"].append(
        {
            "criterion_id": "behavior",
            "text": "Verification succeeded and caller declined text.",
            "quote": "Anything else?",
        }
    )
    service.post_call_reviews.enqueue(call, p, entries())
    frozen = service.call("owner", cid)
    verdict = supported(review_input(frozen))
    verdict["verdicts"].append(
        {
            "claim_index": 1,
            "status": "unverified",
            "criterion_met": False,
            "evidence_id": "",
            "reason": "No single recipient quote establishes caller behavior.",
        }
    )
    conclusion = reviewed_conclusion(frozen, verdict)
    assert conclusion.outcome == "unresolved"
    assert len(conclusion.confirmed_facts) == 1
    assert conclusion.confirmed_facts[0].text == "Available balance is 184 points."


@pytest.mark.parametrize(
    "corruption", ["foreign_evidence", "duplicate_index", "changed_proposal", "changed_scope"]
)
async def test_post_call_review_remains_bound_to_original_claims_scope_and_real_evidence(
    service, brief, tmp_path, corruption
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    service.post_call_reviews.enqueue(call, report(), entries())
    frozen = service.call("owner", cid)
    verdict = supported(review_input(frozen))
    if corruption == "foreign_evidence":
        verdict["verdicts"][0]["evidence_id"] = "f" * 16
    elif corruption == "duplicate_index":
        verdict["verdicts"] *= 2
    elif corruption == "changed_proposal":
        frozen["final_report_review"]["report"]["evidence"][0]["text"] = "Changed value"
    else:
        frozen["revision"] += 1
    with pytest.raises(Refused):
        reviewed_conclusion(frozen, verdict)


async def test_a_true_interim_fact_cannot_complete_an_unmet_business_goal(service, brief, tmp_path):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    service.post_call_reviews.enqueue(
        call,
        report("Pending balance is 184 points."),
        entries("Your pending balance is 184 points."),
    )
    frozen = service.call("owner", cid)
    verdict = supported(review_input(frozen))
    verdict["verdicts"][0]["criterion_met"] = False
    conclusion = reviewed_conclusion(frozen, verdict)
    assert conclusion.outcome == "unresolved" and len(conclusion.confirmed_facts) == 1
    assert "Unverified required criterion: balance" in conclusion.unresolved_questions


async def test_a_supported_refusal_can_finish_declined_without_inventing_the_balance(
    service, brief, tmp_path
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    proposal = report("The recipient declined to disclose the balance.")
    proposal["outcome"] = "declined"
    service.post_call_reviews.enqueue(call, proposal, entries("I cannot disclose your balance."))
    frozen = service.call("owner", cid)
    verdict = supported(review_input(frozen))
    verdict["verdicts"][0]["criterion_met"] = False
    assert reviewed_conclusion(frozen, verdict).outcome == "declined"


async def test_retention_disabled_keeps_review_observations_out_of_persistent_queue(
    service, brief, tmp_path
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    call.setdefault("evidence_policy", {})["transcript"] = False
    service.post_call_reviews.enqueue(call, report(), entries())
    persisted = service.call("owner", cid)
    assert "entries" not in persisted["final_report_review"]
    assert service.post_call_reviews.entries[cid] == entries()


async def test_later_correction_during_review_cannot_commit_stale_balance(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)

    async def correction(client, context):
        call = service.call("owner", cid)
        call["transcript"].append(
            {
                "role": "user",
                "text": "Correction, that is pending, not available.",
                "recipient_turn": 2,
                "recipient_item_id": "later",
            }
        )
        service.save(call)
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", correction)
    await service.post_call_reviews.run("owner", cid)
    assert not service.result("owner", cid)["confirmed_facts"]
    assert service.result("owner", cid)["report_review_status"] == "unverified"


async def test_closed_review_transport_uses_only_the_no_tools_protocol(service, brief, tmp_path):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)
    client = CodexReasonerClient(service, service.call("owner", cid))
    context = review_input(client.call)
    client.request = AsyncMock(return_value={"advice": supported(context), "tool_calls": 0})
    assert await client.review_closed(context) == supported(context)
    assert client.request.call_args.args == ("review_closed_call",)
    assert client.request.call_args.kwargs["output_schema"]["properties"].keys() == {"verdicts"}
    client.request.return_value["tool_calls"] = 1
    with pytest.raises(Refused, match="cannot retrieve"):
        await client.review_closed(context)


async def test_a_second_call_waits_before_dialing_while_closed_review_finishes(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)
    release = asyncio.Event()

    async def slow(client, context):
        await release.wait()
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", slow)
    service.post_call_reviews.dispatch()
    task = service.post_call_reviews.tasks[cid]
    second = await active(service, brief, tmp_path)
    assert service.call("owner", second)["state"] == "queued"
    assert len(service.provider.dials) == 1
    release.set()
    await task
    await asyncio.sleep(0)
    await service.tick()
    assert len(service.provider.dials) == 2
    await service.end("owner", second)


async def test_a_second_call_waits_for_delayed_voice_closure_before_prewarming(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    assert service.call("owner", cid)["final_report_review"]["state"] == "pending"
    assert service.call("owner", cid)["usage"].get("voice_closed_confirmed") is not True

    release = asyncio.Event()

    async def slow(client, context):
        await release.wait()
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", slow)
    second = await active(service, brief, tmp_path)
    assert service.call("owner", second)["state"] == "queued"
    assert len(service.provider.dials) == 1
    service.local_context_worker.before_dial.assert_not_awaited()
    assert not service.post_call_reviews.tasks

    service.confirm_voice_closed(cid, 1000)
    await service.tick()
    task = service.post_call_reviews.tasks[cid]
    assert service.call("owner", second)["state"] == "queued"
    service.local_context_worker.before_dial.assert_not_awaited()
    release.set()
    await task
    await asyncio.sleep(0)
    await service.tick()
    assert service.call("owner", second)["state"] == "active"
    service.local_context_worker.before_dial.assert_awaited_once()
    assert len(service.provider.dials) == 2
    await service.end("owner", second)


async def test_old_pending_reviews_wait_for_an_existing_active_call(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    second = await active(service, brief, tmp_path)
    # Reproduce state persisted by the old scheduler before it gated on pending
    # reports: a new call became active before the old voice socket closed.
    call = service.call("owner", second)
    call.update(
        state="active",
        provider_sid="CA" + "2" * 32,
        dial_started=service.call("owner", cid)["dial_started"],
    )
    service.save(call)
    service.confirm_voice_closed(cid, 1000)
    review = AsyncMock(side_effect=lambda context: supported(context))

    async def review_closed(client, context):
        return await review(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review_closed)
    await service.tick()
    assert not service.post_call_reviews.tasks
    review.assert_not_awaited()

    await service.end("owner", second)
    service.post_call_reviews.dispatch()
    await service.post_call_reviews.tasks[cid]
    review.assert_awaited_once()


async def test_restart_before_voice_close_does_not_strand_the_next_call(service, brief, tmp_path):
    from dots_phone.service import PhoneService
    from tests.conftest import FakeProvider

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    assert service.call("owner", cid)["final_report_review"]["state"] == "pending"
    restarted = PhoneService(service.settings, service.store, FakeProvider())
    await restarted.recover()

    old = restarted.call("owner", cid)
    assert old["final_report_review"]["state"] == "unverified"
    assert old["conclusion"]["outcome"] == "unresolved"
    assert "restarted" in old["conclusion"]["unresolved_questions"][0]
    assert old["usage"].get("voice_closed_confirmed") is not True
    assert not restarted.post_call_reviews.tasks
    second = await active(restarted, brief, tmp_path)
    assert restarted.call("owner", second)["state"] == "active"
    assert len(restarted.provider.dials) == 1
    await restarted.end("owner", second)


async def test_a_stale_review_cannot_overwrite_a_newer_scope_result(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)

    async def changed(client, context):
        call = service.call("owner", cid)
        call["revision"] += 1
        call["result"] = {
            "outcome": "unresolved",
            "confirmed_facts": [],
            "unresolved_questions": ["New scope"],
            "termination_reason": "conversation_finished",
        }
        service.save(call)
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", changed)
    await service.post_call_reviews.run("owner", cid)
    assert service.result("owner", cid)["unresolved_questions"] == ["New scope"]
    assert service.result("owner", cid)["report_review_status"] == "stale"


def attach_recipient_archive(service, cid):
    import base64
    import json

    from dots_phone.call_evidence import CallRecording

    clock = [0.0]
    recording = CallRecording(service, service.call("owner", cid), clock=lambda: clock[0])
    recording.observe(
        json.dumps(
            {"event": "media", "media": {"payload": base64.b64encode(bytes([0]) * 160).decode()}}
        ),
        "recipient",
    )
    clock[0] = 1.0
    recording.finish()


def local_recognition(service, tmp_path, monkeypatch, texts):
    from dots_phone import audio_evidence

    directory = tmp_path / "cached-small.en"
    directory.mkdir(exist_ok=True)
    (directory / "model.bin").write_bytes(b"primary test fixture weights")
    service.settings.post_call_audio_model_directory = directory
    monkeypatch.setattr(
        audio_evidence,
        "transcribe_recipient",
        lambda *_: [
            {"start_ms": index * 100, "end_ms": (index + 1) * 100, "text": text}
            for index, text in enumerate(texts)
        ],
    )


async def test_competing_transcriptions_reach_reasoner_without_rewriting_either_or_the_claim(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    native = entries("Your current available balance is 12,300 points.")
    proposal = report("Available balance is 12,340 points.")
    await finish(service, cid, proposal, native)
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    local_recognition(
        service,
        tmp_path,
        monkeypatch,
        ["Your current available balance is 12,340 points; pending points are excluded."],
    )

    async def review(_client, context):
        catalog = context["recipient_evidence"]
        assert catalog[0]["origin"] == "realtime_transcription"
        assert "12,300" in catalog[0]["text"]
        independent = next(
            e for e in catalog if e["origin"] == "independent_local_audio_transcription"
        )
        assert "12,340" in independent["text"]
        assert independent["provenance_ref"] == "audio_observation"
        assert context["audio_observation"]["source"]["recipient_pcm_sha256"]
        assert context["audio_observation"]["source"]["recording_digest"]
        assert context["proposed_report"] == proposal
        return {
            "verdicts": [
                {
                    "claim_index": 0,
                    "status": "supported",
                    "criterion_met": True,
                    "evidence_id": independent["id"],
                    "reason": "The complete independent observation supports the original interpretation; native ASR differs.",
                }
            ]
        }

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    await service.post_call_reviews.run("owner", cid)
    call = service.call("owner", cid)
    assert call["conclusion"]["outcome"] == "completed"
    assert call["conclusion"]["confirmed_facts"][0]["text"] == proposal["evidence"][0]["text"]
    assert "12,340" in call["conclusion"]["confirmed_facts"][0]["quote"]
    assert call["transcript"] == native
    assert call["final_report_review"]["report"] == proposal
    assert call["final_report_review"]["audio_observation"]["segments"]
    assert len(service.provider.stops) == 1


async def test_actual_later_correction_and_pending_status_remain_visible_to_reasoner(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    local_recognition(
        service,
        tmp_path,
        monkeypatch,
        [
            "Your available balance is 184 points.",
            "Correction: 184 is pending. Your available balance is zero.",
        ],
    )

    async def review(_client, context):
        observed = [
            e
            for e in context["recipient_evidence"]
            if e["origin"] == "independent_local_audio_transcription"
        ]
        assert observed[0]["start_ms"] < observed[1]["start_ms"]
        assert "pending" in observed[1]["text"]
        return {
            "verdicts": [
                {
                    "claim_index": 0,
                    "status": "conflict",
                    "criterion_met": False,
                    "evidence_id": "",
                    "reason": "A later recipient correction identifies 184 as pending, not available.",
                }
            ]
        }

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    await service.post_call_reviews.run("owner", cid)
    result = service.result("owner", cid)
    assert result["outcome"] == "unresolved" and result["confirmed_facts"] == []


async def test_changed_encrypted_audio_cannot_commit_a_stale_review(
    service, brief, tmp_path, monkeypatch
):
    import base64

    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    local_recognition(service, tmp_path, monkeypatch, [entries()[0]["text"]])

    async def change_audio(_client, context):
        chunk = service.store.get("call_audio", f"{cid}:000000", "owner")
        chunk["packets"][0]["payload"] = base64.b64encode(bytes([1]) * 160).decode()
        service.store.put("call_audio", f"{cid}:000000", "owner", chunk)
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", change_audio)
    await service.post_call_reviews.run("owner", cid)
    result = service.result("owner", cid)
    assert result["report_review_status"] == "unverified"
    assert result["confirmed_facts"] == []


async def test_transcript_retention_disabled_keeps_full_local_observations_transient(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    call["evidence_policy"]["transcript"] = False
    service.save(call)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    local_recognition(
        service,
        tmp_path,
        monkeypatch,
        [entries()[0]["text"], "Private unrelated phrase that must not be archived."],
    )
    monkeypatch.setattr(
        CodexReasonerClient,
        "review_closed",
        AsyncMock(side_effect=lambda context: supported(context)),
    )
    await service.post_call_reviews.run("owner", cid)
    call = service.call("owner", cid)
    assert call["transcript"] == []
    assert "entries" not in call["final_report_review"]
    assert "segments" not in call["final_report_review"]["audio_observation"]
    assert "Private unrelated phrase" not in str(call)
    assert call["final_report_review"]["audio_observation"]["source"]["recording_digest"]
    assert call["conclusion"]["confirmed_facts"]  # Existing result quotes are still permitted.
    assert cid not in service.post_call_reviews.entries


async def test_missing_audio_is_labelled_and_does_not_veto_supported_native_evidence(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone import audio_evidence
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)
    local_recognition(service, tmp_path, monkeypatch, [])
    monkeypatch.setattr(audio_evidence, "RECORDING_WAIT_SECONDS", 0.01)

    async def review(_client, context):
        assert context["audio_observation"] == {
            "status": "unavailable",
            "reason": "recording_finalization_unavailable",
        }
        assert len(context["recipient_evidence"]) == 1
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    await service.post_call_reviews.run("owner", cid)
    assert service.result("owner", cid)["outcome"] == "completed"


async def test_entire_review_context_is_frozen_including_criterion_wording(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    service.confirm_voice_closed(cid, 1000)

    async def change_criterion(_client, context):
        call = service.call("owner", cid)
        call["brief"]["completion_criteria"][0]["description"] = "Changed requested meaning"
        service.save(call)
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", change_criterion)
    await service.post_call_reviews.run("owner", cid)
    assert service.result("owner", cid)["confirmed_facts"] == []


async def test_repeated_native_statement_after_a_correction_retains_its_position(
    service, brief, tmp_path
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    repeated = entries()[0]
    observations = [repeated, {"role": "user", "text": "Correction: zero points."}, repeated]
    service.post_call_reviews.enqueue(call, report(), observations)
    catalog = review_input(service.call("owner", cid))["recipient_evidence"]
    assert len(catalog) == 3
    assert [e["observation_index"] for e in catalog] == [0, 1, 2]
    assert catalog[0]["text"] == catalog[2]["text"]
    assert catalog[0]["id"] != catalog[2]["id"]


async def test_late_native_transcript_keeps_audio_order_distinct_from_completion_order(
    service, brief, tmp_path
):
    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    observations = [
        {
            "role": "user",
            "text": "Correction: your available balance is zero.",
            "recipient_turn": 2,
            "recipient_item_id": "second-audio",
        },
        {
            "role": "user",
            "text": "Your available balance is 184 points.",
            "recipient_turn": 1,
            "recipient_item_id": "first-audio-late-transcription",
        },
    ]
    service.post_call_reviews.enqueue(call, report(), observations)
    catalog = review_input(service.call("owner", cid))["recipient_evidence"]
    assert [e["observation_index"] for e in catalog] == [0, 1]
    assert [e["recipient_turn"] for e in catalog] == [2, 1]
    assert all(e["sequence_basis"] == "transcription_completion_order" for e in catalog)


async def test_a_fixed_claim_can_cite_conditions_split_across_adjacent_audio_segments(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    proposal = report("Available reimbursement is $18.05, excluding pending adjustments.")
    await finish(
        service, cid, proposal, entries("The available reimbursement is eighteen dollars.")
    )
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    local_recognition(
        service,
        tmp_path,
        monkeypatch,
        [
            "Your available reimbursement is eighteen dollars",
            "and five cents.",
            "Pending adjustments are excluded.",
        ],
    )

    async def review(_client, context):
        independent = [
            item
            for item in context["recipient_evidence"]
            if item["origin"] == "independent_local_audio_transcription"
        ]
        assert len([item for item in independent if "excerpt_kind" not in item]) == 3
        window = next(item for item in independent if len(item.get("source_evidence_ids", [])) == 3)
        assert len(window["text"]) <= 240
        assert context["proposed_report"] == proposal
        return {
            "verdicts": [
                {
                    "claim_index": 0,
                    "status": "supported",
                    "criterion_met": True,
                    "evidence_id": window["id"],
                    "reason": "The contiguous observation contains the amount and its conditions.",
                }
            ]
        }

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    await service.post_call_reviews.run("owner", cid)
    call = service.call("owner", cid)
    assert call["conclusion"]["outcome"] == "completed"
    assert call["conclusion"]["confirmed_facts"][0]["text"] == proposal["evidence"][0]["text"]
    assert "five cents" in call["conclusion"]["confirmed_facts"][0]["quote"]
    assert "Pending adjustments are excluded" in call["conclusion"]["confirmed_facts"][0]["quote"]


def audio_observation(texts):
    from dots_phone.audio_evidence import RECOGNIZER

    return {
        "status": "available",
        "source": {
            "recording_digest": "a" * 64,
            "recipient_pcm_sha256": "b" * 64,
            "sample_rate_hz": 8000,
            "duration_ms": len(texts) * 3000,
            "boundary": "recipient_audio_received_from_carrier",
        },
        "recognizer": dict(RECOGNIZER),
        "segments": [
            {"start_ms": index * 3000, "end_ms": (index + 1) * 3000, "text": text}
            for index, text in enumerate(texts)
        ],
    }


async def test_compact_context_losslessly_references_full_audio_provenance_and_window_parts(
    service, brief, tmp_path
):
    from dots_phone.audio_evidence import audio_catalog

    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    service.post_call_reviews.enqueue(call, report(), entries())
    observation = audio_observation(
        [
            "Your available balance is 184 points.",
            "Correction: that is pending.",
            "The available balance is zero.",
        ]
    )
    canonical = audio_catalog(observation)
    context = review_input(service.call("owner", cid), audio_observation=observation)
    compact = [
        item
        for item in context["recipient_evidence"]
        if item["origin"] == "independent_local_audio_transcription"
    ]
    by_id = {item["id"]: item for item in compact}
    restored = []
    for item in compact:
        original = dict(item)
        assert original.pop("provenance_ref") == "audio_observation"
        source = context["audio_observation"]["source"]
        original.update(
            source_digest=source["recording_digest"],
            audio_sha256=source["recipient_pcm_sha256"],
            recognizer=context["audio_observation"]["recognizer"],
            timing_basis=context["audio_observation"]["timing_basis"],
        )
        if "segment_parts_ref" in original:
            assert original.pop("segment_parts_ref") == "source_evidence_ids"
            original["segment_parts"] = [
                {
                    "segment_index": by_id[reference]["segment_index"],
                    "segment_part": by_id[reference]["segment_part"],
                }
                for reference in original["source_evidence_ids"]
            ]
        restored.append(original)
    assert restored == canonical
    assert [item["id"] for item in compact] == [item["id"] for item in canonical]
    assert [item["text"] for item in compact] == [item["text"] for item in canonical]


async def test_fifty_audio_and_native_segments_fit_complete_reasoner_wire_request(
    service, brief, tmp_path
):
    import json

    from openai.lib._pydantic import to_strict_json_schema

    from dots_phone.post_call_review import REVIEW_PROMPT, ClosedCallReview

    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    texts = [
        f"The booking item number {index} remains confirmed for Tuesday, including its stated conditions."
        for index in range(50)
    ]
    observations = [
        {
            "role": "user",
            "text": text,
            "recipient_turn": index + 1,
            "recipient_item_id": f"native-audio-item-{index}",
        }
        for index, text in enumerate(texts)
    ]
    service.post_call_reviews.enqueue(call, report(), observations)
    context = review_input(service.call("owner", cid), audio_observation=audio_observation(texts))
    assert len(context["recipient_evidence"]) == 149  # 50 native, 50 local, 49 contiguous windows.
    # Actual compact JSON transport includes the complete prompt and strict
    # output schema, plus conservative allowance for its binding fields.
    wire = {
        "schema": 1,
        "operation": "review_closed_call",
        "call_id": cid,
        "request_id": "r" * 32,
        "revision": 1,
        "brief_hash": "a" * 64,
        "nonce": "n" * 64,
        "created_ms": 1792373499505,
        "expires_ms": 1792373554505,
        "model": "gpt-6-sol",
        "effort": "medium",
        "instructions": REVIEW_PROMPT,
        "context": context,
        "output_schema": to_strict_json_schema(ClosedCallReview),
    }
    assert len(json.dumps(wire, separators=(",", ":")).encode()) + 1000 < 64000


def dual_local_recognition(service, tmp_path, monkeypatch, primary_text, confirmation_text):
    from dots_phone import audio_evidence

    local_recognition(service, tmp_path, monkeypatch, [])
    primary = service.settings.post_call_audio_model_directory
    confirmation = tmp_path / "cached-medium.en"
    confirmation.mkdir(exist_ok=True)
    (confirmation / "model.bin").write_bytes(b"confirmation test fixture weights")
    service.settings.post_call_audio_confirmation_model_directory = confirmation

    def recognize(_pcm, directory):
        text = primary_text if directory == primary else confirmation_text
        if isinstance(text, Exception):
            raise text
        return [{"start_ms": 0, "end_ms": 20, "text": text}]

    monkeypatch.setattr(audio_evidence, "transcribe_recipient", recognize)


async def test_wrong_local_observations_and_correct_native_reach_reasoner_unchanged(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    dual_local_recognition(
        service,
        tmp_path,
        monkeypatch,
        "Your available balance is 999 points.",
        "Your available balance is 888 points.",
    )

    async def review(_client, context):
        assert context["recipient_evidence"][0]["text"] == entries()[0]["text"]
        local = [
            item
            for item in context["recipient_evidence"]
            if item["origin"] == "independent_local_audio_transcription"
        ]
        assert [item["text"] for item in local] == [
            "Your available balance is 999 points.",
            "Your available balance is 888 points.",
        ]
        assert [item["provenance_ref"] for item in local] == [
            "audio_observation",
            "audio_observation.additional_observations[0]",
        ]
        primary = context["audio_observation"]
        second = primary["additional_observations"][0]
        assert primary["source"] == second["source"]
        assert primary["recognizer"]["model"] == "small.en"
        assert second["recognizer"]["model"] == "medium.en"
        assert context["proposed_report"]["evidence"][0]["text"] == report()["evidence"][0]["text"]
        return supported(
            context
        )  # The model can support native evidence; no local vote or preference.

    monkeypatch.setattr(CodexReasonerClient, "review_closed", review)
    await service.post_call_reviews.run("owner", cid)
    result = service.result("owner", cid)
    assert result["outcome"] == "completed"
    assert result["confirmed_facts"][0]["quote"] == entries()[0]["text"]


async def test_nested_confirmation_transcript_stays_transient_when_retention_disabled(
    service, brief, tmp_path, monkeypatch
):
    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    call = service.call("owner", cid)
    call["evidence_policy"]["transcript"] = False
    service.save(call)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    dual_local_recognition(
        service,
        tmp_path,
        monkeypatch,
        "PRIVATE PRIMARY UNRELATED WORDS.",
        "PRIVATE CONFIRMATION UNRELATED WORDS.",
    )
    monkeypatch.setattr(CodexReasonerClient, "review_closed", AsyncMock(side_effect=supported))
    await service.post_call_reviews.run("owner", cid)
    call = service.call("owner", cid)
    retained = call["final_report_review"]["audio_observation"]
    assert "segments" not in retained
    assert "segments" not in retained["additional_observations"][0]
    assert "PRIVATE" not in str(call)
    assert retained["source"] == retained["additional_observations"][0]["source"]
    assert call["conclusion"][
        "confirmed_facts"
    ]  # Only selected result quotes persist, per existing policy.


async def test_confirmation_source_is_checked_even_when_primary_decoder_failed(
    service, brief, tmp_path, monkeypatch
):
    import base64

    from dots_phone.call_reasoner import CodexReasonerClient

    cid = await active(service, brief, tmp_path)
    await finish(service, cid, report(), entries())
    attach_recipient_archive(service, cid)
    service.confirm_voice_closed(cid, 1000)
    dual_local_recognition(
        service, tmp_path, monkeypatch, RuntimeError("failed"), entries()[0]["text"]
    )

    async def change_audio(_client, context):
        assert context["audio_observation"]["status"] == "unavailable"
        assert context["audio_observation"]["additional_observations"][0]["status"] == "available"
        chunk = service.store.get("call_audio", f"{cid}:000000", "owner")
        chunk["packets"][0]["payload"] = base64.b64encode(bytes([1]) * 160).decode()
        service.store.put("call_audio", f"{cid}:000000", "owner", chunk)
        return supported(context)

    monkeypatch.setattr(CodexReasonerClient, "review_closed", change_audio)
    await service.post_call_reviews.run("owner", cid)
    result = service.result("owner", cid)
    assert result["report_review_status"] == "unverified"
    assert result["confirmed_facts"] == []
