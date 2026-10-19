import re

from fastapi.testclient import TestClient
from pydantic import SecretStr

from dots_phone.oauth import HASHER
from dots_phone.server import create_app
from tests.conftest import FakeProvider, FakeSender


def test_readable_approval_preserves_exact_scope_csrf_and_password(settings, brief):
    password = "Synthetic-owner-password-123456"
    settings.owner_secret_hash = SecretStr(HASHER.hash(password))
    app = create_app(settings, FakeProvider(), FakeSender())
    with TestClient(app) as client:
        prepared = app.state.service.prepare("owner", brief)
        cid = prepared["call_id"]
        response = client.get("/approve/" + cid)
        assert response.status_code == 200
        assert "Spending limit" in response.text and "$2.00 maximum" in response.text
        assert "Recording</dt><dd>On" in response.text
        assert "Transcript</dt><dd>On" in response.text
        assert "<details><summary>Review facts, permissions and full scope" in response.text
        assert 'name="viewport"' in response.text
        nonce = re.search(r'<style nonce="([^"]+)"', response.text)[1]
        assert f"style-src 'nonce-{nonce}'" in response.headers["content-security-policy"]
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
        assert not app.state.service.call("owner", cid).get("authorization_id")
        csrf = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
        approved = client.post(
            "/approve/" + cid,
            data={"csrf": csrf, "password": password},
            headers={"Origin": settings.public_base_url},
        )
        assert approved.status_code == 200
        assert app.state.service.call("owner", cid)["authorization_id"]
        replay = client.post(
            "/approve/" + cid,
            data={"csrf": csrf, "password": password},
            headers={"Origin": settings.public_base_url},
        )
        assert replay.status_code == 403
        assert not app.state.service.provider.dials


def test_approval_escapes_task_and_fact_content(settings, brief):
    app = create_app(settings, FakeProvider(), FakeSender())
    brief = brief.model_copy(update={"purpose": '<script>alert("unsafe")</script>'})
    with TestClient(app) as client:
        cid = app.state.service.prepare("owner", brief)["call_id"]
        response = client.get("/approve/" + cid)
        assert "<script>" not in response.text
        assert "&lt;script&gt;" in response.text
        assert not app.state.service.call("owner", cid).get("authorization_id")
