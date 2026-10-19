import pytest

pytest.importorskip("pipecat")

from experiments.pipecat.prepare_reasoner_benchmark import grade, summarize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Correct the final digit; the card ends in zero.", True),
        ("No. The correct number is zero zero four two zero four two zero.", True),
        ("Yes, the readback is correct.", False),
        ("Correct the final digit to one.", False),
    ],
)
def test_benchmark_grader_accepts_valid_readback_correction_without_prescribing_full_repetition(
    text, expected
):
    row = {
        "case": "incorrect_readback",
        "advice": {
            "status": "ready",
            "next_step": text,
            "answer": "",
            "supporting_quote": "",
            "supporting_fact_names": ["library_card"],
            "report_supported": False,
        },
    }
    assert grade(row) is expected


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {
            "last": {
                "inputTokens": 15000,
                "cachedInputTokens": 14000,
                "outputTokens": 30,
                "reasoningOutputTokens": 0,
            }
        },
    ],
)
def test_benchmark_preserves_raw_compact_output_and_handles_missing_stream_timing(usage):
    raw = {"decision": {"kind": "key", "digit": "2", "quote": "For loan information press 2."}}
    report = summarize(
        {
            "configurations": [{"name": "sol_medium"}],
            "rows": [
                {
                    "configuration": "sol_medium",
                    "case": "menu",
                    "repeat": 1,
                    "first_model_turn": False,
                    "first_interaction_turn": True,
                    "elapsed_ms": 1900,
                    "first_token_ms": None,
                    "token_usage": usage,
                    "advice": raw,
                }
            ],
        }
    )
    assert report["rows"][0]["advice"] == raw
    assert report["rows"][0]["normalized_advice"]["action"]["digit"] == "2"
    assert report["summary"]["sol_medium"]["passed"] == 1
    if usage:
        assert report["summary"]["sol_medium"]["diagnostics"]["median_first_token_ms"] is None
