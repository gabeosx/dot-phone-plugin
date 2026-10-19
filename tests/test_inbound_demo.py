import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

pytest.importorskip("pipecat")


@pytest.fixture
def demo(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (root / "inbound_demo.py").exists():
        root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root))
    runtime = {
        "OPENAI_API_KEY": "offline-model-fixture",
        "TWILIO_ACCOUNT_SID": "AC" + "2" * 32,
        "TWILIO_AUTH_TOKEN": "fixture-token",
        "TWILIO_API_KEY": "SK" + "3" * 32,
        "TWILIO_API_SECRET": "fixture-secret",
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime))
    monkeypatch.setenv("PIPECAT_INBOUND_ORIGIN", "https://media.example")
    monkeypatch.setenv("PIPECAT_INBOUND_CALLER", "+12125550100")
    monkeypatch.setenv("PIPECAT_INBOUND_NUMBER", "+12125550101")
    monkeypatch.setenv("PIPECAT_INBOUND_STREAMING_ENABLED", "true")
    monkeypatch.setenv("PIPECAT_INBOUND_RUNTIME_FILE", str(path))
    monkeypatch.setenv("PIPECAT_INBOUND_STATUS_FILE", str(tmp_path / "status.json"))
    spec = importlib.util.spec_from_file_location("inbound_fixture", root / "inbound_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inbound_trial_requires_signature_owned_number_and_caller(demo):
    data = {
        "AccountSid": "AC" + "2" * 32,
        "From": "+12125550100",
        "To": "+12125550101",
        "CallSid": "CA" + "4" * 32,
    }
    validator = RequestValidator("fixture-token")
    with TestClient(demo.app) as client:
        assert client.post("/twilio/inbound", data=data).status_code == 403
        wrong = {**data, "From": "+12125550102"}
        signature = validator.compute_signature("https://media.example/twilio/inbound", wrong)
        assert (
            client.post(
                "/twilio/inbound", data=wrong, headers={"x-twilio-signature": signature}
            ).status_code
            == 403
        )
        signature = validator.compute_signature("https://media.example/twilio/inbound", data)
        response = client.post(
            "/twilio/inbound", data=data, headers={"x-twilio-signature": signature}
        )
        assert response.status_code == 200
        assert "wss://media.example/voice" in response.text
        assert '<Parameter name="permit"' in response.text
        assert "<Hangup" in response.text
        assert client.post("/mcp", json={}).status_code == 404
        assert client.get("/owner/campaign/audio").status_code == 404


def test_phone_evidence_never_retains_human_transcript(demo):
    evidence = demo.PrivateEvidence()
    evidence.mark("caption", role="user", text="do-not-retain")
    evidence.mark("closed", seconds=5)
    assert "do-not-retain" not in json.dumps(evidence.report())


@pytest.mark.asyncio
async def test_early_verified_result_rejects_outdated_and_unverified_facts(demo):
    sent = []

    async def send(event):
        sent.append(event.to_payload())

    llm = SimpleNamespace(evidence=demo.PrivateEvidence(), send_client_event=send)
    verified = {"place": "museum", "closes": "fixture time", "synthetic": True}
    assert (
        await demo.forward_verified_lookup(
            llm, verified, revision=1, is_current=lambda revision: revision == 2
        )
        is None
    )
    assert (
        await demo.forward_verified_lookup(
            llm, {**verified, "synthetic": False}, revision=2, is_current=lambda revision: True
        )
        is None
    )
    assert sent == []
    event_id = await demo.forward_verified_lookup(
        llm, verified, revision=2, is_current=lambda revision: revision == 2
    )
    assert sent[0]["event_id"] == event_id
    assert sent[0]["type"] == "session.commentary.append"
    assert sent[0]["delegation_id"] is None
    assert "fixture time" in sent[0]["content"]
    llm.evidence.close_sent = True
    assert (
        await demo.forward_verified_lookup(
            llm, verified, revision=2, is_current=lambda revision: True
        )
        is None
    )
    assert len(sent) == 1


def test_backend_priority_tier_uses_the_supported_delegation_field(demo):
    from pipecat.services.openai.live.llm import _responses_delegation_config

    delegation = demo.OpenAILiveLLMService.ResponsesDelegation(
        settings=demo.backend_settings, service_tier="priority"
    )
    config = _responses_delegation_config(delegation)
    assert config["service_tier"] == "priority"
    assert config["reasoning"]["effort"] == "none"


def test_synthetic_probe_requires_its_own_signed_route_and_keeps_human_slot(demo):
    data = {
        "AccountSid": "AC" + "2" * 32,
        "From": "+12125550100",
        "To": "+12125550101",
        "CallSid": "CA" + "5" * 32,
    }
    validator = RequestValidator("fixture-token")
    with TestClient(demo.app) as client:
        assert client.post("/twilio/probe", data=data).status_code == 403
        wrong = validator.compute_signature("https://media.example/twilio/inbound", data)
        assert (
            client.post(
                "/twilio/probe", data=data, headers={"x-twilio-signature": wrong}
            ).status_code
            == 403
        )
        correct = validator.compute_signature("https://media.example/twilio/probe", data)
        assert (
            client.post(
                "/twilio/probe", data=data, headers={"x-twilio-signature": correct}
            ).status_code
            == 200
        )
        assert demo.records[data["CallSid"]]["synthetic"] is True
        demo.records[data["CallSid"]]["state"] = "completed"
        data["CallSid"] = "CA" + "6" * 32
        correct = validator.compute_signature("https://media.example/twilio/inbound", data)
        assert (
            client.post(
                "/twilio/inbound", data=data, headers={"x-twilio-signature": correct}
            ).status_code
            == 200
        )
        assert demo.records[data["CallSid"]]["synthetic"] is False


def test_trial_phone_route_is_paused_without_spending_a_model_session(demo):
    demo.streaming_enabled = False
    data = {
        "AccountSid": "AC" + "2" * 32,
        "From": "+12125550100",
        "To": "+12125550101",
        "CallSid": "CA" + "7" * 32,
    }
    signature = RequestValidator("fixture-token").compute_signature(
        "https://media.example/twilio/inbound", data
    )
    with TestClient(demo.app) as client:
        response = client.post(
            "/twilio/inbound", data=data, headers={"x-twilio-signature": signature}
        )
        assert response.status_code == 200
        assert "paused" in response.text
        assert "Stream" not in response.text
        assert demo.records == {}
        assert client.get("/healthz").json()["phone_streaming_enabled"] is False
