import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_reasoner import (
    AutonomousAccountWorker,
    CallReasoner,
    CodexReasonerClient,
    ReasoningAdvice,
    reasoning_context,
)
from dots_phone.models import Brief, Start
from dots_phone.service import Refused


def prepare(service, brief, tmp_path, *, source_verification=False, keypad=False):
    service.settings.account_tasks = True
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    service.settings.local_reasoner_enabled = True
    service.settings.local_reasoner_directory = tmp_path
    service.settings.call_authorization_mode = "connected_owner"
    (tmp_path / "reasoner-ready.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "serving": True,
                "heartbeat_ms": time.time() * 1000,
                "prewarm_supported": True,
                "decision_protocol": 1,
                "post_call_review_protocol": 1,
                "source_selection_protocol": 1,
                "instance_id": "a" * 32,
            }
        )
    )
    service.local_context_worker = AutonomousAccountWorker(service)
    # Most tests exercise the live decision path with a synthetic carrier.
    # Preparation transport and its before-dial ordering are tested below.
    service.local_context_worker.before_dial = AsyncMock()
    data = brief.model_dump(mode="json")
    data.update(
        actions=[
            "read_account_information",
            "record_call_result",
            *(["navigate_phone_menu"] if keypad else []),
            *(["read_call_sources", "verify_account_identity"] if source_verification else []),
        ],
        completion_criteria=[{"id": "balance", "description": "Hear available balance"}],
    )
    return service.prepare("owner", Brief.model_validate(data), client_id="client")


@pytest.mark.parametrize("outcome", ["ready", "timeout", "cancelled"])
async def test_production_prewarm_finishes_before_carrier_and_cancellation_prevents_dial(
    service, brief, tmp_path, monkeypatch, outcome
):
    prepared = prepare(service, brief, tmp_path)
    worker = service.local_context_worker
    worker.before_dial = AutonomousAccountWorker.before_dial.__get__(worker)
    service.settings.dry_run = False
    service.start(
        "owner",
        Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
        client_id="client",
    )

    async def warm(client):
        assert not service.provider.dials
        assert service.call("owner", prepared["call_id"])["state"] == "queued"
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "cancelled":
            await service.end("owner", prepared["call_id"])
        return {
            "prepared": True,
            "worker_instance_id": "a" * 32,
            "lifecycle": {"thread_id": "one-thread", "model_turn_ms": 123},
            "source_capabilities": {
                "enabled": False,
                "selection": "none",
                "tool_names": [],
                "connector_ids": [],
            },
        }

    monkeypatch.setattr(CodexReasonerClient, "prepare", warm)
    await service.tick()
    state = service.call("owner", prepared["call_id"])
    if outcome == "ready":
        assert service.provider.dials == [prepared["call_id"]]
        assert state["usage"]["reasoner_preparation"]["prepared"] is True
        assert state["usage"]["reasoner_preparation"]["source_capabilities"]["selection"] == "none"
    else:
        assert not service.provider.dials
        assert state["state"] == ("failed" if outcome == "timeout" else "cancelled")


async def test_prewarm_transport_binds_worker_scope_and_effort(
    service, brief, tmp_path, monkeypatch
):
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    service.settings.dry_run = False
    service.settings.local_reasoner_effort = "low"
    service.start(
        "owner",
        Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
        client_id="client",
    )
    original_sleep = asyncio.sleep
    seen = []

    async def deliver(_):
        request_path = next(tmp_path.glob("*-reason.json"))
        request = json.loads(request_path.read_text())
        seen.append(request)
        result = {
            key: request[key]
            for key in ("schema", "call_id", "request_id", "revision", "brief_hash", "nonce")
        }
        result.update(
            prepared=True,
            worker_instance_id="a" * 32,
            lifecycle={"thread_id": "one-thread"},
            source_capabilities={
                "enabled": True,
                "selection": "inherited",
                "tool_names": ["connected_plugin.read_only_item_" + str(i) for i in range(233)],
                "connector_ids": ["connected-plugin"],
            },
        )
        request_path.with_name(
            request_path.name.replace("-reason.json", "-reason-result.json")
        ).write_text(json.dumps(result))
        await original_sleep(0)

    monkeypatch.setattr("dots_phone.call_reasoner.asyncio.sleep", deliver)
    result = await CodexReasonerClient(
        service, service.call("owner", prepared["call_id"])
    ).prepare()
    assert result["prepared"] is True
    assert len(result["source_capabilities"]["tool_names"]) == 233
    assert seen[0]["operation"] == "prepare_call" and seen[0]["effort"] == "low"
    assert "decision" in seen[0]["output_schema"]["properties"]
    assert not list(tmp_path.glob("*-reason*.json"))


async def test_old_worker_cannot_silently_skip_prewarming(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    (tmp_path / "reasoner-ready.json").write_text(
        json.dumps({"schema": 1, "serving": True, "heartbeat_ms": time.time() * 1000})
    )
    with pytest.raises(Refused, match="prewarming support"):
        await CodexReasonerClient(service, service.call("owner", prepared["call_id"])).prepare()


async def test_old_prewarm_worker_requires_compact_protocol_before_dial(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    ready_path = tmp_path / "reasoner-ready.json"
    ready = json.loads(ready_path.read_text())
    del ready["decision_protocol"]
    ready_path.write_text(json.dumps(ready))
    with pytest.raises(Refused, match="compact-decision prewarming"):
        await CodexReasonerClient(service, service.call("owner", prepared["call_id"])).prepare()


@pytest.mark.parametrize("operation", ["prepare_call", "reason"])
async def test_old_worker_cannot_ignore_per_call_source_permissions(
    service, brief, tmp_path, operation
):
    prepared = prepare(service, brief, tmp_path)
    ready_path = tmp_path / "reasoner-ready.json"
    ready = json.loads(ready_path.read_text())
    del ready["source_selection_protocol"]
    ready_path.write_text(json.dumps(ready))
    client = CodexReasonerClient(service, service.call("owner", prepared["call_id"]))
    with pytest.raises(Refused, match="source-selection"):
        await client.request(operation, model="gpt-6-sol", instructions="Test", input="{}")
    assert not list(tmp_path.glob("*-reason.json"))


async def test_restarted_worker_cannot_skip_the_prepared_call_scope(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    call["usage"]["reasoner_preparation"] = {"worker_instance_id": "b" * 32}
    client = CodexReasonerClient(service, call)
    with pytest.raises(Refused, match="changed after preparation"):
        await client.request("reason", model="gpt-6-sol", instructions="Test", input="{}")
    assert not list(tmp_path.glob("*-reason.json"))


@pytest.mark.parametrize("invalid", ["missing", "broader", "none", "connectors", "duplicates"])
async def test_prewarm_rejects_manifest_outside_selected_source_tools(
    service, brief, tmp_path, invalid
):
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    call = service.call("owner", prepared["call_id"])
    # Set a new scoped fixture through normal preparation, including its document.
    call["brief"]["source_tools"] = ["notes.read"]
    scoped = service.prepare("owner", Brief.model_validate(call["brief"]), client_id="client")
    client = CodexReasonerClient(service, service.call("owner", scoped["call_id"]))
    manifest = {
        "enabled": True,
        "selection": "explicit",
        "tool_names": ["notes.read"],
        "connector_ids": ["notes"],
    }
    if invalid == "broader":
        manifest["tool_names"].append("mail.read")
    elif invalid == "none":
        manifest["selection"] = "none"
    elif invalid == "connectors":
        manifest["connector_ids"] = []
    elif invalid == "duplicates":
        manifest["tool_names"] *= 2
    client.request = AsyncMock(
        return_value={
            "prepared": True,
            **({"source_capabilities": manifest} if invalid != "missing" else {}),
        }
    )
    with pytest.raises(Refused, match="capabilities"):
        await client.prepare()


@pytest.mark.parametrize(
    "quote,accepted",
    [
        ("Arrive at 10 AM and meet in the west lobby.", True),
        ("Please confirm parking is free.", False),
    ],
)
async def test_canonical_preparation_answers_are_grounded_in_source_text_not_document_sections(
    service, brief, quote, accepted
):
    from tests.test_dot_preparation import request

    value = request(brief).model_copy(update={"unknowns": ["Please confirm parking is free."]})
    prepared = service.prepare("owner", value)
    call = service.call("owner", prepared["call_id"])
    context = reasoning_context(call, [], "Where should I meet?")
    assert context["prepared_sources"] == [] and quote in context["prep_document"]
    advice = ReasoningAdvice(
        status="ready",
        next_step="Answer.",
        answer="The west lobby.",
        supporting_quote=quote,
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
        )
    )
    reasoner = CallReasoner(service, client)
    if accepted:
        assert await reasoner.advise(call, [], "Where should I meet?") == advice
    else:
        with pytest.raises(Refused, match="supporting evidence"):
            await reasoner.advise(call, [], "Where should I meet?")


async def test_oversized_wire_request_is_refused_before_file_submission(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    client = CodexReasonerClient(service, service.call("owner", prepared["call_id"]))
    with pytest.raises(Refused, match="bounded transport"):
        await client.parse(
            model="gpt-6-sol",
            instructions="Test",
            input=json.dumps({"text": "é" * 12000}),
        )
    assert not list(tmp_path.glob("*-reason.json"))


def test_optional_confirmation_requires_host_capacity_before_dialing(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    client = CodexReasonerClient(service, service.call("owner", prepared["call_id"]))
    service.settings.post_call_audio_confirmation_model_directory = tmp_path / "medium.en"
    with pytest.raises(Refused, match="restart the Codex worker"):
        client.require_ready()
    ready_path = tmp_path / "reasoner-ready.json"
    ready = json.loads(ready_path.read_text())
    ready["closed_review_request_limit_bytes"] = 256000
    ready_path.write_text(json.dumps(ready))
    assert client.require_ready()["closed_review_request_limit_bytes"] == 256000


@pytest.mark.parametrize("characters,accepted", [(12000, True), (50000, False)])
async def test_closed_audio_review_has_its_own_bounded_transport(
    service, brief, tmp_path, monkeypatch, characters, accepted
):
    prepared = prepare(service, brief, tmp_path)
    client = CodexReasonerClient(service, service.call("owner", prepared["call_id"]))
    submitted = []

    def capture(path, request):
        submitted.append(request)
        raise RuntimeError("captured before private submission")

    monkeypatch.setattr("dots_phone.local_gmail_worker.private_json", capture)
    with pytest.raises(
        RuntimeError if accepted else Refused,
        match="captured before" if accepted else "bounded transport",
    ):
        await client.request(
            "review_closed_call",
            model="gpt-6-sol",
            instructions="Test",
            input=json.dumps({"text": "é" * characters}),
        )
    assert bool(submitted) == accepted
    assert not list(tmp_path.glob("*-reason.json"))


@pytest.mark.parametrize("source_available", [True, False])
async def test_compact_wire_answer_still_requires_verified_source_evidence(
    service, brief, tmp_path, source_available
):
    prepared = await active(service, brief, tmp_path)
    service.pending_decision(
        prepared["call_id"],
        "compact-source",
        json.dumps({"operation": "reason", "arguments": {"question": "What is the postal code?"}}),
    )
    call = service.call("owner", prepared["call_id"])
    client = CodexReasonerClient(service, call)
    client.request = AsyncMock(
        return_value={
            "advice": {
                "decision": {
                    "kind": "say",
                    "text": "02139",
                    "facts": [],
                    "quote": "City postal code: 02139",
                }
            },
            "tool_quote_verified": source_available,
            "tool_calls": 1,
        }
    )
    if source_available:
        advice = await CallReasoner(service, client).advise(call, [], "What is the postal code?")
        assert advice.answer == "02139"
        saved = service.call("owner", call["call_id"])
        assert saved["reasoner_advice"][-1]["supporting_quote"] == "City postal code: 02139"
    else:
        with pytest.raises(Refused, match="lacks supplied supporting evidence"):
            await CallReasoner(service, client).advise(call, [], "What is the postal code?")


async def test_voice_handoff_omits_reasoning_evidence_after_validation(service, brief, tmp_path):
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(
        advise=AsyncMock(
            return_value=ReasoningAdvice(
                status="ready",
                next_step="Say the verified city.",
                answer="Cambridge",
                supporting_quote="Private source evidence: city Cambridge",
                report_supported=False,
            )
        )
    )
    pending = service.pending_decision(
        cid,
        "reason-tool",
        json.dumps(
            {
                "operation": "reason",
                "arguments": {"question": "What is the verified city?"},
            }
        ),
    )
    await worker.answer(service.call("owner", cid), pending)
    output = json.loads(service.provider.updates[-1][1])
    assert output == {
        "untrusted_data": True,
        "status": "ready",
        "next_step": "Say the verified city.",
        "answer": "Cambridge",
    }


async def test_cancellation_discards_even_a_correctly_bound_inflight_reasoner_result(
    service, brief, tmp_path, monkeypatch
):
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    client = CodexReasonerClient(service, service.call("owner", cid))
    original_sleep = asyncio.sleep

    async def cancel(_):
        await service.end("owner", cid)
        await original_sleep(0)

    monkeypatch.setattr("dots_phone.call_reasoner.asyncio.sleep", cancel)
    with pytest.raises(Refused, match="became stale"):
        await client.parse(
            model="gpt-6-sol",
            instructions="Test",
            input=json.dumps(reasoning_context(client.call, [], "What next?")),
        )
    assert not list(tmp_path.glob("*-*-reason*.json"))


async def active(service, brief, tmp_path, *, source_verification=False, keypad=False):
    prepared = prepare(
        service, brief, tmp_path, source_verification=source_verification, keypad=keypad
    )
    service.settings.dry_run = False
    service.start(
        "owner",
        Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
        client_id="client",
    )
    await service.tick()
    return prepared


async def test_local_runtime_starts_without_dots_tasks_or_subscriptions(service, brief, tmp_path):
    prepared = await active(service, brief, tmp_path)
    assert prepared["calling_worker_mode"] == "autonomous_local_reasoner"
    assert prepared["information_capabilities"]["can_request_host_information"] is False
    assert prepared["host_lifecycle"]["required"] is False
    assert "three_active_call_event_subscriptions" not in prepared["approval_requirements"]
    assert service.provider.dials == [prepared["call_id"]]
    assert service.store.all("subscription") == []
    await service.end("owner", prepared["call_id"])
    closed = json.loads((tmp_path / (prepared["call_id"] + "-reason-closed.json")).read_text())
    assert closed == {"call_id": prepared["call_id"], "brief_hash": prepared["brief_hash"]}


def test_missing_or_stale_codex_worker_blocks_before_dialing(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path)
    (tmp_path / "reasoner-ready.json").unlink()
    service.settings.dry_run = False
    with pytest.raises(Refused, match="before dialing"):
        service.start(
            "owner",
            Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
            client_id="client",
        )
    assert not service.provider.dials


def test_source_enabled_call_blocks_before_start_when_worker_disables_tools(
    service, brief, tmp_path
):
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    ready_path = tmp_path / "reasoner-ready.json"
    ready = json.loads(ready_path.read_text())
    ready["connected_read_tools"] = 0
    ready_path.write_text(json.dumps(ready))
    service.settings.dry_run = False
    with pytest.raises(Refused, match="no connected read tools"):
        service.start(
            "owner",
            Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
            client_id="client",
        )
    assert not service.provider.dials
    assert not list(tmp_path.glob("*-reason.json"))
    call = service.call("owner", prepared["call_id"])
    call["brief"]["source_tools"] = []
    assert CodexReasonerClient(service, call).require_ready()["connected_read_tools"] == 0


async def test_empty_inherited_source_manifest_cannot_pass_prewarm(service, brief, tmp_path):
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    client = CodexReasonerClient(service, service.call("owner", prepared["call_id"]))
    client.request = AsyncMock(
        return_value={
            "prepared": True,
            "source_capabilities": {
                "enabled": False,
                "selection": "inherited",
                "tool_names": [],
                "connector_ids": [],
            },
        }
    )
    with pytest.raises(Refused, match="capabilities"):
        await client.prepare()


async def test_reasoner_failure_answers_locally_without_dots_or_invented_data(
    service, brief, tmp_path
):
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(advise=AsyncMock(side_effect=TimeoutError))
    pending = service.pending_decision(
        cid,
        "reason-tool",
        json.dumps(
            {
                "operation": "reason",
                "arguments": {"question": "Which verification alternative works?"},
            }
        ),
    )
    await worker.answer(service.call("owner", cid), pending)
    state = service.call("owner", cid)
    assert state["state"] == "active" and state["pending"] is None
    answer = json.loads(service.provider.updates[-1][1])
    assert answer["status"] == "unknown" and answer["answer"] == ""
    assert "Do not guess or ask Dots" in answer["next_step"]
    assert "current verified context" in answer["next_step"]
    assert "Ask the recipient for an alternative" not in answer["next_step"]
    assert "finish unresolved" not in answer["next_step"]
    assert not service.store.all("delivery")


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
async def test_semantically_false_completion_is_rejected_despite_matching_quote(
    service, brief, tmp_path, policy
):
    service.settings.local_reasoner_response_policy = policy
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    quote = "Your lifetime points are 50,000. I cannot provide available points."
    service.observe_transcript(cid, [{"role": "user", "text": quote}])
    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(
        advise=AsyncMock(
            return_value=ReasoningAdvice(
                status="blocked",
                next_step="Lifetime points do not establish available points.",
                answer="",
                supporting_quote="",
                report_supported=False,
            )
        )
    )
    pending = service.pending_decision(
        cid,
        "finish",
        json.dumps(
            {
                "operation": "complete",
                "arguments": {
                    "report": {
                        "outcome": "completed",
                        "evidence": [
                            {
                                "criterion_id": "balance",
                                "text": "Available points are 50,000.",
                                "quote": quote,
                            }
                        ],
                        "unresolved_questions": [],
                    }
                },
            }
        ),
    )
    await worker.answer(service.call("owner", cid), pending)
    assert not service.call("owner", cid).get("conclusion")
    assert json.loads(service.provider.updates[-1][1])["result_recorded"] is False
    worker.reasoner.advise.assert_awaited_once()


async def test_api_comparison_backend_retains_request_audit_and_cannot_invent_evidence(
    service, brief, tmp_path
):
    prepared = await active(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    advice = ReasoningAdvice(
        status="ready",
        next_step="Answer the question.",
        answer="Cambridge",
        supporting_quote="The city is Cambridge",
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(
                    output_parsed=advice,
                    usage=None,
                )
            )
        )
    )
    reasoner = CallReasoner(service, client)
    with pytest.raises(Refused, match="supporting evidence"):
        await reasoner.advise(call, [], "What city?")
    assert client.responses.parse.await_args.kwargs["store"] is False
    fresh = service.call("owner", call["call_id"])
    assert fresh["usage"]["call_reasoner"]["requests"] == 1
    fresh["usage"]["call_reasoner"]["requests"] = 20
    service.save(fresh)
    with pytest.raises(Refused, match="supporting evidence"):
        await reasoner.advise(fresh, [], "What city?")
    assert client.responses.parse.await_count == 2
    assert service.call("owner", call["call_id"])["usage"]["call_reasoner"]["requests"] == 21


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
async def test_productive_consultations_continue_past_eight_under_both_policies(
    service, brief, tmp_path, policy
):
    service.settings.local_reasoner_response_policy = policy
    prepared = await active(service, brief, tmp_path)
    cid = prepared["call_id"]
    fact = brief.facts[0]
    advice = ReasoningAdvice(
        status="ready",
        next_step="Answer from the approved briefing.",
        answer=fact.value,
        supporting_quote="",
        supporting_fact_names=[fact.name],
        report_supported=False,
    )
    response = SimpleNamespace(
        output_parsed=advice,
        usage=None,
        codex_transport=True,
        handoff_metrics={},
        tool_calls=0,
        tool_quote_verified=False,
    )
    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)))
    worker = service.local_context_worker
    worker.reasoner = CallReasoner(service, client)
    for index in range(12):
        pending = service.pending_decision(
            cid,
            f"consult-{index}",
            json.dumps(
                {"operation": "reason", "arguments": {"question": "Confirm the approved date."}}
            ),
        )
        await worker.answer(service.call("owner", cid), pending)
        output = json.loads(service.provider.updates[-1][1])
        assert output["status"] == "ready" and output["answer"] == fact.value
        assert service.call("owner", cid)["state"] == "active"
    assert client.responses.parse.await_count == 12
    assert service.call("owner", cid)["usage"]["call_reasoner"]["requests"] == 12


async def test_approved_purpose_can_support_a_routing_answer(service, brief):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    advice = ReasoningAdvice(
        status="ready",
        next_step="State the inquiry goal.",
        answer="I am calling about the booking.",
        supporting_quote=call["brief"]["purpose"],
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=advice, usage=None))
        )
    )
    assert await CallReasoner(service, client).advise(call, [], "What do I ask for?") == advice


def test_missing_or_duplicate_reasoner_tool_cannot_fall_back_to_free_speech():
    from dots_phone.call_reasoner import reasoning_turn_policy

    for policy in ("always_consult", "direct_with_codex"):
        for tools in ([], [{"name": "finish_call"}], [{"name": "consult_call_reasoner"}] * 2):
            with pytest.raises(Refused, match="must be available"):
                reasoning_turn_policy(tools, response_policy=policy)


@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
@pytest.mark.parametrize("effort", ["minimal", "medium"])
def test_voice_keeps_codex_on_demand_and_effort_independent_of_response_policy(
    service, brief, tmp_path, policy, effort
):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice

    service.settings.local_reasoner_response_policy = policy
    service.settings.local_reasoner_voice_effort = effort
    prepared = prepare(service, brief, tmp_path, source_verification=True)
    call = service.call("owner", prepared["call_id"])
    call["usage"]["reasoner_preparation"] = {
        "source_capabilities": {
            "enabled": True,
            "selection": "explicit",
            "connector_ids": ["approved-notes"],
            "tool_names": ["approved-notes.read"],
        }
    }
    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    assert llm._settings.session_properties.reasoning.effort == effort
    assert {tool.name for tool in context.tools.standard_tools} == {
        "consult_call_reasoner",
        "finish_call",
        "hang_up",
        "wait_for_recipient",
    }
    assert ("Each recipient turn starts with a required consult_call_reasoner" in prompt) == (
        policy == "always_consult"
    )
    assert '"connector_ids": ["approved-notes"]' in prompt
    assert '"tool_count": 1' in prompt
    assert "read relevant IDs with read_call_source" not in prompt
    instructions = " ".join(prompt.split())
    assert "A request to say, spell or confirm a value needs a spoken answer" in instructions
    assert "a numeric identifier alone does not imply keypad input" in instructions.lower()
    if policy == "direct_with_codex":
        assert "Answer ordinary questions immediately" in prompt
        assert "a live plugin lookup is" in prompt
        assert "Before saying a requested fact is unavailable" in prompt
        assert "Their belonging to the owner is not a reason to refuse" in prompt
        assert "You and Codex are reasoning partners" in instructions
        assert "There is no fixed number of attempts" in instructions
        assert "automated-only tests must honor their stop conditions" in instructions
        assert "Do not try a third topic rewrite" not in instructions


async def test_spoken_verification_reaches_reasoner_with_method_hint_and_returns_no_keypad_control(
    service, brief, tmp_path
):
    fact = brief.facts[0].model_copy(update={"name": "reference", "value": "0076102"})
    prepared = await active(
        service,
        brief.model_copy(update={"facts": [fact], "disclosures": ["reference"]}),
        tmp_path,
        source_verification=True,
    )
    cid = prepared["call_id"]
    recipient = "What is your reference? Please say it."
    service.observe_transcript(cid, [{"role": "user", "text": recipient}])
    advice = ReasoningAdvice(
        status="ready",
        next_step="Say the reference.",
        answer="Zero zero seven six one zero two.",
        supporting_quote="",
        supporting_fact_names=["reference"],
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(output_parsed=advice, usage=None),
            )
        )
    )
    service.local_context_worker.reasoner = CallReasoner(service, client)
    service.provider.enter_verification_fact = AsyncMock()
    service.provider.press_phone_key = AsyncMock()
    pending = service.pending_decision(
        cid,
        "spoken-reference",
        json.dumps(
            {
                "operation": "reason",
                "arguments": {"question": "How should I provide the reference?"},
            }
        ),
    )
    await service.local_context_worker.answer(service.call("owner", cid), pending)
    request = client.responses.parse.await_args.kwargs
    assert json.loads(request["input"])["recent_transcript"][-1]["text"] == recipient
    instructions = " ".join(request["instructions"].split())
    assert "put the spoken answer in answer and set action null" in instructions
    assert (
        "Only when the recipient explicitly requests keypad entry for a newly retrieved numeric value"
        in instructions
    )
    assert (
        "Use keypad actions only when the recipient explicitly requests keypad input or offers a keypad option"
        in instructions
    )
    output = json.loads(service.provider.updates[-1][1])
    assert output["answer"] == advice.answer and "control_result" not in output
    service.provider.enter_verification_fact.assert_not_awaited()
    service.provider.press_phone_key.assert_not_awaited()


async def test_optional_consult_dispatches_missing_fact_to_codex_without_host_lookup():
    pytest.importorskip("pipecat")
    from dots_phone.call_task import task_tools

    llm = SimpleNamespace(request_supervision=AsyncMock())
    consult = next(
        tool
        for tool in task_tools(llm, reasoner=True, source_tools=False)
        if getattr(tool, "__name__", "") == "consult_call_reasoner"
    )
    params = SimpleNamespace()
    await consult(params, "Find the current reference in the approved notes plugin.")
    arguments, keywords = llm.request_supervision.await_args
    assert arguments[0] is params
    assert json.loads(arguments[1]) == {
        "operation": "reason",
        "arguments": {"question": "Find the current reference in the approved notes plugin."},
    }
    assert keywords == {"defer_source_speech": True}


async def test_codex_result_requires_matching_nonce_revision_and_hash(
    service, brief, tmp_path, monkeypatch
):
    prepared = await active(service, brief, tmp_path)
    call = service.call("owner", prepared["call_id"])
    client = CodexReasonerClient(service, call)
    original_sleep = __import__("asyncio").sleep

    async def deliver(_):
        request_path = next(tmp_path.glob("*-reason.json"))
        request = json.loads(request_path.read_text())
        result = {
            **{
                k: request[k] for k in ("schema", "call_id", "request_id", "revision", "brief_hash")
            },
            "nonce": "wrong",
            "advice": {},
        }
        request_path.with_name(
            request_path.name.replace("-reason.json", "-reason-result.json")
        ).write_text(json.dumps(result))
        await original_sleep(0)

    monkeypatch.setattr("dots_phone.call_reasoner.asyncio.sleep", deliver)
    with pytest.raises(Refused, match="not bound"):
        await client.parse(
            model="gpt-6-sol",
            instructions="Fictional test",
            input=json.dumps(reasoning_context(call, [], "What should we do?")),
        )
    assert not list(tmp_path.glob("*-reason*.json"))


async def test_reasoner_selected_keypad_does_not_trigger_spoken_continuation():
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    llm = SupervisedRealtimeService.__new__(SupervisedRealtimeService)
    from dots_phone.lookup_wait import LookupWait

    llm.lookup_wait = LookupWait(llm)
    llm.check_decision = lambda *args: None
    llm.stage = lambda *args: None
    llm._completed_tool_calls = set()
    llm.source_continuations = {}
    llm.source_continuation_task = None
    llm.reasoner_tool_turns = {}
    llm._send_tool_result = AsyncMock()
    params = SimpleNamespace(result_callback=AsyncMock())
    future = asyncio.get_running_loop().create_future()
    await llm.deliver_source(
        {"runtime_supervision": "local_reasoner", "pending": {"request_id": "test"}},
        "tool",
        json.dumps({"control_result": {"carrier_control_accepted": True}}),
        params,
        future,
    )
    assert future.result() is True
    assert llm.source_continuations == {} and llm.source_continuation_task is None
    assert params.result_callback.await_args.kwargs["properties"].run_llm is False


async def test_connected_verification_data_is_bound_to_this_call_and_exact_source_digits(
    service, brief, tmp_path
):
    from dots_phone.account_task import verification_digits

    prepared = await active(service, brief, tmp_path, source_verification=True)
    cid = prepared["call_id"]
    pending = service.pending_decision(cid, "lookup", "A scoped reasoning request")
    call = service.call("owner", cid)
    advice = ReasoningAdvice(
        status="ready",
        next_step="Use the verified postal code.",
        answer="02139",
        supporting_quote="The current postal code is 02139.",
        report_supported=False,
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(
                    output_parsed=advice,
                    usage=None,
                    codex_transport=True,
                    tool_calls=1,
                    tool_quote_verified=True,
                )
            )
        )
    )
    await CallReasoner(service, client).advise(call, [], "What postal code is available?")
    fresh = service.call("owner", cid)
    source_id = "lookup:" + pending["request_id"]
    quote = "Enter your postal code followed by pound."
    assert (
        verification_digits(
            fresh, "", quote, quote, source_id=source_id, source_value="02139", terminator="#"
        )
        == "02139#"
    )
    with pytest.raises(Refused, match="source evidence"):
        verification_digits(fresh, "", quote, quote, source_id=source_id, source_value="99999")
