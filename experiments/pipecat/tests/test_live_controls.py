import json

import pytest

from bot import Evidence, error_details
from run_live import SCOPE, hard_assertions, read_key, reserve


def test_live_reservation_requires_separate_approval_and_never_retries(tmp_path):
    with pytest.raises(ValueError):
        reserve(tmp_path, dict(SCOPE))
    approval = {**SCOPE, "approval_reference": "test human approval"}
    reserve(tmp_path, approval)
    with pytest.raises(FileExistsError):
        reserve(tmp_path, approval)
    assert json.loads((tmp_path / "reservation.json").read_text())["state"] == "reserved"


def test_live_scope_refuses_larger_campaign(tmp_path):
    with pytest.raises(ValueError):
        reserve(tmp_path, {**SCOPE, "max_runs": 2, "approval_reference": "test"})


def test_key_loader_rejects_symlinks_and_public_files(tmp_path):
    key = tmp_path / "key"
    key.write_text("fixture")
    key.chmod(0o644)
    with pytest.raises(ValueError):
        read_key(key)
    key.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(ValueError):
        read_key(link)
    assert read_key(key) == "fixture"


def test_interruption_gate_rejects_noise_and_missing_story():
    evidence = Evidence()
    evidence.timeline = [
        {"kind": "output_audio", "at_ms": 10, "speech": False},
        {"kind": "turn_sent", "at_ms": 100, "turn_index": 1},
    ]
    assert not hard_assertions(evidence)["voiced_output_at_interruption"]
    assert not hard_assertions(evidence)["story_caption_before_interruption"]
    evidence.timeline.insert(
        0, {"kind": "caption", "at_ms": 1, "role": "assistant", "text": "A museum story"}
    )
    evidence.timeline.insert(1, {"kind": "output_audio", "at_ms": 50, "speech": True})
    assert hard_assertions(evidence)["voiced_output_at_interruption"]
    assert hard_assertions(evidence)["story_caption_before_interruption"]
    evidence.timeline[-1]["at_ms"] = 1000
    assert not hard_assertions(evidence)["voiced_output_at_interruption"]


def test_provider_error_capture_redacts_credentials():
    from types import SimpleNamespace

    key = "sk-" + "a" * 30
    error = SimpleNamespace(
        code=None, type="invalid_request_error", param="response", message=f"bad credential {key}"
    )
    details = error_details(error, key)
    assert key not in details["message"]
    assert details["message"] == "bad credential [REDACTED]"
    assert details["type"] == "invalid_request_error"
