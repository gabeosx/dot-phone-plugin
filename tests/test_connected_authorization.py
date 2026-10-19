"""Authenticated task requests authorize scoped calls without a second owner page."""

import base64
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dots_phone.models import Start
from dots_phone.server import create_app
from dots_phone.service import Refused
from tests.conftest import FakeProvider, FakeSender, subscribed


def connected(service, brief):
    service.settings.call_authorization_mode = "connected_owner"
    return service.prepare("owner", brief, client_id="client")


def start_parameters(prepared):
    return Start(**{k: prepared[k] for k in ("call_id", "revision", "authorization_id")})


def test_authenticated_mcp_preparation_issues_grant_without_dialing(settings, brief):
    settings.call_authorization_mode = "connected_owner"
    provider = FakeProvider()
    app = create_app(settings, provider, FakeSender())
    token = app.state.oauth.issue("client").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "mcp-method": "tools/call",
        "mcp-name": "prepare_call",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            "name": "prepare_call",
            "arguments": brief.model_dump(mode="json"),
        },
    }
    with TestClient(app) as client:
        assert client.post("/mcp", json=body).status_code == 401
        assert app.state.service.store.all("call") == []
        forged = {
            **body,
            "params": {
                **body["params"],
                "arguments": {
                    **body["params"]["arguments"],
                    "client_id": "another-client",
                    "approved": True,
                },
            },
        }
        rejected = client.post("/mcp", headers=headers, json=forged).json()["result"]
        assert rejected["isError"] is True
        assert app.state.service.store.all("call") == []
        response = client.post("/mcp", headers=headers, json=body)
        assert response.status_code == 200, response.text
        prepared = response.json()["result"]["structuredContent"]
        assert prepared["authorization_id"]
        assert prepared["approval_url"] is None
        assert prepared["approval_requirements"][0] == "authenticated_connected_owner"
        grant = app.state.service.store.get("approval", prepared["authorization_id"], "owner")
        assert grant["authorization_source"] == "connected_owner"
        assert grant["authorizing_client_id"] == "client"
        assert grant["hash"] == prepared["brief_hash"]
        assert not grant["consumed"]
        assert not provider.dials
        settings.dry_run = False
        start_body = {
            **body,
            "params": {
                **body["params"],
                "name": "start_call",
                "arguments": start_parameters(prepared).model_dump(mode="json"),
            },
        }
        start_headers = {**headers, "mcp-name": "start_call"}
        assert client.post("/mcp", headers=start_headers, json=start_body).json()["result"][
            "isError"
        ]
        for event in ("call.decision_required", "call.completed", "call.failed"):
            subscription = {
                **body,
                "method": "events/subscribe",
                "params": {
                    "_meta": body["params"]["_meta"],
                    "name": event,
                    "arguments": {"call_id": prepared["call_id"]},
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://receiver.example/events",
                        "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                    },
                },
            }
            response = client.post(
                "/mcp", headers={**headers, "mcp-method": "events/subscribe"}, json=subscription
            )
            assert "error" not in response.json(), response.text
        other = app.state.oauth.issue("other-client").access_token
        rejected = client.post(
            "/mcp", headers={**start_headers, "Authorization": "Bearer " + other}, json=start_body
        )
        assert rejected.json()["result"]["isError"] is True
        started = client.post("/mcp", headers=start_headers, json=start_body)
        assert not started.json()["result"].get("isError"), started.text
        assert started.json()["result"]["structuredContent"]["state"] in {
            "queued",
            "dialing",
            "active",
        }
        assert app.state.service.store.get("approval", grant["id"], "owner")["consumed"]


def test_reading_an_old_draft_does_not_authorize_it(service, brief):
    service.settings.call_authorization_mode = "connected_owner"
    draft = service.prepare("owner", brief)
    assert service.get("owner", draft["call_id"])["authorization_id"] is None
    assert service.store.all("approval") == []


async def test_connected_start_requires_subscriptions_and_same_client_and_is_single_use(
    service, brief
):
    prepared = connected(service, brief)
    params = start_parameters(prepared)
    service.settings.dry_run = False
    with pytest.raises(Refused, match="another connected client"):
        service.start("owner", params, client_id="other-client")
    with pytest.raises(Refused, match="another connected client"):
        service.start("owner", params)
    with pytest.raises(Refused, match="subscribe"):
        service.start("owner", params, client_id="client")
    assert service.store.all("connected_call_reservation") == []
    await subscribed(service, prepared["call_id"])
    service.start("owner", params, client_id="client")
    service.start("owner", params, client_id="client")
    await service.tick()
    assert len(service.provider.dials) == 1
    assert len(service.store.all("connected_call_reservation")) == 1
    with pytest.raises(Refused, match="another connected client"):
        service.start("owner", params, client_id="other-client")


@pytest.mark.parametrize("change", ["mode", "budget", "duration", "destination", "expiry", "scope"])
async def test_connected_grant_rechecks_policy_and_frozen_scope_at_start(service, brief, change):
    prepared = connected(service, brief)
    await subscribed(service, prepared["call_id"])
    service.settings.dry_run = False
    if change == "mode":
        service.settings.call_authorization_mode = "owner_page"
    elif change == "budget":
        service.settings.connected_call_budget_usd = Decimal("1")
    elif change == "duration":
        service.settings.max_seconds = 1
    elif change == "destination":
        service.settings.allowed_numbers = []
    elif change == "expiry":
        grant = service.store.get("approval", prepared["authorization_id"], "owner")
        grant["expires"] = time.time() - 1
        service.store.put("approval", grant["id"], "owner", grant)
    else:
        call = service.call("owner", prepared["call_id"])
        call["brief_hash"] = "changed-scope"
        service.save(call)
    with pytest.raises(Refused):
        service.start("owner", start_parameters(prepared), client_id="client")
    assert not service.provider.dials
    assert service.store.all("connected_call_reservation") == []


def test_connected_call_budget_is_enforced_before_creating_draft(service, brief):
    service.settings.call_authorization_mode = "connected_owner"
    service.settings.connected_call_budget_usd = Decimal("1")
    with pytest.raises(Refused, match="call budget"):
        service.prepare("owner", brief, client_id="client")
    assert service.store.all("call") == []
    assert service.store.all("approval") == []


async def test_daily_reservations_survive_call_deletion_and_expire_after_24_hours(service, brief):
    first = connected(service, brief)
    service.settings.connected_daily_budget_usd = Decimal("3")
    await subscribed(service, first["call_id"])
    service.settings.dry_run = False
    service.start("owner", start_parameters(first), client_id="client")
    # Retention cannot reset the spending limit or trigger another start.
    service.store.delete("call", first["call_id"])
    second = connected(service, brief)
    await subscribed(service, second["call_id"])
    with pytest.raises(Refused, match="daily call budget"):
        service.start("owner", start_parameters(second), client_id="client")
    assert len(service.store.all("connected_call_reservation")) == 1
    reservation = service.store.get("connected_call_reservation", first["call_id"], "owner")
    reservation["expires"] = time.time() - 1
    service.store.put("connected_call_reservation", first["call_id"], "owner", reservation)
    service.start("owner", start_parameters(second), client_id="client")
    assert service.get("owner", second["call_id"])["state"] == "queued"


async def failed_prewarm(service, brief, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from dots_phone.call_reasoner import CodexReasonerClient
    from tests.test_general_call_reasoner import prepare_general, start

    service.settings.connected_daily_budget_usd = Decimal("3")
    first = prepare_general(service, brief, tmp_path)
    monkeypatch.setattr(CodexReasonerClient, "prepare", AsyncMock(side_effect=TimeoutError))
    start(service, first)
    assert service.connected_reserved_budget("owner") == brief.budget_usd
    await service.tick()
    assert not service.provider.dials
    assert service.call("owner", first["call_id"])["result"]["termination_reason"] == (
        "reasoner_preparation_failed"
    )
    return first


async def test_failed_prewarm_releases_only_effective_budget_and_preserves_audit(
    service, brief, tmp_path, monkeypatch
):
    from tests.test_general_call_reasoner import prepare_general, start

    first = await failed_prewarm(service, brief, tmp_path, monkeypatch)
    reservation = service.store.get("connected_call_reservation", first["call_id"], "owner")
    assert reservation["budget_usd"] == str(brief.budget_usd)
    assert service.connected_reserved_budget("owner") == 0
    second = prepare_general(service, brief, tmp_path)
    start(service, second)
    assert service.call("owner", second["call_id"])["state"] == "queued"
    assert service.connected_reserved_budget("owner") == brief.budget_usd
    assert service.store.get("connected_call_reservation", first["call_id"], "owner") == (
        reservation
    )
    assert len(service.store.all("connected_call_reservation", "owner")) == 2
    assert service.settings.connected_daily_budget_usd == Decimal("3")
    assert not service.provider.dials


@pytest.mark.parametrize(
    "uncertainty",
    [
        "provider_sid",
        "dial_intent",
        "provider_seconds",
        "voice_session",
        "response_usage",
        "different_failure",
        "nonterminal",
        "deleted_record",
    ],
)
async def test_ambiguous_or_used_reservations_are_never_released(
    service, brief, tmp_path, monkeypatch, uncertainty
):
    from tests.test_general_call_reasoner import prepare_general, start

    first = await failed_prewarm(service, brief, tmp_path, monkeypatch)
    call = service.call("owner", first["call_id"])
    if uncertainty == "provider_sid":
        call["provider_sid"] = "carrier-attempt"
    elif uncertainty == "dial_intent":
        call["dial_started"] = time.time()
    elif uncertainty == "provider_seconds":
        call["usage"]["provider_seconds"] = 1
    elif uncertainty == "voice_session":
        call["usage"]["realtime_session_count"] = 1
    elif uncertainty == "response_usage":
        call["usage"]["realtime_response_usage"] = [{"output_tokens": 1}]
    elif uncertainty == "different_failure":
        call["result"]["termination_reason"] = "voice_failed"
    elif uncertainty == "nonterminal":
        call["state"] = "reconcile_required"
    service.save(call)
    if uncertainty == "deleted_record":
        service.store.delete("call", first["call_id"])
    second = prepare_general(service, brief, tmp_path)
    with pytest.raises(Refused, match="daily call budget"):
        start(service, second)
    assert service.connected_reserved_budget("owner") == brief.budget_usd
    assert len(service.store.all("connected_call_reservation", "owner")) == 1


def test_legacy_owner_page_mode_still_requires_its_grant(service, brief):
    prepared = service.prepare("owner", brief, client_id="client")
    assert prepared["authorization_id"] is None
    assert prepared["approval_url"]


async def test_task_destination_needs_no_allowlist_and_is_bound_to_start(service, brief):
    service.settings.destination_policy = "task_scope"
    service.settings.allowed_numbers = []
    brief = brief.model_copy(update={"destination": "+12125550201", "synthetic": False})
    prepared = connected(service, brief)
    await subscribed(service, prepared["call_id"])
    service.settings.dry_run = False
    # An explicitly restricted policy can still revoke an outstanding grant.
    service.settings.destination_policy = "allowlist"
    with pytest.raises(Refused, match="destination"):
        service.start("owner", start_parameters(prepared), client_id="client")
    assert service.store.all("connected_call_reservation") == []
    service.settings.destination_policy = "task_scope"
    service.start("owner", start_parameters(prepared), client_id="client")
    await service.tick()
    call = service.call("owner", prepared["call_id"])
    assert call["brief"]["destination"] == "+12125550201"
    assert service.provider.dials == [prepared["call_id"]]
