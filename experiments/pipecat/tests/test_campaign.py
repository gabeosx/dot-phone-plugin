import pytest

from run_campaign import CAMPAIGN_SCOPE, run_campaign


async def test_campaign_stops_after_unknown_usage_and_preserves_reservation(tmp_path):
    calls = []

    async def evaluator(key, scenario, directory, **kwargs):
        calls.append(directory)
        return {
            "baseline_passed": False,
            "usage_confirmed": False,
            "final_seconds": None,
            "backend_responses": 0,
        }

    approval = {**CAMPAIGN_SCOPE, "approval_reference": "fixture"}
    summary = await run_campaign("fixture", None, tmp_path, approval, evaluator)
    assert len(calls) == 1
    assert not summary["baseline_passed"]
    with pytest.raises(FileExistsError):
        await run_campaign("fixture", None, tmp_path, approval, evaluator)
