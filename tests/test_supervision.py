import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dots_phone.account_task import AccountTaskWorker
from dots_phone.models import CallCleanup, CallHandlers, CompletionCriterion, Start
from dots_phone.server import create_app
from dots_phone.service import Refused
from dots_phone.supervision import previous_terminal_calls, register, report_cleanup
from tests.conftest import FakeProvider, FakeSender, subscribed


def prepare(service, brief):
    service.settings.account_tasks = True
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    service.settings.call_authorization_mode = "connected_owner"
    service.local_context_worker = AccountTaskWorker(service)
    return service.prepare(
        "owner",
        brief.model_copy(
            update={
                "actions": ["read_account_information", "record_call_result", "read_call_sources"],
                "constraints": ["Read-only account information inquiry"],
                "completion_criteria": [
                    CompletionCriterion(
                        id="balance", description="Hear the current available balance."
                    )
                ],
            }
        ),
        client_id="client",
    )


def handlers(prepared):
    return CallHandlers(
        call_id=prepared["call_id"], decision_task_id="a" * 32, terminal_task_id="b" * 32
    )


def cleanup(refs):
    return CallCleanup(
        **refs.model_dump(), decision_task_enabled=False, terminal_task_enabled=False
    )


def test_retry_cleanup_candidates_are_owner_and_destination_scoped_and_preserve_history(
    service, brief
):
    old = prepare(service, brief)
    call = service.call("owner", old["call_id"])
    call["state"] = "cancelled"
    call["result"] = {"outcome": "unresolved"}
    service.save(call)
    current = prepare(service, brief)
    candidates = current["previous_terminal_calls"]
    assert candidates[0]["call_id"] == old["call_id"]
    assert "brief" not in candidates[0]
    assert "automatically_pause" in candidates[0]["host_lifecycle"]["cleanup_policy"]
    assert (
        previous_terminal_calls(service, "other-owner", service.call("owner", current["call_id"]))
        == []
    )
    changed = service.call("owner", current["call_id"])
    changed["brief"]["destination"] = "+12025550199"
    assert previous_terminal_calls(service, "owner", changed) == []
    refs = handlers(old)
    register(service, "owner", "client", refs)
    report_cleanup(service, "owner", "client", cleanup(refs))
    assert (
        previous_terminal_calls(service, "owner", service.call("owner", current["call_id"])) == []
    )
    assert service.call("owner", old["call_id"])["result"] == {"outcome": "unresolved"}


async def test_connected_account_start_requires_durable_actual_handler_references(service, brief):
    prepared = prepare(service, brief)
    assert prepared["host_lifecycle"]["required"]
    await subscribed(service, prepared["call_id"])
    service.settings.dry_run = False
    start = Start(**{k: prepared[k] for k in ("call_id", "revision", "authorization_id")})
    with pytest.raises(Refused, match="register_call_handlers"):
        service.start("owner", start, client_id="client")
    assert not service.store.all("connected_call_reservation")
    refs = handlers(prepared)
    assert register(service, "owner", "client", refs)["state"] == "registered"
    assert register(service, "owner", "client", refs)["decision_task_id"] == refs.decision_task_id
    with pytest.raises(Refused, match="cannot be replaced"):
        register(service, "owner", "other-client", refs)
    with pytest.raises(Refused, match="cannot be replaced"):
        register(service, "owner", "client", refs.model_copy(update={"terminal_task_id": "c" * 32}))
    service.start("owner", start, client_id="client")
    await service.tick()
    assert service.provider.dials == [prepared["call_id"]]
    with pytest.raises(Refused, match="terminal call"):
        report_cleanup(service, "owner", "client", cleanup(refs))


async def test_cleanup_attestation_needs_closed_voice_and_matching_disabled_native_tasks(
    service, brief
):
    prepared = prepare(service, brief)
    refs = handlers(prepared)
    register(service, "owner", "client", refs)
    await subscribed(service, prepared["call_id"])
    service.settings.dry_run = False
    service.start(
        "owner",
        Start(**{k: prepared[k] for k in ("call_id", "revision", "authorization_id")}),
        client_id="client",
    )
    await service.tick()
    await service.end("owner", prepared["call_id"])
    assert (
        service.result("owner", prepared["call_id"])["host_lifecycle"]["state"]
        == "awaiting_call_closure"
    )
    with pytest.raises(Refused, match="confirmed voice closure"):
        report_cleanup(service, "owner", "client", cleanup(refs))
    call = service.call("owner", prepared["call_id"])
    original_result = call["result"].copy()
    call["usage"]["voice_closed_confirmed"] = True
    service.save(call)
    assert (
        service.result("owner", prepared["call_id"])["host_lifecycle"]["state"]
        == "cleanup_required"
    )
    with pytest.raises(Refused, match="registered call handlers"):
        report_cleanup(service, "owner", "other-client", cleanup(refs))
    with pytest.raises(Refused, match="registered call handlers"):
        report_cleanup(
            service,
            "owner",
            "client",
            cleanup(refs).model_copy(update={"terminal_task_id": "c" * 32}),
        )
    result = report_cleanup(service, "owner", "client", cleanup(refs))
    assert result["state"] == "reported_closed"
    assert result["cleanup_basis"] == "host_reported_native_task_settings"
    assert report_cleanup(service, "owner", "client", cleanup(refs)) == result
    assert service.call("owner", prepared["call_id"])["result"] == original_result
    assert service.result("owner", prepared["call_id"])["host_lifecycle"] == result


async def test_unstarted_cancellation_and_legacy_recovery_preserve_history(service, brief):
    prepared = prepare(service, brief)
    refs = handlers(prepared)
    await service.end("owner", prepared["call_id"])
    register(service, "owner", "client", refs)
    assert report_cleanup(service, "owner", "client", cleanup(refs))["state"] == "reported_closed"
    assert service.result("owner", prepared["call_id"])["outcome"] == "unresolved"
    assert not service.provider.dials


@pytest.mark.parametrize("field", ["decision_task_enabled", "terminal_task_enabled"])
def test_enabled_native_task_cannot_be_reported_as_cleaned_up(field):
    data = {
        "call_id": "call",
        "decision_task_id": "a" * 32,
        "terminal_task_id": "b" * 32,
        "decision_task_enabled": False,
        "terminal_task_enabled": False,
        field: True,
    }
    with pytest.raises(ValidationError):
        CallCleanup(**data)


def test_one_native_task_cannot_fill_both_lifecycle_roles():
    with pytest.raises(ValidationError):
        CallHandlers(call_id="call", decision_task_id="a" * 32, terminal_task_id="a" * 32)


def test_native_handler_cannot_be_reused_for_another_call(service, brief):
    first = prepare(service, brief)
    register(service, "owner", "client", handlers(first))
    second = prepare(service, brief)
    with pytest.raises(Refused, match="another call"):
        register(service, "owner", "client", handlers(second))
    assert service.get("owner", second["call_id"])["host_lifecycle"]["state"] == "not_registered"


def test_authenticated_mcp_lifecycle_keeps_business_history_and_labels_host_attestation(
    settings, brief
):
    app = create_app(settings, FakeProvider(), FakeSender())
    token = app.state.oauth.issue("client").access_token
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "mcp-method": "tools/call",
    }
    with TestClient(app) as client:
        prepared = app.state.service.prepare("owner", brief)
        refs = handlers(prepared)

        def call(name, arguments, authenticated=True):
            return client.post(
                "/mcp",
                headers={**headers, "mcp-name": name} if authenticated else {},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                        "name": name,
                        "arguments": arguments,
                    },
                },
            )

        assert call("register_call_handlers", refs.model_dump(), False).status_code == 401
        registered = call("register_call_handlers", refs.model_dump()).json()["result"]
        assert registered["structuredContent"]["state"] == "registered"
        early = call("report_call_cleanup", cleanup(refs).model_dump()).json()["result"]
        assert early["isError"]
        call("end_call", {"call_id": prepared["call_id"]})
        before = call("get_call_result", {"call_id": prepared["call_id"]}).json()["result"][
            "structuredContent"
        ]
        assert before["host_lifecycle"]["state"] == "cleanup_required"
        reported = call("report_call_cleanup", cleanup(refs).model_dump()).json()["result"]
        assert (
            reported["structuredContent"]["cleanup_basis"] == "host_reported_native_task_settings"
        )
        after = call("get_call_result", {"call_id": prepared["call_id"]}).json()["result"][
            "structuredContent"
        ]
        assert after["host_lifecycle"]["state"] == "reported_closed"
        assert {k: v for k, v in before.items() if k != "host_lifecycle"} == {
            k: v for k, v in after.items() if k != "host_lifecycle"
        }
    assert not app.state.service.provider.dials
