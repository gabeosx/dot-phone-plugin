import json
import wave

import pytest

from scripts.private_call_regression import FILES, regression


@pytest.fixture
def private_evidence(tmp_path):
    root = tmp_path / "private-call"
    root.mkdir(mode=0o700)
    (root / "evidence").mkdir(mode=0o700)
    bound = {"call_id": "1" * 32, "revision": 1, "brief_hash": "2" * 64}
    transcript = [{"role": "user", "text": "Your current points balance is 37,412."}]
    result = {
        **bound,
        "state": "completed",
        "outcome": "completed",
        "usage": {"voice_closed_confirmed": True, "realtime_usage_confirmed": True},
        "recording": {
            "state": "complete",
            "assistant_bytes": 80,
            "recipient_bytes": 80,
            "duration_ms": 10,
        },
        "transcript": transcript,
        "confirmed_facts": [{"quote": transcript[0]["text"]}],
    }
    records = {
        "prepared.json": bound,
        "worker-final-result.json": result,
        "evidence/transcript.json": transcript,
        "carrier-closure.json": {"status": "completed", "active_carrier_calls": 0},
        "restoration.json": {
            "phone": {"DOTS_PHONE_DRY_RUN": "true"},
            "route_unchanged": True,
            "all_eight_persistent_ids_preserved": True,
        },
    }
    for name in FILES:
        path = root / name
        if name.endswith(".wav"):
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\x01\x00" * 160)
        else:
            path.write_text(json.dumps(records.get(name, {})))
        path.chmod(0o600)
    return root


def test_private_regression_seals_checks_and_never_replaces_accepted_evidence(private_evidence):
    assert regression(private_evidence, "seal")["network_calls"] == 0
    assert regression(private_evidence, "check")["passed"]
    with pytest.raises(FileExistsError):
        regression(private_evidence, "seal")
    (private_evidence / "outcome-audit.json").write_text('{"changed": true}')
    with pytest.raises(ValueError, match="changed since acceptance"):
        regression(private_evidence, "check")


@pytest.mark.parametrize("change", ["quote", "scope", "closure", "symlink", "permissions"])
def test_bad_or_exposed_evidence_cannot_be_sealed(private_evidence, change):
    path = private_evidence / "worker-final-result.json"
    result = json.loads(path.read_text())
    if change == "quote":
        result["confirmed_facts"][0]["quote"] = "Different result not heard."
    elif change == "scope":
        result["revision"] = 2
    elif change == "closure":
        result["usage"]["voice_closed_confirmed"] = False
    elif change == "symlink":
        target = private_evidence / "outside.json"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
    else:
        path.chmod(0o644)
    if change not in {"symlink", "permissions"}:
        path.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        regression(private_evidence, "seal")
    assert not (private_evidence / "private-acceptance-lock.json").exists()
