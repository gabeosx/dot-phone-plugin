import pytest
from loguru import logger
from pipecat.services.openai.live import llm as live_llm

from bot import MeasuredLiveService
from run_baseline import run


@pytest.fixture(autouse=True)
def quiet():
    logger.remove()


async def test_real_adapter_and_eval_harness_complete_local_conversation():
    report = await run()
    assert report["baseline_passed"], report["eval_failures"]
    assert report["provider_requests"] == report["pstn_calls"] == 0


async def test_audio_sent_before_provider_ready_is_buffered():
    report = await run(start_delay=1)
    assert report["baseline_passed"], report["eval_failures"]
    started = next(e["at_ms"] for e in report["timeline"] if e["kind"] == "session_started")
    sent = next(e["at_ms"] for e in report["timeline"] if e["kind"] == "turn_sent")
    audio = next(e["at_ms"] for e in report["timeline"] if e["kind"] == "input_audio")
    assert sent < started <= audio


async def test_hangup_drains_delayed_backend_continuation_before_closing():
    report = await run(end_completion_delay=0.3, close_delay=0.5)
    assert report["baseline_passed"], report["eval_failures"]
    assert not report["fixture_post_close_events"]
    close = next(e["at_ms"] for e in report["timeline"] if e["kind"] == "close_requested")
    continuations = [
        e["at_ms"]
        for e in report["timeline"]
        if e["kind"] == "client_event" and e["event_type"] == "ResponseCreateEvent"
    ]
    assert len(continuations) == 2
    assert all(at < close for at in continuations)


async def test_missing_drain_reproduces_live_closing_error(monkeypatch):
    async def no_drain(self):
        pass

    monkeypatch.setattr(MeasuredLiveService, "drain_delegations", no_drain)
    report = await run(end_completion_delay=0.3, close_delay=0.5)
    assert report["eval_passed"]
    assert not report["baseline_passed"]
    assert "response.create" in report["fixture_post_close_events"]
    assert any("session is closing" in e.get("message", "") for e in report["errors"])


@pytest.mark.parametrize("failure", ["omit_closed", "omit_usage", "cancel"])
async def test_eval_success_never_overrides_unconfirmed_provider_teardown(failure, monkeypatch):
    monkeypatch.setattr(live_llm, "SESSION_CLOSE_TIMEOUT_SECS", 0.05)
    report = await run(**{failure: True})
    assert report["eval_passed"], report["eval_failures"]
    assert not report["baseline_passed"]
    if failure in {"omit_closed", "cancel"}:
        assert not report["hard_assertions"]["provider_closed"]
    assert not report["hard_assertions"]["final_usage_confirmed"]
