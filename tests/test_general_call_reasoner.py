"""No-network checks for locally supervised general informational calls."""

import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_reasoner import AutonomousCallWorker, CodexReasonerClient, ReasoningAdvice
from dots_phone.models import Brief, Start, now
from dots_phone.service import Refused, digest


def prepare_general(service, brief, tmp_path, **overrides):
    service.settings.account_tasks = True
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    service.settings.local_reasoner_enabled = True
    service.settings.local_reasoner_directory = tmp_path
    service.settings.call_authorization_mode = "connected_owner"
    (tmp_path / "reasoner-ready.json").write_text(
        json.dumps({"schema": 1, "serving": True, "heartbeat_ms": time.time() * 1000})
    )
    service.local_context_worker = AutonomousCallWorker(service)
    data = brief.model_dump(mode="json")
    data.update(
        actions=["ask_question", "record_call_result"],
        source_tools=[],
        completion_criteria=[{"id": "options", "description": "Hear fictional flight options"}],
    )
    data.update(overrides)
    return service.prepare("owner", Brief.model_validate(data), client_id="client")


def start(service, prepared, *, client_id="client"):
    service.settings.dry_run = False
    return service.start(
        "owner",
        Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
        client_id=client_id,
    )


async def test_general_prewarm_precedes_dial_and_terminal_retires_local_agent(
    service, brief, tmp_path, monkeypatch
):
    prepared = prepare_general(service, brief, tmp_path)
    assert prepared["calling_worker_mode"] == "autonomous_local_reasoner"
    assert "three_active_call_event_subscriptions" not in prepared["approval_requirements"]
    assert not prepared["host_lifecycle"]["required"]
    call = service.call("owner", prepared["call_id"])
    scope = service.local_context_worker.scope(call)
    assert scope["read_only_general"] and "read_only_account" not in scope
    assert not scope["source_tools"] and not scope["host_sources"]

    async def warm(client):
        assert service.provider.dials == []
        assert client.call["state"] == "queued"
        return {"prepared": True, "worker_instance_id": "a" * 32}

    monkeypatch.setattr(CodexReasonerClient, "prepare", warm)
    start(service, prepared)
    start(service, prepared)  # Same authorized request is idempotent.
    await service.tick()
    assert service.provider.dials == [prepared["call_id"]]
    assert service.store.all("subscription") == []
    await service.end("owner", prepared["call_id"])
    closed = json.loads((tmp_path / (prepared["call_id"] + "-reason-closed.json")).read_text())
    assert closed == {"call_id": prepared["call_id"], "brief_hash": prepared["brief_hash"]}
    assert service.provider.stops


@pytest.mark.parametrize(
    "failure", ["missing_worker", "non_autonomous", "stale_heartbeat", "wrong_client"]
)
def test_general_start_requires_real_local_supervision_and_bound_client(
    service, brief, tmp_path, failure
):
    prepared = prepare_general(service, brief, tmp_path)
    if failure == "missing_worker":
        service.local_context_worker = None
    elif failure == "non_autonomous":
        service.local_context_worker.autonomous = False
    elif failure == "stale_heartbeat":
        (tmp_path / "reasoner-ready.json").write_text(
            json.dumps({"schema": 1, "serving": True, "heartbeat_ms": 0})
        )
    with pytest.raises(Refused):
        start(service, prepared, client_id="other" if failure == "wrong_client" else "client")
    assert service.provider.dials == []


async def test_general_failed_prewarm_never_dials(service, brief, tmp_path, monkeypatch):
    prepared = prepare_general(service, brief, tmp_path)
    monkeypatch.setattr(CodexReasonerClient, "prepare", AsyncMock(side_effect=TimeoutError))
    start(service, prepared)
    await service.tick()
    assert not service.provider.dials
    call = service.call("owner", prepared["call_id"])
    assert call["state"] == "failed"
    assert call["result"]["termination_reason"] == "reasoner_preparation_failed"


async def test_general_reasoning_uses_question_permission_without_account_action(
    service, brief, tmp_path
):
    prepared = prepare_general(service, brief, tmp_path)
    service.local_context_worker.before_dial = AsyncMock()
    start(service, prepared)
    await service.tick()
    worker = service.local_context_worker
    worker.reasoner = SimpleNamespace(
        advise=AsyncMock(
            return_value=ReasoningAdvice(
                status="ready",
                next_step="Ask which option is refundable.",
                answer="",
                supporting_quote="",
                report_supported=False,
            )
        )
    )
    pending = service.pending_decision(
        prepared["call_id"],
        "reason-tool",
        json.dumps(
            {"operation": "reason", "arguments": {"question": "What clarification is needed?"}}
        ),
    )
    await worker.answer(service.call("owner", prepared["call_id"]), pending)
    call = service.call("owner", prepared["call_id"])
    assert call["state"] == "active" and call["pending"] is None
    assert "read_account_information" not in call["brief"]["actions"]
    assert (
        json.loads(service.provider.updates[-1][1])["next_step"]
        == "Ask which option is refundable."
    )


@pytest.mark.parametrize("mutation", ["owner", "hash", "expiry", "destination", "grant", "budget"])
def test_general_scope_revalidates_authorized_boundaries(service, brief, tmp_path, mutation):
    prepared = prepare_general(service, brief, tmp_path)
    start(service, prepared)
    call = service.call("owner", prepared["call_id"])
    if mutation == "owner":
        call["owner"] = "other"
    elif mutation == "hash":
        call["brief_hash"] = "a" * 64
    elif mutation == "expiry":
        call["brief"]["deadline"] = (now() - timedelta(seconds=1)).isoformat()
        call["brief_hash"] = digest(call["brief"])
    elif mutation == "destination":
        call["brief"]["destination"] = "+12125550102"
        call["brief_hash"] = digest(call["brief"])
    elif mutation == "grant":
        call["started_authorization"] = "missing"
    elif mutation == "budget":
        call["brief"]["budget_usd"] = "3.01"
        call["brief_hash"] = digest(call["brief"])
    with pytest.raises(Refused):
        service.local_context_worker.scope(call)


def test_other_general_actions_keep_subscription_requirement(service, brief, tmp_path):
    prepared = prepare_general(
        service,
        brief,
        tmp_path,
        actions=["ask_question", "record_call_result", "purchase_ticket"],
    )
    assert prepared["calling_worker_mode"] == "manual"
    assert "three_active_call_event_subscriptions" in prepared["approval_requirements"]
    with pytest.raises(Refused, match="subscribe before dialing"):
        start(service, prepared)


@pytest.mark.parametrize("effort", ["minimal", "low", "medium"])
@pytest.mark.parametrize("policy", ["direct_with_codex", "always_consult"])
def test_general_voice_honors_effort_with_native_audio_and_local_tools(
    service, brief, tmp_path, effort, policy
):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice

    prepared = prepare_general(service, brief, tmp_path)
    service.settings.local_reasoner_voice_effort = effort
    call = service.call("owner", prepared["call_id"])
    call["reasoner_response_policy"] = policy
    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    config = llm._settings.session_properties
    assert config.max_output_tokens == 512
    assert config.reasoning.effort == effort
    assert config.audio.input.noise_reduction is None
    vad = config.audio.input.turn_detection
    assert vad.type == "server_vad" and vad.threshold == 0.5
    assert vad.silence_duration_ms == 300 and vad.idle_timeout_ms is None
    assert vad.create_response and vad.interrupt_response
    assert {tool.name for tool in context.tools.standard_tools} == {
        "consult_call_reasoner",
        "finish_call",
        "hang_up",
        "wait_for_recipient",
    }
    assert "Obtain account information only" not in prompt
    assert "When a person answers, make the approved request directly" in prompt
    assert "Do not add a routine unsolicited AI introduction" in prompt
    assert "If asked who you are or whether you are AI" in prompt
