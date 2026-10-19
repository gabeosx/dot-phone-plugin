from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from dots_phone.audio_campaign import approve, run
from dots_phone.config import Settings, live_number
from dots_phone.harness import load_cases
from dots_phone.service import Refused


def test_fictional_allowlist_cannot_be_activated():
    assert not live_number("+12125550100")
    assert not live_number("+12125550199")
    assert live_number("+12125550200")
    with pytest.raises(ValidationError):
        Settings(
            public_base_url="https://phone.example",
            owner_secret_hash="$argon2id$synthetic",
            encryption_key="synthetic",
            dry_run=False,
            phone_number="+12125550100",
            allowed_numbers=["+12125550101"],
            openai_key="synthetic",
            twilio_account="synthetic",
            twilio_auth="synthetic",
            twilio_key="synthetic",
            twilio_secret="synthetic",
        )


def test_live_task_destination_policy_starts_without_per_number_configuration():
    settings = Settings(
        public_base_url="https://phone.example",
        owner_secret_hash="$argon2id$synthetic",
        encryption_key="synthetic",
        dry_run=False,
        phone_number="+12125550200",
        openai_key="synthetic",
        twilio_account="synthetic",
        twilio_auth="synthetic",
        twilio_key="synthetic",
        twilio_secret="synthetic",
    )
    assert settings.allowed_numbers == []
    assert settings.destination_allowed("+12125550201")
    assert not settings.destination_allowed("+12125550100")
    assert not settings.destination_allowed("911")
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "destination_policy": "allowlist"})


def test_queue_time_can_fit_same_monetary_budget_without_bypassing_installation_cap(service, brief):
    longer = brief.model_copy(update={"max_seconds": 600, "budget_usd": Decimal("3")})
    prepared = service.prepare("owner", longer)
    assert prepared["brief"]["max_seconds"] == 600
    assert prepared["brief"]["budget_usd"] == "3"
    assert float(prepared["estimate_usd"]) < 3
    with pytest.raises(Refused, match="budget below"):
        service.prepare("owner", longer.model_copy(update={"budget_usd": Decimal("2")}))
    service.settings.max_seconds = 300
    with pytest.raises(Refused, match="configured bound .*300"):
        service.prepare("owner", longer)


async def test_failed_audio_run_keeps_reservation_freezes_and_records_safe_error(
    service, tmp_path, monkeypatch
):
    cases = load_cases()
    campaign_id = approve(service.store, "owner", cases)

    async def failure(*args, **kwargs):
        raise RuntimeError("PRIVATE-PROVIDER-DETAIL-MUST-NOT-APPEAR")

    monkeypatch.setattr("dots_phone.harness.evaluate_audio_case", failure)
    with pytest.raises(RuntimeError):
        await run(
            service.store,
            "owner",
            campaign_id,
            cases,
            "synthetic",
            tmp_path / "audio",
            cases[0]["id"],
        )
    campaign = service.store.get("audio_campaign", campaign_id, "owner")
    assert campaign["revoked"] and campaign["reserved_usd"] == "0.25"
    recorded = service.store.all("audio_run", "owner")[0]
    assert recorded["state"] == "unconfirmed"
    assert recorded["failure_type"] == "RuntimeError"
    assert (
        "PRIVATE-PROVIDER-DETAIL"
        not in Path(tmp_path / "audio" / recorded["id"] / "failure.json").read_text()
    )
    # Even an accidental administrative unfreeze cannot restart ambiguous work.
    campaign["revoked"] = False
    service.store.put("audio_campaign", campaign_id, "owner", campaign)
    with pytest.raises(Refused, match="reconciliation"):
        await run(service.store, "owner", campaign_id, cases, "synthetic", tmp_path / "audio")
    assert len(service.store.all("audio_run", "owner")) == 1


async def test_unwritable_artifact_storage_fails_before_reservation(service, tmp_path, monkeypatch):
    cases = load_cases()
    campaign_id = approve(service.store, "owner", cases)
    original = Path.write_bytes

    def fail_probe(path, data):
        if path.name.startswith(".write-probe-"):
            raise OSError("read-only synthetic test")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_probe)
    with pytest.raises(OSError):
        await run(service.store, "owner", campaign_id, cases, "synthetic", tmp_path / "audio")
    assert service.store.all("audio_run", "owner") == []
    assert service.store.get("audio_campaign", campaign_id, "owner")["runs"] == 0
