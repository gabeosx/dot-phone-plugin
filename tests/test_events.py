from __future__ import annotations

import base64
import json
import time

import pytest
from standardwebhooks.webhooks import Webhook

from dots_phone.events import Events, SafeWebhook, signing_key
from dots_phone.service import Refused
from tests.conftest import FakeSender, started, subscribed


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example/x",
        "https://127.0.0.1/x",
        "https://receiver.example:8443/x",
        "https://u:p@receiver.example/x",
        "https://metadata.google.internal/x",
        "https://receiver.example/x#secret",
    ],
)
def test_ssrf_static_rejected(url):
    with pytest.raises(Refused):
        SafeWebhook(["receiver.example"]).validate_url(url)


@pytest.mark.parametrize(
    "secret", ["x", "whsec_notbase64!", "whsec_" + base64.b64encode(b"x").decode()]
)
def test_invalid_keys(secret):
    with pytest.raises(Refused):
        signing_key(secret)


async def test_verified_signed_retried_and_revoked(service, brief):
    sender = FakeSender()
    id, _ = await started(service, brief)
    events = await subscribed(service, id, sender)
    await service.end("owner", id)
    sender.status = 503
    await events.tick()
    last = sender.requests[-1]
    Webhook("whsec_" + base64.b64encode(b"s" * 32).decode()).verify(last[1], last[2])
    row = service.store.db.execute("SELECT * FROM deliveries").fetchone()
    assert row["status"] == "pending"
    event_id = json.loads(last[1])["eventId"]
    service.store.db.execute("UPDATE deliveries SET due=?", (time.time() - 1,))
    sender.status = 200
    await events.tick()
    assert json.loads(sender.requests[-1][1])["eventId"] == event_id
    assert service.store.db.execute("SELECT status FROM deliveries").fetchone()[0] == "delivered"
    events.revoke_client("client")
    assert not service.subscriptions_ready("owner", id)


async def test_failed_challenge_no_subscription(service, brief):
    sender = FakeSender()
    sender.echo = False
    events = Events(service, sender)
    call_id = service.prepare("owner", brief)["call_id"]
    with pytest.raises(Refused):
        await events.subscribe(
            "owner",
            "client",
            {
                "name": "call.completed",
                "arguments": {"call_id": call_id},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/events",
                    "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                },
            },
        )
    assert service.store.all("subscription") == []


async def test_completed_watchers_close_only_after_delivery_and_voice_closure(service, brief):
    sender = FakeSender()
    call_id, _ = await started(service, brief)
    events = await subscribed(service, call_id, sender)
    await service.end("owner", call_id)
    sender.status = 503
    await events.tick()
    assert service.subscriptions_ready("owner", call_id)
    sender.status = 200
    service.store.db.execute("UPDATE deliveries SET due=?", (time.time() - 1,))
    await events.tick()
    assert service.subscriptions_ready("owner", call_id)
    call = service.call("owner", call_id)
    call["usage"]["voice_closed_confirmed"] = True
    service.save(call)
    await events.tick()
    assert not any(s["active"] for s in service.store.all("subscription"))
    assert service.result("owner", call_id)["call_id"] == call_id
    assert service.result("owner", call_id)["usage"]["voice_closed_confirmed"]
    before = len(sender.requests)
    await events.tick()
    assert len(sender.requests) == before
    with pytest.raises(Refused, match="monitoring is finished"):
        await subscribed(service, call_id, sender)


async def test_failed_terminal_delivery_keeps_watchers_even_after_voice_closes(service, brief):
    sender = FakeSender()
    call_id, _ = await started(service, brief)
    events = await subscribed(service, call_id, sender)
    await service.end("owner", call_id)
    call = service.call("owner", call_id)
    call["usage"]["voice_closed_confirmed"] = True
    service.save(call)
    sender.status = 503
    await events.tick()
    assert service.subscriptions_ready("owner", call_id)
    sender.status = 200
    service.store.db.execute("UPDATE deliveries SET due=?", (time.time() - 1,))
    await events.tick()
    assert not service.subscriptions_ready("owner", call_id)


async def test_recovered_abnormal_closure_wakes_cleanup_and_waits_for_latest_receipt(
    service, brief
):
    sender = FakeSender()
    call_id, _ = await started(service, brief)
    events = await subscribed(service, call_id, sender)
    await service.end("owner", call_id, "voice_path_failed")
    await events.tick()
    assert service.subscriptions_ready("owner", call_id)
    call = service.call("owner", call_id)
    original_result = call["result"].copy()
    call["usage"].update(realtime_close_code=1011, realtime_usage_confirmed=False)
    service.save(call)
    await service.recover()
    call = service.call("owner", call_id)
    assert call["usage"]["voice_closed_confirmed"] is True
    assert call["usage"]["realtime_usage_confirmed"] is False
    assert call["result"] == original_result
    assert service.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2
    await service.recover()
    assert service.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2
    sender.status = 503
    await events.tick()
    assert service.subscriptions_ready("owner", call_id)
    sender.status = 200
    service.store.db.execute("UPDATE deliveries SET due=?", (time.time() - 1,))
    await events.tick()
    assert not service.subscriptions_ready("owner", call_id)


async def test_recovery_without_observed_socket_close_does_not_invent_closure(service, brief):
    call_id, _ = await started(service, brief)
    await service.end("owner", call_id, "voice_path_failed")
    await service.recover()
    assert not service.call("owner", call_id)["usage"].get("voice_closed_confirmed")


async def test_completed_cleanup_preserves_another_clients_undelivered_result(service, brief):
    call_id, _ = await started(service, brief)
    events = await subscribed(service, call_id, FakeSender())
    for name in ("call.completed", "call.failed", "call.decision_required"):
        await events.subscribe(
            "owner",
            "second-client",
            {
                "name": name,
                "arguments": {"call_id": call_id},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/second",
                    "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                },
            },
        )
    await service.end("owner", call_id)
    call = service.call("owner", call_id)
    call["usage"]["voice_closed_confirmed"] = True
    service.save(call)
    for client in ("client", "second-client"):
        terminal = next(
            s
            for s in service.store.all("subscription")
            if s["client_id"] == client and s["name"] == "call.completed"
        )
        service.store.db.execute(
            "UPDATE deliveries SET status='delivered' WHERE sub_id=?", (terminal["id"],)
        )
        events.close_completed_monitoring()
        assert not any(
            s["active"] for s in service.store.all("subscription") if s["client_id"] == client
        )
        if client == "client":
            assert all(
                s["active"]
                for s in service.store.all("subscription")
                if s["client_id"] == "second-client"
            )


async def test_unsubscribe_documented_identity_client_isolation(service, brief):
    call_id = service.prepare("owner", brief)["call_id"]
    events = await subscribed(service, call_id, FakeSender())
    identity = {
        "name": "call.completed",
        "arguments": {"call_id": call_id},
        "delivery": {"mode": "webhook", "url": "https://receiver.example/events"},
    }
    events.unsubscribe("owner", "other-client", identity)
    assert service.subscriptions_ready("owner", call_id)
    events.unsubscribe("owner", "client", identity)
    events.unsubscribe("owner", "client", identity)
    assert not service.subscriptions_ready("owner", call_id)
    assert all(e["delivery"] == ["webhook"] for e in events.catalog()["events"])


async def test_dns_mixed_private_public_is_rejected(monkeypatch):
    import asyncio
    import socket

    from dots_phone.events import PublicResolver

    async def resolve(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in ("8.8.8.8", "127.0.0.1")
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(Refused, match="public"):
        await PublicResolver().resolve("receiver.example")
