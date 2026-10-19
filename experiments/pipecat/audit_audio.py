"""Independently transcribe retained synthetic PCM locally, without API calls."""

import argparse
import json
import re
from pathlib import Path

import numpy as np

from audio_metrics import word_received_ms


def audit_run(directory, model):
    report = json.loads((directory / "result.json").read_text())
    segments, _ = model.transcribe(
        str(directory / "audio/output.wav"),
        language="en",
        word_timestamps=True,
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    words = []
    for segment in segments:
        for word in segment.words or []:
            words.append(
                {
                    "text": word.word,
                    "start": word.start,
                    "end": word.end,
                    "received_ms": word_received_ms(report["timeline"], word.start),
                }
            )
    lookup = next(
        (
            e["at_ms"]
            for e in report["timeline"]
            if e["kind"] == "tool_call" and e["name"] == "lookup_hours"
        ),
        None,
    )
    interrupt = next(
        (
            e["at_ms"]
            for e in report["timeline"]
            if e["kind"] == "turn_sent" and e["turn_index"] == 1
        ),
        None,
    )
    closing = None
    for index, word in enumerate(words):
        phrase = re.sub(
            r"[^a-z0-9]", "", "".join(w["text"] for w in words[index : index + 4]).lower()
        )
        if (
            re.match(r"^(?:five|5)pm", phrase)
            and word["received_ms"] is not None
            and lookup is not None
            and word["received_ms"] >= lookup
        ):
            closing = word
            break
    timing = report.get("audio_timing") or {}
    speech_start = timing.get("lookup_input_first_voiced_ms", interrupt)
    story = " ".join(
        w["text"]
        for w in words
        if w["received_ms"] is not None
        and speech_start is not None
        and w["received_ms"] <= speech_start
    )
    story_topic = " ".join(
        w["text"]
        for w in words
        if w["received_ms"] is not None and lookup is not None and w["received_ms"] < lookup
    )
    answer_latency = (
        round(closing["received_ms"] - timing["lookup_input_last_voiced_ms"], 2)
        if closing and timing.get("measurement_state") == "measured"
        else None
    )
    assertions = {
        "independent_story_transcription": len(re.findall(r"\b[a-z]+\b", story.lower())) >= 3
        and any(
            topic in story_topic.lower()
            for topic in ("museum", "gallery", "curator", "painting", "exhibit", "artifact")
        ),
        "independent_closing_time_transcription": closing is not None,
        "interruption_quiet_within_1500ms": timing.get("interruption_to_300ms_quiet_ms") is not None
        and timing["interruption_to_300ms_quiet_ms"] <= 1500,
        "closing_answer_within_5000ms": answer_latency is not None and answer_latency <= 5000,
        "close_ack_within_3000ms": timing.get("close_ack_ms") is not None
        and timing["close_ack_ms"] <= 3000,
        "protocol_baseline": report["baseline_passed"],
    }
    audit = {
        "transcriber": "faster-whisper 1.2.1 / tiny.en / CPU int8",
        "provider_transcript_used_as_input": False,
        "words": words,
        "text": "".join(w["text"] for w in words),
        "hard_assertions": assertions,
        "passed": all(assertions.values()),
        "answer_audio_received_after_last_input_voice_ms": answer_latency,
        "interruption_to_300ms_quiet_ms": timing.get("interruption_to_300ms_quiet_ms"),
        "close_ack_ms": timing.get("close_ack_ms"),
        "limits": "ASR word timing and RMS quiet detection are approximate; measurements exclude transport playback and carrier delay. Five-run p95 is descriptive, not an SLA.",
    }
    (directory / "audio-audit.json").write_text(json.dumps(audit, indent=2))
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--model-directory", type=Path, default=Path("../../artifacts/pipecat/models")
    )
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "tiny.en",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        download_root=str(args.model_directory),
        use_auth_token=False,
    )
    audits = [audit_run(p.parent, model) for p in sorted(args.directory.glob("run-*/result.json"))]
    metrics = {}
    for name in (
        "answer_audio_received_after_last_input_voice_ms",
        "interruption_to_300ms_quiet_ms",
        "close_ack_ms",
    ):
        values = [a[name] for a in audits if a[name] is not None]
        metrics[name] = {
            "samples": len(values),
            "median": round(float(np.median(values)), 2) if values else None,
            "p95": round(float(np.percentile(values, 95)), 2) if values else None,
        }
    summary = {
        "runs": len(audits),
        "passed": sum(a["passed"] for a in audits),
        "all_passed": bool(audits) and all(a["passed"] for a in audits),
        "metrics": metrics,
        "limits": "Synthetic model-boundary measurements; excludes playback and telephone-carrier delay. This small sample does not establish production reliability.",
    }
    (args.directory / "audio-audit-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["all_passed"] else 1)


if __name__ == "__main__":
    main()
