import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def test_gmail_audit_requires_the_spoken_status_to_match_the_actual_lookup():
    path = (
        Path(__file__).resolve().parent.parent / "scripts/audit_pipecat_supervised_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("gmail_carrier_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    heard = module.gmail_marker_answer_heard
    assert heard("No matching Gmail test messages were found.", 0)
    assert heard("A matching Gmail test message was found.", 1)
    assert not heard("No matching Gmail test messages were found.", 1)
    assert not heard("A matching Gmail test message was found.", 0)
    assert not heard("There are no matching museum records.", 0)
    assert not heard("I'll check the Gmail test marker.", 0)
    assert not heard("A matching Gmail test message was found.", 2)


def test_answer_phase_requires_the_exact_received_interval_and_unchanged_audio(tmp_path):
    path = (
        Path(__file__).resolve().parent.parent / "scripts/audit_pipecat_supervised_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("gmail_answer_phase", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, clip = tmp_path / "received.wav", tmp_path / "answer-phase.wav"
    source.write_bytes(b"whole received PCM fixture")
    clip.write_bytes(b"answer PCM fixture")
    words = [{"text": "No", "start": 3.2, "end": 3.3}]
    (tmp_path / "answer-audio-asr.json").write_text(json.dumps([{"words": words}]))
    proof = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "clip_sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
        "silence_removed": False,
        "start_seconds": 3,
        "end_seconds": 6,
    }
    manifest = tmp_path / "answer-phase.json"
    manifest.write_text(json.dumps(proof))
    assert module.answer_phase_words(tmp_path, source, 1, 4, 7, []) == words
    for changed in [
        {"start_seconds": 2},
        {"end_seconds": 8},
        {"silence_removed": True},
        {"source_sha256": "wrong"},
    ]:
        manifest.write_text(json.dumps({**proof, **changed}))
        with pytest.raises(ValueError, match="received audio interval"):
            module.answer_phase_words(tmp_path, source, 1, 4, 7, [])
