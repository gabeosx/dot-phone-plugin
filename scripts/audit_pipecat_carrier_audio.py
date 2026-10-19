"""Local transcription of synthetic audio received through an actual carrier call."""

import argparse
import json
import re
from pathlib import Path


def closing_time_mentions(text):
    # Word timestamps can split punctuation into a separate ASR token: p .m.
    return len(re.findall(r"(?:five|5)\s*(?:p\s*\.?\s*m\.?|o.clock)", text, re.I))


def words_started_in_phase(words, start, end):
    # An interrupted word need not finish; ASR can stretch its end into silence.
    return " ".join(w["text"] for w in words if start <= w["start"] < end)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--asr-model", default="tiny.en")
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    audio = next(args.directory.glob("CA*-received.wav"))
    model = WhisperModel(
        args.asr_model,
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        download_root="artifacts/pipecat/models",
        local_files_only=args.asr_model == "tiny.en",
    )
    segments, _ = model.transcribe(
        str(audio),
        language="en",
        word_timestamps=True,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    rows, words = [], []
    for segment in segments:
        rows.append({"text": segment.text, "start": segment.start, "end": segment.end})
        words.extend(
            {"text": w.word, "start": w.start, "end": w.end} for w in (segment.words or [])
        )
    text = " ".join(s["text"] for s in rows)
    carrier = json.loads((args.directory / "carrier-result.json").read_text())
    caller = carrier["owned-bot-status.json"]["calls"][carrier["call_sid"]]
    story_turn = next(t for t in caller["turns"] if t["name"] == "story")
    interruption = next(t for t in caller["turns"] if t["name"] == "interrupt")
    alignment = caller.get("first_media_timestamp_ms", 0) / 1000 - caller.get(
        "first_media_at_seconds", 0
    )
    story_text = words_started_in_phase(
        words,
        story_turn.get("last_voice_sent_at_seconds", story_turn["at_seconds"]) + alignment,
        interruption["at_seconds"] + alignment + 1,
    )
    hangup = next(t for t in caller["turns"] if t["name"] == "hangup")
    answer_text = " ".join(
        w["text"]
        for w in words
        if interruption.get("last_voice_sent_at_seconds", interruption["at_seconds"]) + alignment
        <= w["start"]
        < hangup["at_seconds"] + alignment
    )
    answer_mentions = closing_time_mentions(answer_text)
    assertions = {
        "ai_introduction": bool(re.search(r"\bAI\b", text, re.I)),
        "story_audio": len(re.findall(r"\b[a-z]+\b", story_text, re.I)) >= 5,
        "spoken_tool_answer": bool(re.search(r"(?:five|5)\s*(?:p\.?\s*m\.?|o.clock)", text, re.I)),
        "tool_answer_not_duplicated": answer_mentions == 1,
    }
    audit = {
        "audit_version": 5,
        "transcriber": f"faster-whisper 1.2.1 / {args.asr_model} CPU int8",
        "provider_transcript_used_as_input": False,
        "audio_path": str(audio),
        "text": text,
        "segments": rows,
        "words": words,
        "story_phase_text": story_text,
        "answer_phase_text": answer_text,
        "closing_time_mentions_before_hangup": answer_mentions,
        "assertions": assertions,
        "passed": all(assertions.values()),
        "spoken_ending_phrase": bool(
            re.search(r"hang(?:ing)?\s+up|ending|end the call|goodbye|you got it", text, re.I)
        ),
        "hangup_verification": "Use the carrier result's stop, completed legs, end_call tool and confirmed model closure; a specific goodbye phrase is not required",
        "scope": "Synthetic caller receiving actual Twilio audio; no human audio retained",
    }
    (args.directory / "audio-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))
    raise SystemExit(0 if audit["passed"] else 1)


if __name__ == "__main__":
    main()
