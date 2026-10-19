"""Budget-derived duration is frozen before approval; no provider or model calls."""

from decimal import Decimal

import pytest

from dots_phone.call_budget import affordable_seconds, reservation_usd
from dots_phone.models import Brief
from dots_phone.service import Refused, digest


@pytest.mark.parametrize(
    ("budget", "ceiling", "expected"),
    [("2", 600, 379), ("3", 600, 600), ("3", 3600, 651), ("2", 120, 120)],
)
@pytest.mark.parametrize("omit", [True, False])
def test_preparation_resolves_budget_before_document_and_grant(
    service, brief, budget, ceiling, expected, omit
):
    data = brief.model_dump()
    data.update(budget_usd=budget, max_seconds=None)
    if omit:
        del data["max_seconds"]
    service.settings.max_seconds = ceiling
    service.settings.call_authorization_mode = "connected_owner"
    prepared = service.prepare("owner", Brief.model_validate(data), client_id="client")
    call = service.call("owner", prepared["call_id"])
    assert call["brief"]["max_seconds"] == expected
    assert call["brief"]["budget_usd"] == budget
    assert Decimal(call["estimate_usd"]) <= Decimal(budget)
    assert call["brief_hash"] == digest(call["brief"])
    grant = service.store.get("approval", prepared["authorization_id"], "owner")
    assert grant["hash"] == call["brief_hash"]
    assert str(expected) in call["prep_document"]["markdown"]
    assert not service.provider.dials


@pytest.mark.parametrize("budget", ["0.61", "0.7", "1.9999", "2", "2.0001", "3", "19.9999"])
def test_maximal_duration_never_rounds_past_budget(budget):
    amount = Decimal(budget)
    seconds = affordable_seconds(amount, 3600)
    if seconds:
        assert reservation_usd(seconds) <= amount
    if seconds < 3600:
        assert reservation_usd(seconds + 1) > amount


def test_explicit_user_duration_is_preserved_and_not_extended(service, brief):
    prepared = service.prepare("owner", brief.model_copy(update={"max_seconds": 180}))
    assert prepared["brief"]["max_seconds"] == 180


@pytest.mark.parametrize(("seconds", "error"), [(601, "duration exceeds"), (500, "budget below")])
def test_explicit_unaffordable_or_over_installation_duration_is_refused(
    service, brief, seconds, error
):
    with pytest.raises(Refused, match=error):
        service.prepare("owner", brief.model_copy(update={"max_seconds": seconds}))
    assert not service.provider.dials


def test_too_small_budget_refused_without_increasing_spend(service, brief):
    with pytest.raises(Refused, match="budget below"):
        service.prepare(
            "owner", brief.model_copy(update={"max_seconds": None, "budget_usd": Decimal("0.5")})
        )
    assert not service.store.all("call")
