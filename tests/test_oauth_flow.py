import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from dots_phone.oauth import HASHER
from dots_phone.server import create_app
from tests.conftest import FakeProvider, FakeSender


@pytest.mark.parametrize("private_tunnel", [False, True])
def test_full_pkce_consent_refresh_replay_and_revocation(settings, private_tunnel):
    password = "Synthetic-owner-password-123456"
    settings.owner_secret_hash = SecretStr(HASHER.hash(password))
    if private_tunnel:
        settings.mcp_resource_url = "https://api.openai.com/v1/tunnel/tunnel_fixture"
        settings.mcp_metadata_base_url = "http://phone-app:8000"
    app = create_app(settings, FakeProvider(), FakeSender())
    redirect = settings.redirect_uris[0]
    verifier = "x" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with TestClient(app, follow_redirects=False) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["issuer"] == settings.public_base_url
        assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
        assert metadata["authorization_response_iss_parameter_supported"] is True
        prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert prm["resource"] == app.state.oauth.resource
        assert prm["authorization_servers"] == [settings.public_base_url]
        challenge_response = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "server/discover"}
        )
        assert challenge_response.status_code == 401
        discovery_base = settings.mcp_metadata_base_url or settings.public_base_url
        assert (
            discovery_base + "/.well-known/oauth-protected-resource/mcp"
            in challenge_response.headers["www-authenticate"]
        )
        registered = client.post(
            "/register",
            json={
                "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "phone:control",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        blocked = client.post("/register", json={"redirect_uris": ["https://evil.example/cb"]})
        assert blocked.status_code >= 400
        authorize = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": app.state.oauth.resource,
                "state": "state-fixture",
                "scope": "phone:control",
            },
        )
        assert authorize.status_code == 302, authorize.text
        consent_url = authorize.headers["location"]
        page = client.get(consent_url)
        assert redirect in page.headers["content-security-policy"]
        assert "form-action 'self' " + redirect in page.headers["content-security-policy"]
        csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
        consent = client.post(
            consent_url,
            data={"csrf": csrf, "password": password},
            headers={"Origin": settings.public_base_url},
        )
        assert consent.status_code == 303, consent.text
        query = parse_qs(urlsplit(consent.headers["location"]).query)
        assert query["state"] == ["state-fixture"]
        assert query["iss"] == [settings.public_base_url]
        code = query["code"][0]
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "resource": app.state.oauth.resource,
        }
        bad = client.post("/token", data={**body, "code_verifier": "y" * 64})
        assert bad.status_code == 400
        tokens = client.post("/token", data=body)
        assert tokens.status_code == 200, tokens.text
        assert client.post("/token", data=body).status_code == 400
        previous = tokens.json()
        authenticated = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer " + previous["access_token"],
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "mcp-method": "tools/list",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert authenticated.status_code == 200, authenticated.text
        assert len(authenticated.json()["result"]["tools"]) == 10
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": previous["refresh_token"],
                "resource": app.state.oauth.resource,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        # Reuse of a rotated refresh token revokes the whole token family and subscriptions.
        replay = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": previous["refresh_token"],
                "resource": app.state.oauth.resource,
            },
        )
        assert replay.status_code == 400
        headers = {"Authorization": "Bearer " + refreshed.json()["access_token"]}
        assert (
            client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
            ).status_code
            == 401
        )
