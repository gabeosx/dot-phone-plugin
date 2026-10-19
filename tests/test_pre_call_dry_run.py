"""No-network rehearsal of the prepared IVR-only call contract and teardown."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.call_reasoner import (
    AutonomousAccountWorker,
    CodexReasonerClient,
    reasoning_context,
)
from dots_phone.ivr_test_mode import tools
from dots_phone.models import Brief, Start
from dots_phone.task_voice import create_task_voice
from tests.conftest import FakeProvider


@pytest.mark.parametrize("stop_kind", ["human", "response_cap"])
async def test_prepared_mail_scoped_ivr_call_rehearsal(
    service, brief, tmp_path, monkeypatch, stop_kind
):
    # The injected provider is FakeProvider; no live provider or model is created.
    assert isinstance(service.provider, FakeProvider)
    service.settings.dry_run = False
    service.settings.account_tasks = True
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    service.settings.local_reasoner_enabled = True
    service.settings.local_reasoner_directory = tmp_path
    service.settings.call_authorization_mode = "connected_owner"
    service.local_context_worker = AutonomousAccountWorker(service)
    (tmp_path / "reasoner-ready.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "serving": True,
                "heartbeat_ms": time.time() * 1000,
                "connected_read_tools": 2,
                "instance_id": "a" * 32,
            }
        )
    )
    selected = ["gmail.search_emails", "yahoo_dot.yahoo_read_message"]
    data = brief.model_dump(mode="json")
    data.update(
        purpose="Fixture only: ask an automated system for the fictional account's tier.",
        facts=[
            {
                **data["facts"][0],
                "name": "account_number",
                "value": "0012345",
                "provenance": "Synthetic rehearsal fixture; never disclose to a real recipient",
            }
        ],
        disclosures=["account_number"],
        actions=["read_account_information", "record_call_result", "read_call_sources"],
        source_tools=selected,
        stop_on_human=True,
        test_response_limit=2,
        completion_criteria=[{"id": "tier", "description": "Hear the fictional current tier"}],
        synthetic=True,
    )
    prepared = service.prepare("owner", Brief.model_validate(data), client_id="fixture-client")
    call_id = prepared["call_id"]
    manifest = {
        "enabled": True,
        "selection": "explicit",
        "tool_names": selected,
        "connector_ids": ["fixture-gmail", "fixture-yahoo"],
    }
    request = AsyncMock(
        return_value={
            "prepared": True,
            "worker_instance_id": "a" * 32,
            "source_capabilities": manifest,
        }
    )
    monkeypatch.setattr(CodexReasonerClient, "request", request)
    service.start(
        "owner",
        Start(**{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}),
        client_id="fixture-client",
    )
    await service.tick()
    call = service.call("owner", call_id)
    assert call["state"] == "active"
    assert len(service.provider.dials) == 1  # Simulated carrier only.
    assert call["usage"]["reasoner_preparation"]["source_capabilities"] == manifest
    document = call["prep_document"]["markdown"]
    context = reasoning_context(call, [], "Fixture verification question")
    assert context["prep_document"] == document
    assert context["brief"]["source_tools"] == selected
    llm, voice_context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    assert document in prompt and "0012345" in prompt
    names = {tool.name for tool in voice_context.tools.standard_tools}
    assert {"consult_call_reasoner", "end_test_on_human"} <= names
    assert 'independent_reasoner_sources_enabled": true' in prompt
    assert not llm._websocket
    llm.return_local_tool_result = AsyncMock()
    if stop_kind == "human":
        handler = tools(llm, call)[0].handler
        params = SimpleNamespace(
            arguments={"evidence": "I'm Alex, how can I help?", "reason": "Fixture live greeting"}
        )
        await handler(params)
        assert llm.return_local_tool_result.call_args.kwargs["run_llm"] is False
    else:
        for response_id in ["fixture-one", "fixture-two"]:
            await llm.observe({"type": "response.created", "response": {"id": response_id}})
    await llm.stop_task
    result = service.result("owner", call_id)
    assert result["outcome"] == "unresolved"
    assert result["termination_reason"] == (
        "test_human_detected" if stop_kind == "human" else "test_response_limit"
    )
    assert not result["confirmed_facts"]
    assert len(service.provider.stops) == 1
    assert not service.provider.updates
