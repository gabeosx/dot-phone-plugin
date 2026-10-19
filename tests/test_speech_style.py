"""Neutral acknowledgements must not invent an explanation or confirmation."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dots_phone.speech_style import choose_holding_phrase, speech_library
from dots_phone.voice_prompt import conversation_guidance


def test_holding_bank_is_neutral_and_does_not_answer_or_confirm_any_fact():
    bank = speech_library()["holding"]
    phrases = [*bank["short"], *bank["conversational"]]
    assert len(phrases) >= 40 and len(set(phrases)) == len(phrases)
    for phrase in phrases:
        assert not re.search(
            r"\b(?:check|checking|search|searching|lookup|looking|find|finding|fetch|"
            r"forgot|remember|system|record|tool|codex|name|address|yes|correct|right)\b|\d",
            phrase,
            re.I,
        ), phrase


@pytest.mark.parametrize("ivr_only", [False, True])
def test_picker_varies_across_calls_without_repeating_recent_holding_lines(ivr_only):
    bank = speech_library()["holding"]
    allowed = set(bank["short"] if ivr_only else [*bank["short"], *bank["conversational"]])
    samples = [choose_holding_phrase(ivr_only=ivr_only) for _ in range(100)]
    assert set(samples) <= allowed
    assert len(set(samples)) >= 13
    assert all(p not in samples[max(0, i - 12) : i] for i, p in enumerate(samples))


def test_full_bank_is_available_to_both_voice_and_reasoning_guidance():
    from dots_phone.call_reasoner import REASONING_PROMPT

    guide = conversation_guidance()
    for group in [
        *speech_library()["holding"].values(),
        *speech_library()["conversation_examples"].values(),
    ]:
        for phrase in group:
            assert phrase in guide and phrase in REASONING_PROMPT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node worker not installed")
def test_full_production_guidance_passes_the_codex_worker_request_validator():
    from dots_phone.call_reasoner import REASONING_PROMPT

    request = {
        "schema": 1,
        "call_id": "a" * 32,
        "request_id": "b" * 32,
        "nonce": "c" * 64,
        "revision": 1,
        "brief_hash": "d" * 64,
        "created_ms": 1000,
        "expires_ms": 2000,
        "model": "gpt-6-sol",
        "instructions": REASONING_PROMPT,
        "context": {"brief": {"actions": []}},
        "output_schema": {"type": "object"},
    }
    script = """
import fs from 'node:fs';
import {validateRequest} from './experiments/pipecat/codex_call_reasoner.mjs';
const r=JSON.parse(fs.readFileSync(0,'utf8'));
validateRequest(r,r.call_id+'-'+r.request_id+'-reason.json',1500);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
