import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def test_clerk_publishes_new_source_only_after_connection_scenario_runs(tmp_path, monkeypatch):
    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (location / "hotel_clerk.py").exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    assets, mailbox = tmp_path / "clerk", tmp_path / "mail"
    assets.mkdir()
    mailbox.mkdir()
    (assets / "hotel-scenario.json").write_text(
        json.dumps({"call_id": "a" * 32, "max_seconds": 180, "mailbox_path": str(mailbox)})
    )
    for name in ("a", "b", "c"):
        directory = assets / "clerk-variants" / name
        directory.mkdir(parents=True)
        (directory / "late-message.json").write_text(
            json.dumps({"id": "jordan-note", "text": "A previously unseen fact."})
        )
    monkeypatch.setattr(module.secrets, "choice", lambda choices: choices[-1])
    record = {}
    scenario = module.HotelClerkScenario(
        None,
        "MZfixture",
        assets,
        record,
        decision_marker=tmp_path / "marker.json",
        receiver_sid="CAfixture",
    )
    assert not list(mailbox.iterdir())
    assert set(record["question_order"]) == {"identity", "dates", "reference"}
    scenario.publish_new_mail()
    message = json.loads((mailbox / "jordan-note.json").read_text())
    assert message["text"] == "A previously unseen fact." and isinstance(message["sent_at"], float)
    assert record["late_mail"]["source_sha256"]


@pytest.mark.asyncio
async def test_clerk_waits_for_fresh_audio_after_source_and_confirmation(tmp_path, monkeypatch):
    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not location.exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    scenario = object.__new__(module.HotelClerkScenario)
    scenario.record = {"turns": []}
    scenario.question_order = ["identity", "dates", "reference"]
    scenario.hotel = {"call_id": "a" * 32}
    scenario.decision_marker = tmp_path / "marker.json"
    scenario.stopped = __import__("asyncio").Event()
    clock = [10.0]
    scenario.now = lambda: clock[0]
    scenario.publish_new_mail = lambda: None
    scenario.wait_voice = AsyncMock()
    scenario.wait_quiet = AsyncMock()
    observed = []

    async def speak(name):
        clock[0] += 5
        scenario.record["turns"].append({"last_voice_sent_at_seconds": clock[0]})
        if name == "hangup":
            scenario.stopped.set()

    async def marker(suffix):
        clock[0] += 0.2
        return {"marker_observed_at": clock[0]}

    async def answer(name, after):
        observed.append((name, after, clock[0]))

    scenario.speak, scenario.wait_marker, scenario.heard_answer = speak, marker, answer
    await scenario.scenario()
    for name, after, current in observed:
        if name in {"missing", "confirm"}:
            assert after == current, "Old holding audio must not count as a new source answer"


def test_real_mail_clerk_uses_owned_mock_and_never_publishes_fixture_mail(tmp_path, monkeypatch):
    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not location.exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    (tmp_path / "hotel-scenario.json").write_text(
        json.dumps({"mode": "real_mail_mock", "max_seconds": 180, "call_id": "a" * 32})
    )
    record = {}
    scenario = module.RealMailHotelClerkScenario(
        None,
        "MZfixture",
        tmp_path,
        record,
        decision_marker=tmp_path / "decision.json",
        receiver_sid="CAfixture",
    )
    assert set(scenario.live_order) == {"room", "routing", "gmail"}
    assert scenario.live_order[-1] == "gmail" and record["real_hotel_called"] is False
    assert not list(tmp_path.glob("*note*"))


@pytest.mark.asyncio
async def test_real_mail_clerk_rejects_prior_question_read_marker(tmp_path, monkeypatch):
    import asyncio
    import time

    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not location.exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    scenario = object.__new__(module.RealMailHotelClerkScenario)
    scenario.hotel = {"call_id": "a" * 32}
    scenario.receiver_sid = "CAfixture"
    scenario.decision_marker = tmp_path / "decision.json"
    scenario.used_reads = set()
    scenario.stopped = asyncio.Event()
    marker = tmp_path / "CAfixture-context-read.json"
    after = time.time()
    prior = {
        "call_id": "a" * 32,
        "receiver_sid": "CAfixture",
        "request_id": "b" * 32,
        "request_created_ms": (after - 5) * 1000,
    }
    marker.write_text(json.dumps(prior))
    task = asyncio.create_task(scenario.fresh_read(after))
    await asyncio.sleep(0.03)
    assert not task.done()
    marker.write_text(
        json.dumps({**prior, "request_id": "c" * 32, "request_created_ms": (after + 0.1) * 1000})
    )
    result = await task
    assert result["request_id"] == "c" * 32 and "c" * 32 in scenario.used_reads


def test_transient_partial_marker_is_retried_instead_of_hanging_up(tmp_path, monkeypatch):
    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not location.exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    marker = tmp_path / "marker.json"
    marker.write_text("{")
    assert module.read_marker(marker) is None
    marker.write_text('{"call_id":"verified"}')
    assert module.read_marker(marker)["call_id"] == "verified"


@pytest.mark.asyncio
async def test_mock_clerk_recognizes_scoped_reconfirmation_and_prioritizes_verified_success(
    tmp_path, monkeypatch
):
    import asyncio

    location = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not location.exists():
        location = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(location))
    module = importlib.import_module("hotel_clerk")
    scenario = object.__new__(module.RealMailHotelClerkScenario)
    scenario.hotel = {"call_id": "a" * 32}
    scenario.receiver_sid = "CAfixture"
    scenario.decision_marker = tmp_path / "decision.json"
    scenario.stopped = asyncio.Event()
    marker = {
        "call_id": "a" * 32,
        "receiver_sid": "CAfixture",
        "request_id": "b" * 32,
        "completion_verified": False,
    }
    (tmp_path / "CAfixture-confirmation-retry.json").write_text(json.dumps(marker))
    status, value = await scenario.wait_confirmation_or_retry()
    assert status == "retry" and value == marker
    (tmp_path / "CAfixture-hotel-confirmed.json").write_text(
        json.dumps({**marker, "completion_verified": True})
    )
    assert (await scenario.wait_confirmation_or_retry())[0] == "confirmed"
