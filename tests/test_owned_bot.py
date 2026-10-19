import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator


@pytest.fixture
def bot(tmp_path):
    root = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (root / "owned_bot.py").exists():
        root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("owned_bot_fixture", root / "owned_bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    validator = RequestValidator("fixture-token")
    app.include_router(
        module.create_owned_bot_router(
            origin="https://media.example",
            caller="+12125550100",
            number="+12125550101",
            account_sid="AC" + "2" * 32,
            validator=validator,
            assets=tmp_path,
            directory=tmp_path,
        )
    )
    return module, app, validator, tmp_path


def test_caller_requires_exact_owned_endpoints_and_its_own_signature(bot):
    _, app, validator, directory = bot
    data = {
        "AccountSid": "AC" + "2" * 32,
        "CallSid": "CA" + "4" * 32,
        "From": "+12125550100",
        "To": "+12125550101",
    }
    with TestClient(app) as client:
        assert client.post("/twilio/owned-bot", data=data).status_code == 403
        wrong = {**data, "To": "+12125550102"}
        signature = validator.compute_signature("https://media.example/twilio/owned-bot", wrong)
        assert (
            client.post(
                "/twilio/owned-bot", data=wrong, headers={"x-twilio-signature": signature}
            ).status_code
            == 403
        )
        signature = validator.compute_signature("https://media.example/twilio/owned-bot", data)
        response = client.post(
            "/twilio/owned-bot", data=data, headers={"x-twilio-signature": signature}
        )
        assert response.status_code == 200
        xml = ElementTree.fromstring(response.text)
        permit = xml.find("./Connect/Stream/Parameter").attrib["value"]
        assert permit not in (directory / "owned-bot-status.json").read_text()
        duplicate = client.post(
            "/twilio/owned-bot", data=data, headers={"x-twilio-signature": signature}
        )
        assert "Stream" not in duplicate.text
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/owned-bot/media"):
                pass
        assert closed.value.code == 1008
        ws_signature = validator.compute_signature("https://media.example/owned-bot/media", {})
        with client.websocket_connect(
            "/owned-bot/media", headers={"x-twilio-signature": ws_signature}
        ) as ws:
            ws.send_json(
                {
                    "event": "start",
                    "start": {
                        "accountSid": data["AccountSid"],
                        "callSid": data["CallSid"],
                        "customParameters": {"permit": "wrong"},
                    },
                }
            )
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008


def test_receiver_creates_new_output_directory_before_accepting_webhook(bot, tmp_path):
    module, _, validator, _ = bot
    directory = tmp_path / "fresh-attempt" / "receiver"
    app = FastAPI()
    app.include_router(
        module.create_owned_bot_router(
            origin="https://media.example",
            caller="+12125550100",
            number="+12125550101",
            account_sid="AC" + "2" * 32,
            validator=validator,
            assets=tmp_path,
            directory=directory,
        )
    )
    assert directory.is_dir()
    data = {
        "AccountSid": "AC" + "2" * 32,
        "CallSid": "CA" + "5" * 32,
        "From": "+12125550100",
        "To": "+12125550101",
    }
    signature = validator.compute_signature("https://media.example/twilio/owned-bot", data)
    with TestClient(app) as client:
        response = client.post(
            "/twilio/owned-bot", data=data, headers={"x-twilio-signature": signature}
        )
    assert response.status_code == 200
    assert "Stream" in response.text
    assert (
        json.loads((directory / "owned-bot-status.json").read_text())["calls"][data["CallSid"]][
            "state"
        ]
        == "reserved"
    )


def test_received_synthetic_audio_preserves_carrier_timestamps(bot):
    module, _, _, directory = bot
    events = [
        {
            "event": "media",
            "streamSid": "MZfixture",
            "media": {"timestamp": "1000", "payload": base64.b64encode(b"\xff" * 160).decode()},
        },
        {"event": "stop", "streamSid": "MZfixture"},
    ]

    class Socket:
        async def receive_json(self):
            return events.pop(0)

    record = {"stream_stopped": False}
    scenario = module.CallerScenario(Socket(), "MZfixture", directory, record)
    asyncio.run(scenario.receive())
    assert len(scenario.output) == (1000 + 20) * 16
    assert scenario.output[:16000] == b"\0" * 16000
    assert record["stream_stopped"] is True
    assert json.dumps(record)


async def test_owned_receiver_retains_actual_identifier_digits_and_pound(bot):
    module, _, _, directory = bot
    events = [
        {"event": "dtmf", "streamSid": "MZfixture", "dtmf": {"track": "inbound", "digit": digit}}
        for digit in "2804217#"
    ]
    events.append({"event": "stop", "streamSid": "MZfixture"})

    class Socket:
        async def receive_json(self):
            return events.pop(0)

    scenario = module.CallerScenario(Socket(), "MZfixture", directory, {})
    await scenario.receive()
    assert "".join(key["digit"] for key in scenario.record["received_keys"]) == "2804217#"
    assert all(
        key["source"] == "Twilio inbound DTMF event" for key in scenario.record["received_keys"]
    )


@pytest.mark.asyncio
async def test_interruption_waits_for_sustained_speech_after_a_short_acknowledgement(
    bot, monkeypatch
):
    module, _, _, directory = bot
    scenario = module.CallerScenario(None, "MZfixture", directory, {})
    scenario.now = lambda: 1.5
    scenario.last_voice = 1.5
    scenario.energy = [{"at_seconds": 1.5, "duration_ms": 600, "rms": 500}]
    waited = []

    async def more_speech(delay):
        waited.append(delay)
        scenario.energy.append({"at_seconds": 1.5, "duration_ms": 1420, "rms": 500})

    monkeypatch.setattr(module.asyncio, "sleep", more_speech)
    await scenario.wait_active_speech(after=0)
    assert waited == [0.02]


async def test_supervised_caller_ignores_holding_audio_and_wrong_call_marker(bot, monkeypatch):
    module, _, _, directory = bot
    marker = directory / "decision.json"
    scenario = module.CallerScenario(
        None, "MZfixture", directory, {}, decision_marker=marker, receiver_sid="CAowned"
    )
    scenario.last_voice = scenario.now()  # A holding sentence is already audible.
    marker.write_text(json.dumps({"receiver_sid": "CAother", "decision_forwarded": True}))
    waits = []

    async def deliver_after_wrong_marker(delay):
        waits.append(delay)
        assert "supervisor_forwarding_observed_at_seconds" not in scenario.record
        marker.write_text(json.dumps({"receiver_sid": "CAowned", "decision_forwarded": True}))

    monkeypatch.setattr(module.asyncio, "sleep", deliver_after_wrong_marker)
    await scenario.wait_supervisor()
    assert waits == [0.05]
    assert "supervisor_forwarding_observed_at_seconds" in scenario.record


async def test_followup_wait_requires_a_new_request_marker(bot, monkeypatch):
    module, _, _, directory = bot
    (directory / "followup.wav").write_bytes(b"fixture")
    marker = directory / "decision.json"
    marker.write_text(
        json.dumps({"receiver_sid": "CAowned", "decision_forwarded": True, "request_id": "first"})
    )
    scenario = module.CallerScenario(
        None, "MZfixture", directory, {}, decision_marker=marker, receiver_sid="CAowned"
    )
    await scenario.wait_supervisor()
    waits = []

    async def second_result(delay):
        waits.append(delay)
        marker.write_text(
            json.dumps(
                {"receiver_sid": "CAowned", "decision_forwarded": True, "request_id": "second"}
            )
        )

    monkeypatch.setattr(module.asyncio, "sleep", second_result)
    await scenario.wait_supervisor()
    assert waits == [0.05]
    assert [r["request_id"] for r in scenario.record["supervisor_forwardings"]] == [
        "first",
        "second",
    ]


async def test_account_lookup_ignores_stale_and_wrong_call_read_markers(bot, monkeypatch):
    import sys
    import time

    module, _, _, directory = bot
    monkeypatch.setitem(sys.modules, "owned_bot", module)
    root = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (root / "account_clerk.py").is_file():
        root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "account_clerk_fixture", root / "account_clerk.py"
    )
    account = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(account)
    assets = directory / "assets"
    assets.mkdir()
    (assets / "account-scenario.json").write_text(
        json.dumps({"mode": "live_lookup", "call_id": "fixture-call"})
    )
    marker = directory / "source-read.json"
    after = time.time() * 1000
    marker.write_text(
        json.dumps(
            {
                "call_id": "fixture-call",
                "source_read_delivered": True,
                "at_epoch_ms": after - 1000,
                "request_id": "stale",
            }
        )
    )
    scenario = account.LookupAccountClerkScenario(None, "MZfixture", assets, {})
    waited = []

    async def provide_fresh_read(delay):
        waited.append(delay)
        assert "source_read_request_id" not in scenario.record
        marker.write_text(
            json.dumps(
                {
                    "call_id": "another-call" if len(waited) == 1 else "fixture-call",
                    "source_read_delivered": True,
                    "at_epoch_ms": time.time() * 1000,
                    "request_id": "fresh",
                }
            )
        )

    monkeypatch.setattr(account.asyncio, "sleep", provide_fresh_read)
    await scenario.wait_source_read(after, timeout=1)
    assert waited == [0.05, 0.05]
    assert scenario.record["source_read_request_id"] == "fresh"


@pytest.mark.parametrize("required", [True, False, "false"])
async def test_lookup_recipient_can_omit_upfront_verification_without_omitting_lookup(
    bot, monkeypatch, required
):
    import sys
    from unittest.mock import AsyncMock

    module, _, _, directory = bot
    monkeypatch.setitem(sys.modules, "owned_bot", module)
    root = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (root / "account_clerk.py").is_file():
        root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("account_clerk_quick", root / "account_clerk.py")
    account = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(account)
    assets = directory / "assets"
    assets.mkdir()
    (assets / "account-scenario.json").write_text(
        json.dumps({"call_id": "fixture-call", "verification_required": required})
    )
    scenario = account.LookupAccountClerkScenario(None, "MZfixture", assets, {})
    for name in ["speak", "wait_keys", "wait_voice", "wait_quiet", "wait_source_read"]:
        setattr(scenario, name, AsyncMock(return_value=0))
    scenario.stopped.set()
    monkeypatch.setattr(account.asyncio, "sleep", AsyncMock())
    if not isinstance(required, bool):
        with pytest.raises(ValueError, match="boolean"):
            await scenario.scenario()
        scenario.speak.assert_not_awaited()
        return
    await scenario.scenario()
    clips = [c.args[0] for c in scenario.speak.call_args_list]
    assert clips[-3:] == ["agent", "profile", "balance"]
    assert ("verification" in clips and "hold" in clips) is required
    assert scenario.wait_keys.await_count == (2 if required else 1)
    scenario.wait_source_read.assert_awaited_once()
    assert scenario.wait_quiet.call_args.kwargs["timeout"] == 45
    assert scenario.record["profile_reply_observed"] is True


def test_caller_selects_a_blinded_variant_only_when_its_stream_scenario_is_created(
    bot, monkeypatch
):
    module, _, _, directory = bot
    for name in ("a", "b"):
        variant = directory / "variants" / name
        variant.mkdir(parents=True)
        for turn in ("interrupt", "followup"):
            (variant / (turn + ".wav")).write_bytes(b"fixture")
    monkeypatch.setattr(module.secrets, "choice", lambda choices: choices[-1])
    record = {}
    scenario = module.CallerScenario(None, "MZfixture", directory, record)
    assert record["scenario_variant"] == "b" and scenario.followup
    assert "question" not in record


async def test_mail_only_caller_skips_story_and_allows_longer_answer_playback(bot):
    module, _, _, directory = bot
    (directory / "mail-only.signal").touch()
    (directory / "followup.wav").write_bytes(b"fixture")
    scenario = module.CallerScenario(None, "MZfixture", directory, {})
    spoken, quiet_limits = [], []

    async def speak(name):
        spoken.append(name)
        if name == "hangup":
            scenario.stopped.set()
        return scenario.now()

    async def voice(*args, **kwargs):
        return None

    async def quiet(*args, **kwargs):
        quiet_limits.append(kwargs.get("timeout", 12))

    scenario.speak = speak
    scenario.wait_voice = voice
    scenario.wait_quiet = quiet
    scenario.wait_supervisor = voice
    await scenario.scenario()
    assert spoken == ["interrupt", "followup", "hangup"]
    assert quiet_limits == [12, 24, 24]
    assert scenario.record["scenario_mode"] == "mail_questions_only"
