"""Offline audit of audible answers; does not use provider captions as input."""

import argparse
import json
import re
from pathlib import Path


def checks(text, question):
    normalized = re.sub(r"[^a-z0-9 ]", "", text.lower())
    if question == "room.wav":
        return {
            "room_and_bed": "king" in normalized and "bed" in normalized,
            "view": "city view" in normalized,
            "occupancy": bool(re.search(r"\b(two|2) adults\b", normalized)),
        }
    return {
        "later_property": "omni" in normalized and "later" in normalized,
        "omni_date": bool(re.search(r"june (2(?:nd)?|two|second)\b", normalized)),
        "other_date": bool(re.search(r"may (29(?:th)?|twenty ninth|twenty nine)\b", normalized)),
        "difference": bool(re.search(r"\b(four|4) days\b", normalized)),
    }


def main(args):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "small.en",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        download_root="artifacts/pipecat/models",
        local_files_only=True,
    )
    rows = []
    for directory in args.directories:
        for path in sorted(directory.glob("*/result.json")):
            result = json.loads(path.read_text())
            segments, _ = model.transcribe(
                str(path.parent / "answer.wav"),
                language="en",
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
                beam_size=5,
            )
            heard = [
                {"text": segment.text, "start": segment.start, "end": segment.end}
                for segment in segments
            ]
            text = " ".join(segment["text"] for segment in heard)
            content = checks(text, result["clip"])
            rows.append(
                {
                    "directory": str(path.parent),
                    "mode": result["mode"],
                    "silence_ms": result["silence_ms"],
                    "heard": heard,
                    "checks": content,
                    "content_passed": all(content.values()),
                    "usage_confirmed": result["usage_confirmed"],
                    "errors": result["errors"],
                    "provider_caption_used": False,
                    "first_voiced_audio_delay_ms": result["first_voiced_audio_delay_ms"],
                }
            )
    args.output.write_text(
        json.dumps({"transcriber": "local small.en CPU int8", "rows": rows}, indent=2)
    )
    args.output.chmod(0o600)
    print(
        json.dumps({"audited": len(rows), "content_passed": sum(r["content_passed"] for r in rows)})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
