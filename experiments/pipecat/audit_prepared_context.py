"""Independently audit the prepared-context experiment's synthetic audio locally."""

import argparse
import json
import re
from pathlib import Path

from audio_metrics import word_received_ms
from prepared_context_probe import measurements, write_json


def audit(directory):
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "small.en",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        download_root="artifacts/pipecat/models",
        local_files_only=True,
    )
    results = []
    for path in sorted(directory.glob("run-*/result.json")):
        report = json.loads(path.read_text())
        segments, _ = model.transcribe(
            str(path.parent / "audio/output.wav"),
            language="en",
            word_timestamps=True,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        rows, words = [], []
        for segment in segments:
            rows.append(
                {"text": segment.text, "start": float(segment.start), "end": float(segment.end)}
            )
            words.extend(
                {
                    "text": w.word,
                    "start": float(w.start),
                    "end": float(w.end),
                    "received_ms": word_received_ms(report["timeline"], w.start),
                }
                for w in segment.words or []
            )
        text = " ".join(s["text"] for s in rows)
        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        returned = [e for e in report["timeline"] if e["kind"] == "worker_result_observed"]
        assertions = {
            "spoken_closing_time": "5pm" in compact or "fivepm" in compact,
            "spoken_prepared_reference": "muse4281" in compact,
            "spoken_actual_search_result": "match" in compact
            and ("no" in compact or "zero" in compact)
            and len(returned) == 1
            and returned[0]["timings"]["status"] == "no_matches",
            "spoken_goodbye": "goodbye" in compact,
            "protocol_and_teardown": report["passed"],
        }
        result = {
            "run": path.parent.name,
            "transcriber": "faster-whisper small.en / CPU int8 / local cached model",
            "provider_transcript_used_as_input": False,
            "text": text,
            "segments": rows,
            "words": words,
            "assertions": assertions,
            "passed": all(assertions.values()),
            "measurements": measurements(report),
            "limits": "No carrier or microphone delay; three synthetic conversations are a feasibility test, not a latency SLA. Answer timing is the start of the substantive response, not the exact answer word.",
        }
        write_json(path.parent / "audio-audit.json", result)
        results.append(result)
    write_json(
        directory / "audio-audit-summary.json",
        {
            "passed": bool(results) and all(r["passed"] for r in results),
            "runs": len(results),
            "results": [
                {k: r[k] for k in ("run", "passed", "assertions", "measurements")} for r in results
            ],
        },
    )
    print(
        json.dumps(
            {
                "runs": len(results),
                "passed": bool(results) and all(r["passed"] for r in results),
                "texts": [r["text"] for r in results],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    audit(parser.parse_args().directory)
