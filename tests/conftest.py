from __future__ import annotations

import json
from datetime import timedelta

import pytest

from dots_phone.config import Settings
from dots_phone.events import Events
from dots_phone.models import Brief, now
from dots_phone.service import PhoneService
from dots_phone.store import Store


class FakeProvider:
    def __init__(self):
        self.dials, self.stops, self.updates = [], [], []
        self.fail_dial = self.fail_stop = False
        self.on_dial = None

    async def dial(self, call):
        self.dials.append(call["call_id"])
        if self.on_dial:
            await self.on_dial(call)
        if self.fail_dial:
            raise TimeoutError()
        return "CA" + "1" * 32

    async def stop(self, call):
        self.stops.append(call["provider_sid"])
        if self.fail_stop:
            raise TimeoutError()

    async def close_voice(self, call):
        pass

    async def update(self, call, delegation, text):
        self.updates.append((delegation, text))

    async def steer(self, call, instruction):
        self.updates.append((None, instruction))


class FakeSender:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.echo = True

    def validate_url(self, url):
        from dots_phone.events import SafeWebhook

        SafeWebhook(["receiver.example"]).validate_url(url)

    async def post(self, url, body, headers):
        self.requests.append((url, body, headers))
        payload = json.loads(body)
        response = json.dumps(
            {"challenge": payload.get("challenge", "") if self.echo else "wrong"}
        ).encode()
        return self.status, response


@pytest.fixture
def settings(tmp_path):
    return Settings(
        profile="evaluation",
        database=tmp_path / "state.db",
        allowed_numbers=["+12125550100", "+12125550101"],
        destination_policy="allowlist",
        callback_hosts=["receiver.example"],
        redirect_uris=["https://chatgpt.com/connector_platform/oauth_redirect"],
    )


@pytest.fixture
def service(settings):
    store = Store(settings.database, settings.encryption_key.get_secret_value())
    provider = FakeProvider()
    service = PhoneService(settings, store, provider)
    service.ready = True
    yield service
    service.store.close()


@pytest.fixture
def brief():
    return Brief(
        destination="+12125550100",
        purpose="Ask fictional hotel about a synthetic booking",
        deadline=now() + timedelta(hours=1),
        budget_usd="2",
        max_seconds=300,
        facts=[
            {
                "name": "date",
                "value": "2026-11-06",
                "provenance": "synthetic fixture",
                "verified_at": now() - timedelta(minutes=1),
                "expires_at": now() + timedelta(hours=1),
            }
        ],
        disclosures=["date"],
        actions=["ask_availability"],
    )


async def subscribed(service, call_id, sender=None):
    import base64

    events = Events(service, sender or FakeSender())
    for name in ("call.completed", "call.failed", "call.decision_required"):
        await events.subscribe(
            "owner",
            "client",
            {
                "name": name,
                "arguments": {"call_id": call_id},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/events",
                    "secret": "whsec_" + base64.b64encode(b"s" * 32).decode(),
                },
            },
        )
    return events


async def started(service, brief):
    from dots_phone.models import Start

    call_id = service.prepare("owner", brief)["call_id"]
    await subscribed(service, call_id)
    authorization = service.approve("owner", call_id)
    service.settings.dry_run = False  # Injected fake provider; no external calls.
    params = Start(call_id=call_id, revision=1, authorization_id=authorization)
    service.start("owner", params)
    await service.tick()
    return call_id, params
