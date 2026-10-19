from __future__ import annotations

from fastapi.testclient import TestClient

from dots_phone.server import create_app
from tests.conftest import FakeProvider, FakeSender


def test_http_mcp_discovery_tools_and_auth(settings, brief):
    app = create_app(settings, FakeProvider(), FakeSender())
    token = app.state.oauth.issue("client").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "MCP-Protocol-Version": "2026-07-28",
        "Accept": "application/json, text/event-stream",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
            ).status_code
            == 401
        )
        response = client.post(
            "/mcp",
            headers={**headers, "mcp-method": "server/discover"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": meta},
            },
        )
        assert response.status_code == 200, response.text
        discovery = response.json()
        assert discovery["result"]["capabilities"]["events"] == {}, discovery
        response = client.post(
            "/mcp",
            headers={**headers, "mcp-method": "tools/list"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["result"]["tools"]) == 10
        for name, params in (
            ("events/list", {}),
            ("tools/call", {"name": "prepare_call", "arguments": brief.model_dump(mode="json")}),
        ):
            response = client.post(
                "/mcp",
                headers={
                    **headers,
                    "mcp-method": name,
                    **({"mcp-name": params["name"]} if "name" in params else {}),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": name,
                    "params": {**params, "_meta": meta},
                },
            )
            assert response.status_code == 200, response.text
            assert "error" not in response.json(), response.text
        assert client.get("/healthz").json()["dry_run"] is True


def test_approval_rejects_model_flag_and_csrf(settings, brief):
    app = create_app(settings, FakeProvider(), FakeSender())
    call_id = app.state.service.prepare("owner", brief)["call_id"]
    with TestClient(app) as client:
        response = client.post("/approve/" + call_id, data={"approved": "true"})
        assert response.status_code == 403
        assert app.state.service.call("owner", call_id).get("authorization_id") is None
        assert (
            client.post(
                "/twilio/status/" + call_id, data={"CallSid": "CA1", "CallStatus": "completed"}
            ).status_code
            == 403
        )


async def test_token_revocation_resource_and_expiry(settings, tmp_path):
    from dots_phone.oauth import OwnerOAuth, token_id
    from dots_phone.store import Store

    store = Store(settings.database, settings.encryption_key.get_secret_value())
    oauth = OwnerOAuth(settings, store)
    token = oauth.issue("client")
    assert await oauth.verify_token(token.access_token)
    row = store.get("oauth_access", token_id(token.access_token))
    row["resource"] = "https://other.example/mcp"
    store.put("oauth_access", token_id(token.access_token), "owner", row)
    assert await oauth.verify_token(token.access_token) is None
    row["resource"] = oauth.resource
    store.put("oauth_access", token_id(token.access_token), "owner", row)
    await oauth.revoke_token(await oauth.verify_token(token.access_token))
    assert await oauth.verify_token(token.access_token) is None
    store.close()
