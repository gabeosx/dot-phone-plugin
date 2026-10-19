import asyncio
import re
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from dots_phone.models import Start, now
from dots_phone.oauth import HASHER
from dots_phone.phone_test_campaign import approve, brief_scope
from dots_phone.server import create_app
from dots_phone.service import Refused, digest
from dots_phone.store import Store
from tests.conftest import FakeProvider, FakeSender, subscribed


@pytest.fixture
def museum(settings, brief):
    settings.phone_number = "+12125550101"
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    return brief.model_copy(
        update={
            "purpose": "Synthetic museum interruption and hours lookup",
            "facts": [],
            "disclosures": [],
            "max_seconds": 90,
            "actions": ["tell_fictional_story", "lookup_fictional_hours"],
            "deadline": now() + timedelta(minutes=30),
        }
    )


def grant(service, museum):
    template = service.prepare("owner", museum)["call_id"]
    return approve(service, "owner", template)


async def test_campaign_returns_fresh_call_bound_grants_and_still_requires_subscriptions(
    service, museum
):
    campaign = grant(service, museum)
    first = service.prepare("owner", museum)
    second = service.prepare(
        "owner", museum.model_copy(update={"deadline": now() + timedelta(minutes=20)})
    )
    assert first["campaign_id"] == second["campaign_id"] == campaign
    assert first["authorization_id"] != second["authorization_id"]
    assert first["approval_url"] is None
    assert "approved_test_campaign" in first["approval_requirements"]
    service.settings.dry_run = False
    start = Start(call_id=first["call_id"], revision=1, authorization_id=first["authorization_id"])
    with pytest.raises(Refused, match="subscribe"):
        service.start("owner", start)
    await subscribed(service, first["call_id"])
    service.start("owner", start)
    await service.tick()
    assert service.provider.dials == [first["call_id"]]
    assert service.start("owner", start)["state"] == "active"
    assert service.store.get("campaign", campaign, "owner")["calls"] == 2


def test_unknown_billing_keeps_full_campaign_reservations_across_restart(service, museum):
    campaign = grant(service, museum)
    first = service.prepare("owner", museum)
    before = service.store.get("campaign", campaign, "owner")
    auth = service.campaign_authorization(
        "owner", campaign, first["call_id"], digest(brief_scope(museum.model_dump(mode="json")))
    )
    assert auth == first["authorization_id"]
    assert service.store.get("campaign", campaign, "owner") == before
    service.store.close()
    service.store = Store(
        service.settings.database, service.settings.encryption_key.get_secret_value()
    )
    for _ in range(9):
        assert service.prepare("owner", museum)["authorization_id"]
    exhausted = service.prepare("owner", museum)
    assert exhausted["authorization_id"] is None
    row = service.store.get("campaign", campaign, "owner")
    assert row["calls"] == 10 and row["seconds"] == 2400 and Decimal(row["reserved_usd"]) == 20
    assert not service.provider.dials


@pytest.mark.parametrize(
    "change",
    [
        {"actions": ["make_purchase"]},
        {"purpose": "different task"},
        {"synthetic": False},
        {"max_seconds": 91},
        {"budget_usd": Decimal("2.01")},
    ],
)
def test_campaign_never_authorizes_a_changed_scope(service, museum, change):
    campaign = grant(service, museum)
    call = service.prepare("owner", museum.model_copy(update=change))
    assert call["authorization_id"] is None
    assert service.store.get("campaign", campaign, "owner")["calls"] == 0


def test_campaign_owner_and_provider_binding(service, museum):
    grant(service, museum)
    assert service.prepare("other-owner", museum)["authorization_id"] is None
    service.settings.phone_number = "+12125550199"
    assert service.prepare("owner", museum)["authorization_id"] is None


@pytest.mark.parametrize("reason", ["expired", "revoked", "provider_changed"])
async def test_standing_grant_is_rechecked_at_start(service, museum, reason):
    campaign_id = grant(service, museum)
    call = service.prepare("owner", museum)
    await subscribed(service, call["call_id"])
    service.settings.dry_run = False
    row = service.store.get("campaign", campaign_id, "owner")
    if reason == "expired":
        row["expires"] = time.time() - 1
    elif reason == "revoked":
        row["revoked"] = True
    else:
        service.settings.twilio_account = SecretStr("AC" + "3" * 32)
    service.store.put("campaign", campaign_id, "owner", row)
    with pytest.raises(Refused):
        service.start(
            "owner",
            Start(call_id=call["call_id"], revision=1, authorization_id=call["authorization_id"]),
        )
    assert not service.provider.dials


def test_owner_page_binds_campaign_policy_authenticates_and_revokes_active_call(settings, museum):
    password = "Synthetic-owner-password-123456"
    settings.owner_secret_hash = SecretStr(HASHER.hash(password))
    settings.dry_run = False
    app = create_app(settings, FakeProvider(), FakeSender())
    service = app.state.service
    template = service.prepare("owner", museum)["call_id"]
    url = "/owner/campaign/phone/" + template
    with TestClient(app) as client:
        assert client.post(url, data={"approved": "true"}).status_code == 403
        page = client.get(url)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        original = service.call("owner", template)
        changed = service.call("owner", template)
        changed["brief"]["purpose"] = "scope changed after page was viewed"
        service.save(changed)
        assert (
            client.post(
                url,
                data={"csrf": csrf, "password": password},
                headers={"origin": settings.public_base_url},
            ).status_code
            == 403
        )
        assert not service.store.all("campaign", "owner")
        service.save(original)
        page = client.get(url)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        assert (
            client.post(
                url,
                data={"csrf": csrf, "password": password},
                headers={"origin": settings.public_base_url},
            ).status_code
            == 200
        )
        campaign = service.store.all("campaign", "owner")[0]
        assert (
            client.post(
                url,
                data={"csrf": csrf, "password": password},
                headers={"origin": settings.public_base_url},
            ).status_code
            == 403
        )
        call = service.prepare("owner", museum)
        asyncio.run(subscribed(service, call["call_id"]))
        service.start(
            "owner",
            Start(call_id=call["call_id"], revision=1, authorization_id=call["authorization_id"]),
        )
        asyncio.run(service.tick())
        revoke = "/owner/campaign/phone/" + campaign["id"] + "/revoke"
        page = client.get(revoke)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        assert (
            client.post(
                revoke,
                data={"csrf": csrf, "password": password},
                headers={"origin": settings.public_base_url},
            ).status_code
            == 200
        )
        assert service.get("owner", call["call_id"])["state"] == "cancelled"
        assert service.store.get("campaign", campaign["id"], "owner")["revoked"]
        assert service.prepare("owner", museum)["authorization_id"] is None
