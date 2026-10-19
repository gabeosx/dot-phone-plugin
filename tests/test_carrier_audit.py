import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("5 p .m. is the museum's closing time.", 1),
        ("Five PM is closing time. Five p.m. is closing time.", 2),
        ("Looking up the closing time. 5 PM.", 1),
        ("Five people are visiting the museum.", 0),
    ],
)
def test_closing_time_counter_handles_split_asr_punctuation_and_repeated_answers(text, count):
    path = Path(__file__).resolve().parent.parent / "scripts/audit_pipecat_carrier_audio.py"
    spec = importlib.util.spec_from_file_location("carrier_audit_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.closing_time_mentions(text) == count


def test_interrupted_word_onset_counts_without_letting_later_answer_enter_story_phase():
    path = Path(__file__).resolve().parent.parent / "scripts/audit_pipecat_carrier_audio.py"
    spec = importlib.util.spec_from_file_location("carrier_phase_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    words = [
        {"text": "museum", "start": 1.1, "end": 1.5},
        {"text": "built", "start": 1.9, "end": 2.8},
        {"text": "five PM", "start": 5.0, "end": 5.5},
    ]
    assert module.words_started_in_phase(words, 1.0, 2.0) == "museum built"
