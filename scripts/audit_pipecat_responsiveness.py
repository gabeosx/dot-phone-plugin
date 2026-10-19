"""Measure actual caller-side yield and answer timing from retained synthetic audio."""

import argparse
import json
import re
from pathlib import Path


def measure(directory):
    carrier = json.loads((directory / "carrier-result.json").read_text())
    sid = carrier["call_sid"]
    caller = carrier["owned-bot-status.json"]["calls"][sid]
    energy = json.loads((directory / (sid + "-energy.json")).read_text())
    interrupt = next(t for t in caller["turns"] if t["name"] == "interrupt")
    first = interrupt["first_voice_sent_at_seconds"]
    last = interrupt["last_voice_sent_at_seconds"]
    quiet_start, quiet_duration, quiet_confirmed = None, 0, None
    for sample in energy:
        at = sample["at_seconds"]
        if at < first:
            continue
        if at > last:
            break
        if sample["rms"] > 200:
            quiet_start, quiet_duration = None, 0
        else:
            if quiet_start is None:
                quiet_start = at
            quiet_duration += sample["duration_ms"]
            if quiet_duration >= 300:
                quiet_confirmed = quiet_start
                break
    resumed = [
        s
        for s in energy
        if quiet_confirmed is not None
        and quiet_confirmed + 0.3 <= s["at_seconds"] <= last
        and s["rms"] > 200
    ]
    acknowledgement = next(
        (s["at_seconds"] for s in energy if s["at_seconds"] > last and s["rms"] > 200), None
    )
    audit = json.loads((directory / "audio-audit.json").read_text())
    answer = None
    for word in audit.get("words", []):
        if re.fullmatch(r"(?:five|5)(?:p\.?m\.?)?", word["text"].strip(" .,!?"), re.I):
            # A word stretched over silence is not a defensible latency timestamp.
            if not 0 < word["end"] - word["start"] <= 1:
                continue
            answer = next(
                (
                    s["at_seconds"]
                    for s in energy
                    if s["timestamp_ms"] + s["duration_ms"] > word["start"] * 1000
                    and s["timestamp_ms"] < word["end"] * 1000
                    and s["rms"] > 200
                ),
                None,
            )
            if answer is not None and answer > last:
                break
            answer = None
    assistant = carrier["assistant"][0]
    timing = json.loads((directory / (assistant["call_sid"] + "-timing.json")).read_text())
    shift = timing["clock_origin_monotonic"] - caller["clock_origin_monotonic"]
    timeline = [
        {**e, "caller_clock_seconds": shift + e["at_ms"] / 1000} for e in timing["timeline"]
    ]
    local_start = next(
        (
            e["caller_clock_seconds"]
            for e in timeline
            if e["kind"] == "caller_speech_started" and first <= e["caller_clock_seconds"] <= last
        ),
        None,
    )
    projected = next(
        (
            e["caller_clock_seconds"]
            for e in timeline
            if e["kind"] == "playback_cleared"
            and e.get("reason") == "projected_turn"
            and first <= e["caller_clock_seconds"] <= last + 3
        ),
        None,
    )
    lookup_started = next(
        (
            e["caller_clock_seconds"]
            for e in timeline
            if e["kind"] == "tool_started" and e.get("name") == "lookup_hours"
        ),
        None,
    )
    result = {
        "audit_version": 2,
        "protocol_passed": carrier["protocol_passed"],
        "audio_passed": audit["passed"],
        "speech_at_interruption": caller["voice_at_interruption"],
        "yield_first_300ms_quiet_ms": round((quiet_confirmed - first) * 1000)
        if quiet_confirmed
        else None,
        "no_speech_resumed_during_caller_turn": quiet_confirmed is not None and not resumed,
        "resumed_speech_ms": round(sum(s["duration_ms"] for s in resumed)),
        "local_detection_after_caller_send_ms": round((local_start - first) * 1000)
        if local_start
        else None,
        "projected_turn_after_caller_send_ms": round((projected - first) * 1000)
        if projected
        else None,
        "acknowledgement_after_last_caller_voice_ms": round((acknowledgement - last) * 1000)
        if acknowledgement
        else None,
        "closing_time_after_last_caller_voice_ms": round((answer - last) * 1000)
        if answer
        else None,
        "lookup_tool_after_last_caller_voice_ms": round((lookup_started - last) * 1000)
        if lookup_started
        else None,
        "input_buffer": assistant["input_buffer"],
        "playback": assistant["playback"],
        "boundary": "Synthetic caller audio sent to Twilio to returning carrier PCM received; actual energy samples, not model captions. ASR word timing is approximate. This excludes a handset speaker/microphone.",
    }
    result["responsiveness_passed"] = (
        result["protocol_passed"]
        and result["audio_passed"]
        and result["speech_at_interruption"]
        and result["yield_first_300ms_quiet_ms"] is not None
        and result["yield_first_300ms_quiet_ms"] <= 1000
        and result["no_speech_resumed_during_caller_turn"]
        and result["closing_time_after_last_caller_voice_ms"] is not None
        and 0 <= result["closing_time_after_last_caller_voice_ms"] <= 5000
        and answer < next(t["at_seconds"] for t in caller["turns"] if t["name"] == "hangup")
    )
    (directory / "responsiveness.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = measure(args.directory)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["responsiveness_passed"] else 1)


if __name__ == "__main__":
    main()
