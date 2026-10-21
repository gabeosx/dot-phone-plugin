"""Import the complete packaged skill over authenticated MCP, without dialing."""

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from dots_phone.server import create_app
from dots_phone.skills import EXTENSION, SKILL_URI, SkillCatalog
from tests.conftest import FakeProvider, FakeSender


def test_authenticated_import_reconciles_every_resource_digest(settings):
    provider = FakeProvider()
    app = create_app(settings, provider, FakeSender())
    token = app.state.oauth.issue("skill-importer").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    with TestClient(app) as client:

        def request(method, params=None, *, status=200):
            response = client.post(
                "/mcp",
                headers={
                    **headers,
                    "mcp-method": method,
                    **({"mcp-name": params["uri"]} if method == "resources/read" else {}),
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": {"_meta": meta, **(params or {})},
                },
            )
            assert response.status_code == status, response.text
            return response.json()

        discovery = request("server/discover")["result"]
        assert discovery["capabilities"]["extensions"][EXTENSION] == {}
        assert discovery["capabilities"]["events"] == {}
        catalog = request("skills/list")["result"]
        assert len(catalog["skills"]) == 1 and "nextCursor" not in catalog
        skill = catalog["skills"][0]
        assert skill["frontmatter"]["metadata"]["version"] == "0.3.2"
        assert request("skills/get", {"uri": skill["uri"]})["result"]["skill"] == skill
        assert {r["uri"] for r in skill["resources"]} == {
            SKILL_URI,
            "skill://dots-phone/dots-phone/references/calling.md",
            "skill://dots-phone/dots-phone/references/conversation.md",
            "skill://dots-phone/dots-phone/references/speech-library.json",
        }
        for resource in skill["resources"]:
            contents = request("resources/read", {"uri": resource["uri"]})["result"]["contents"]
            assert len(contents) == 1 and contents[0]["uri"] == resource["uri"]
            body = (
                contents[0]["text"].encode()
                if "text" in contents[0]
                else base64.b64decode(contents[0]["blob"])
            )
            assert resource["digest"] == "sha256:" + hashlib.sha256(body).hexdigest()
        assert len(request("tools/list")["result"]["tools"]) == 10
        for method, params in [
            ("skills/get", {"uri": "file:///run/secrets/runtime"}),
            (
                "resources/read",
                {"uri": "skill://dots-phone/dots-phone/../../.secrets/runtime.json"},
            ),
            ("skills/list", {"cursor": "invented"}),
        ]:
            assert request(method, params, status=400)["error"]["code"] == -32602
        for method in ["skills/list", "skills/get", "resources/read"]:
            assert (
                client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method}).status_code
                == 401
            )
    assert not provider.dials


def test_skill_snapshot_does_not_follow_symlinks_or_read_outside_package(tmp_path):
    directory = tmp_path / "dots-phone"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: dots-phone\ndescription: Test\n---\nInstructions.\n"
    )
    private = tmp_path / "runtime.json"
    private.write_text(json.dumps({"private": "not for import"}))
    (directory / "secret.md").symlink_to(private)
    with pytest.raises(ValueError, match="symlinks"):
        SkillCatalog(directory)


def test_legacy_initialization_and_skill_import(settings):
    app = create_app(settings, FakeProvider(), FakeSender())
    token = app.state.oauth.issue("legacy-importer").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    with TestClient(app) as client:
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "skill-import-fixture", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["capabilities"]["extensions"][EXTENSION] == {}
        for method, params in [
            ("skills/list", {}),
            ("skills/get", {"uri": SKILL_URI}),
            ("resources/read", {"uri": SKILL_URI}),
        ]:
            response = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
            )
            assert response.status_code == 200 and "result" in response.json(), response.text


def test_skill_catalog_freezes_content_and_rejects_unknown_uris(tmp_path):
    directory = tmp_path / "dots-phone"
    directory.mkdir()
    main = directory / "SKILL.md"
    original = (
        "---\nname: dots-phone\ndescription: Test\nmetadata:\n  version: fixture\n---\nOriginal.\n"
    )
    main.write_text(original)
    catalog = SkillCatalog(directory)
    main.write_text(original.replace("Original", "Changed"))
    assert catalog.read(SKILL_URI)["contents"][0]["text"] == original
    returned = catalog.list()
    returned["skills"][0]["frontmatter"]["name"] = "changed"
    assert catalog.get(SKILL_URI)["skill"]["frontmatter"]["name"] == "dots-phone"
    with pytest.raises(ValueError, match="unknown"):
        catalog.read("file:///run/secrets/runtime")


def test_packaged_workflow_requires_preparation_and_explains_on_demand_codex():
    workflow = SkillCatalog().workflow()
    text = json.dumps(workflow)
    assert "Create the prep document before dialing" in text
    assert "prep_document.ready" in text
    assert "Known facts do not require" in text
    assert "source_tools" in text
    assert "Dots and Codex have separate connected-tool" in text


def test_workflow_fallback_is_authenticated_read_only_and_never_creates_calls(settings):
    provider = FakeProvider()
    app = create_app(settings, provider, FakeSender())
    token = app.state.oauth.issue("workflow-reader").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "mcp-method": "tools/call",
        "mcp-name": "get_phone_workflow",
    }
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    with TestClient(app) as client:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"_meta": meta, "name": "get_phone_workflow", "arguments": {}},
        }
        assert client.post("/mcp", json=body).status_code == 401
        response = client.post("/mcp", headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError")
        workflow = result["structuredContent"]
        assert workflow == SkillCatalog().workflow()
        assert workflow["version"] == "0.3.2"
        assert workflow["origin"] == "static_packaged_phone_skill"
        for supplied in [{"call_id": "a" * 32}, {"path": "/run/secrets/runtime"}]:
            body["params"]["arguments"] = supplied
            denied = client.post("/mcp", headers=headers, json=body)
            assert denied.status_code == 200
            assert denied.json()["result"]["isError"] is True
        assert not app.state.service.store.all("call")
        assert not app.state.service.store.all("approval")
    assert not provider.dials
