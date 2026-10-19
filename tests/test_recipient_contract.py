import time

import httpx
import pytest
from twilio.request_validator import RequestValidator

from dots_phone.harness import load_cases
from dots_phone.service import Refused
from dots_phone.test_recipient import (
    TEST_KEYS,
    counterparty_config,
    create_recipient,
    validate_scope,
)
from tests.conftest import FakeProvider


def scope():
    return {
        "campaign_id": "synthetic-owner-approved-fixture",
        "approval_reference": "offline-human-approval-fixture",
        "public_base_url": "https://recipient.example",
        "caller": "+12125550200",
        "recipient": "+12125550201",
        "expires": time.time() + 300,
        "max_calls": 1,
        "max_seconds": 60,
        "budget_usd": "2",
        "scenarios": ["hotel_happy"],
    }


def test_recipient_context_excludes_grader_and_production_brief():
    case = load_cases()[0]
    case["grading"]["answer"] = "GRADER-EXPECTATION-MUST-NOT-LEAK"
    case["brief"]["purpose"] = "PRIVATE-OWNER-BRIEF-MUST-NOT-LEAK"
    instructions = counterparty_config(case)["instructions"]
    assert "GRADER-EXPECTATION-MUST-NOT-LEAK" not in instructions
    assert "PRIVATE-OWNER-BRIEF-MUST-NOT-LEAK" not in instructions
    assert counterparty_config(case)["audio"]["format"]["type"] == "audio/pcmu"


def test_recipient_refuses_expired_or_fictional_routes():
    value = scope()
    value["recipient"] = "+12125550100"
    with pytest.raises(Refused):
        validate_scope(value)
    value = scope()
    value["expires"] = time.time() - 1
    with pytest.raises(Refused):
        validate_scope(value)


async def test_inbound_signature_owned_route_cap_and_restart_persistence(tmp_path):
    values = dict.fromkeys(TEST_KEYS, "synthetic")
    provider = FakeProvider()
    app = create_recipient(scope(), values, tmp_path, provider)
    form = {
        "AccountSid": "synthetic",
        "CallSid": "CA" + "1" * 32,
        "From": "+12125550200",
        "To": "+12125550201",
    }
    validator = RequestValidator("synthetic")
    url = "https://recipient.example/twilio/inbound/hotel_happy"
    signature = validator.compute_signature(url, form)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://recipient.example"
    ) as client:
        assert (await client.post(url, data=form)).status_code == 403
        bad = {**form, "From": "+12125550202"}
        assert (
            await client.post(
                url, data=bad, headers={"x-twilio-signature": validator.compute_signature(url, bad)}
            )
        ).status_code == 403
        accepted = await client.post(url, data=form, headers={"x-twilio-signature": signature})
        assert accepted.status_code == 200 and "wss://recipient.example/voice" in accepted.text
        assert len(app.state.service.store.all("call")) == 1
        assert not provider.dials
        assert (
            await client.post(url, data=form, headers={"x-twilio-signature": signature})
        ).status_code == 409
    service = app.state.service
    call = service.store.all("call")[0]
    await service.end("test", call["call_id"])
    service.store.close()
    restarted = create_recipient(scope(), values, tmp_path, provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="https://recipient.example"
    ) as client:
        assert (
            await client.post(url, data=form, headers={"x-twilio-signature": signature})
        ).status_code == 409
    restarted.state.service.store.close()
