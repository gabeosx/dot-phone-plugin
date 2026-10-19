import pytest

from prepared_context_probe import measurements, require_ready, validate_result, write_json


def envelope():
    request = {"nonce": "one-call-only", "created_ms": 100, "expires_ms": 1000}
    result = {
        "schema": 1,
        "nonce": request["nonce"],
        "status": "no_matches",
        "match_count": 0,
        "request_observed_ms": 110,
        "plugin_started_ms": 120,
        "plugin_finished_ms": 130,
        "result_written_ms": 140,
    }
    return request, result


def test_only_content_free_correlated_result_reaches_voice():
    request, result = envelope()
    assert validate_result(result, request, 150) == {
        "status": "no_matches",
        "match_count": 0,
        "email_contents_disclosed": False,
    }
    result["email_body"] = "private content"
    with pytest.raises(ValueError):
        validate_result(result, request, 150)


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "different-call"),
        ("status", "made_up"),
        ("match_count", True),
        ("match_count", 2),
        ("status", "unexpected_match"),
        ("plugin_started_ms", 90),
        ("plugin_finished_ms", 151),
    ],
)
def test_rejects_stale_unscoped_or_inconsistent_worker_result(field, value):
    request, result = envelope()
    result[field] = value
    with pytest.raises(ValueError):
        validate_result(result, request, 150)


def test_expired_lookup_cannot_disclose_a_late_result():
    request, result = envelope()
    with pytest.raises(ValueError):
        validate_result(result, request, 1001)


def test_latency_excludes_holding_audio_before_the_substantive_answer():
    timeline = [
        {"kind": "input_audio", "at_ms": 80, "speech": True},
        {
            "kind": "caller_boundary",
            "at_ms": 100,
            "event_type": "input_audio_buffer.speech_stopped",
        },
        {"kind": "output_audio", "at_ms": 120, "speech": True, "response_id": "holding"},
        {"kind": "caption", "at_ms": 140, "text": "Let me check", "response_id": "holding"},
        {"kind": "output_audio", "at_ms": 500, "speech": True, "response_id": "answer"},
        {"kind": "caption", "at_ms": 600, "text": "It closes at five PM", "response_id": "answer"},
    ]
    assert measurements({"timeline": timeline})[0]["answer_delay_ms"] == 420
    assert measurements({"timeline": timeline})[0]["first_output_audio_ms"] == 120


def test_voice_session_requires_worker_to_be_actively_serving(tmp_path):
    status = {"identity": "subscription-worker", "ready": True}
    write_json(tmp_path / "worker-ready.json", status)
    with pytest.raises(ValueError):
        require_ready(tmp_path)
    write_json(tmp_path / "worker-ready.json", {**status, "serving": True})
    require_ready(tmp_path)
