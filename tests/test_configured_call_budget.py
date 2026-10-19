"""Worker capabilities share the service's budget policy; no paid requests."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from dots_phone.account_task import ACCOUNT_ACTION, RESULT_ACTION
from dots_phone.service import Refused, digest
from tests.test_general_call_reasoner import prepare_general, start


def prepare_task(service, brief, tmp_path, task):
    service.settings.connected_call_budget_usd = Decimal("5")
    service.settings.connected_daily_budget_usd = Decimal("10")
    return prepare_general(
        service,
        brief,
        tmp_path,
        budget_usd="4.50",
        actions=[ACCOUNT_ACTION if task == "account" else "ask_question", RESULT_ACTION],
    )


@pytest.mark.parametrize("task", ["account", "general"])
async def test_approved_budget_above_three_reaches_worker_and_fake_dial(
    service, brief, tmp_path, task
):
    prepared = prepare_task(service, brief, tmp_path, task)
    worker = service.local_context_worker
    worker.before_dial = AsyncMock()
    assert worker.scope(service.call("owner", prepared["call_id"]))["read_only_" + task]
    start(service, prepared)
    await service.tick()
    call = service.call("owner", prepared["call_id"])
    assert worker.scope(call)["read_only_" + task]
    assert service.provider.dials == [prepared["call_id"]]
    assert service.connected_reserved_budget("owner") == Decimal("4.50")


@pytest.mark.parametrize("task", ["account", "general"])
@pytest.mark.parametrize("guard", ["per_call", "rolling"])
def test_configured_budgets_still_block_start(service, brief, tmp_path, task, guard):
    prepared = prepare_task(service, brief, tmp_path, task)
    if guard == "per_call":
        service.settings.connected_call_budget_usd = Decimal("4")
    else:
        service.settings.connected_daily_budget_usd = Decimal("4")
    with pytest.raises(Refused, match="call budget"):
        start(service, prepared)
    assert not service.provider.dials
    assert service.store.all("connected_call_reservation") == []


@pytest.mark.parametrize("task", ["account", "general"])
def test_higher_configured_ceiling_does_not_expand_approved_budget(service, brief, tmp_path, task):
    prepared = prepare_task(service, brief, tmp_path, task)
    start(service, prepared)
    call = service.call("owner", prepared["call_id"])
    # This still fits configuration, but the owner authorized only $4.50.
    call["brief"]["budget_usd"] = "4.75"
    call["brief_hash"] = digest(call["brief"])
    with pytest.raises(Refused, match="start grant"):
        service.local_context_worker.scope(call)
    assert service.connected_reserved_budget("owner") == Decimal("4.50")
