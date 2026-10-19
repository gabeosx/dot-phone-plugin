import time
from types import SimpleNamespace

import pytest

from dots_phone.audio_campaign import approve, reserve
from dots_phone.harness import (
    assistant_context,
    load_cases,
    meaningful_latency,
    percentile,
    scenario,
)
from dots_phone.service import Refused


def test_harness_scenarios_do_not_expose_grader_or_hidden_facts():
    cases = load_cases()
    assert {c["split"] for c in cases} == {"heldout", "regression"}
    for case in cases:
        scenario(case)
        context = assistant_context(case)
        assert "HIDDEN-RECIPIENT-ANSWER" not in context
        assert "SEEDED-OWNER-SECRET" not in context
        assert case["simulator"]["facts"]["hidden_seed"] not in context


def test_campaign_combined_seconds_money_and_ownership(service):
    cases = load_cases()
    id = approve(service.store, "owner", cases)
    with pytest.raises(Refused):
        reserve(service.store, "other", id, cases[0], cases)
    for _ in range(16):
        reserve(service.store, "owner", id, cases[0], cases)
    campaign = service.store.get("audio_campaign", id, "owner")
    assert campaign["reserved_usd"] == "4.00"
    assert campaign["seconds_reserved"] == 3520
    with pytest.raises(Refused, match="exhausted"):
        reserve(service.store, "owner", id, cases[0], cases)


def test_campaign_rejects_changed_scenarios_revocation_expiry(service):
    cases = load_cases()
    id = approve(service.store, "owner", cases)
    with pytest.raises(Refused, match="outside"):
        reserve(service.store, "owner", id, cases[0], cases[1:])
    row = service.store.get("audio_campaign", id, "owner")
    row["expires"] = time.time() - 1
    service.store.put("audio_campaign", id, "owner", row)
    with pytest.raises(Refused):
        reserve(service.store, "owner", id, cases[0], cases)


def test_substantive_metric_does_not_credit_filler_or_receipt_time():
    timeline = SimpleNamespace(
        turns=[
            SimpleNamespace(
                role="assistant", start_ms=1100, end_ms=1500, transcript="Let me check."
            ),
            SimpleNamespace(
                role="assistant",
                start_ms=3500,
                end_ms=4000,
                transcript="November sixth is available.",
            ),
        ]
    )
    turns = [{"end_ms": 1000, "window_end_ms": 5000, "audio": {"response_expected": True}}]
    values, missing = meaningful_latency(timeline, turns, ["available"])
    assert values == [3000] and missing == 0
    values, missing = meaningful_latency(timeline, turns, ["denied"])
    assert values == [] and missing == 1
    assert percentile([]) is None


async def test_delegation_reply_does_not_block_live_receiver(monkeypatch):
    import asyncio

    from dots_phone.harness import PhoneAudioParticipant

    barrier = asyncio.Event()

    async def controller(transcript):
        await barrier.wait()
        return "Authorized fixture answer"

    participant = PhoneAudioParticipant(
        api_key="synthetic", instructions="Synthetic", controller=controller
    )
    sent = []

    async def send(event):
        sent.append(event)

    monkeypatch.setattr(participant, "_send_live", send)
    await asyncio.wait_for(
        participant._observe_event(
            {
                "type": "session.delegation.created",
                "delegation": {"target": "client", "id": "delegation-1"},
            }
        ),
        0.1,
    )
    await participant._observe_event(
        {"type": "session.input_transcript.delta", "delta": "Stop calling."}
    )
    assert participant.transcript[-1]["text"] == "Stop calling."
    barrier.set()
    await asyncio.gather(*participant.supervision_tasks)
    assert sent[0]["delegation_id"] == "delegation-1"


async def test_finalization_uses_documented_fifteen_second_bound(monkeypatch):
    import asyncio
    import json

    import aiohttp

    from dots_phone.harness import PhoneAudioParticipant

    gate = asyncio.Event()
    timeouts = []
    original = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        timeouts.append(timeout)
        return await original(awaitable, timeout)

    class Socket:
        closed = False

        async def send_json(self, message):
            assert message["type"] == "session.close"
            asyncio.get_running_loop().call_later(0.01, gate.set)

        def __aiter__(self):
            return self.receive()

        async def receive(self):
            await gate.wait()
            yield SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"type": "session.closed", "usage": {"seconds": 8}}),
            )

        async def close(self):
            self.closed = True

    participant = PhoneAudioParticipant(api_key="synthetic", instructions="Synthetic")
    participant.ws = Socket()
    participant._started = True
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await participant.close()
    assert timeouts == [15]
    assert participant.confirmed_seconds == 8


async def test_harness_retains_finalization_failure_and_partial_usage(tmp_path, monkeypatch):
    import json

    from dots_phone.harness import evaluate_audio_case

    class Participant:
        def __init__(self, **options):
            self.confirmed_seconds = 33 if "independent" in options["instructions"] else None
            self.transcript = []
            self.supervision_times = []
            self.supervision_failures = 0
            self.start_attempts = 1

    class Runner:
        def __init__(self, *args, **options):
            self.directory = options["save_conversations"]
            self.timeline = SimpleNamespace(turns=[])

        async def run(self):
            self.directory.joinpath("conversation.result.json").write_text(
                json.dumps(
                    {
                        "scenario_id": "hotel_happy",
                        "task_status": "error",
                        "termination_reason": "session_close_failed",
                        "turn_metrics": [],
                        "response_latencies_ms": [],
                        "yield_latencies_ms": [],
                    }
                )
            )
            raise RuntimeError("PRIVATE-VENDOR-ERROR-DO-NOT-REPORT")

    monkeypatch.setattr("dots_phone.harness.PhoneAudioParticipant", Participant)
    monkeypatch.setattr("dots_phone.harness.DualGptLiveRunner", Runner)
    result = await evaluate_audio_case(load_cases()[0], "synthetic", tmp_path / "audio")
    assert result["provider_failure_type"] == "RuntimeError"
    assert not result["audio_gate"]["full_acceptance_passed"]
    assert not result["hard_assertions"]["both_voice_usage_confirmed"]
    assert result["usage_accounting"]["simulator_seconds"] == 33
    assert result["usage_accounting"]["assistant_seconds"] is None
    assert "PRIVATE-VENDOR-ERROR" not in json.dumps(result)
