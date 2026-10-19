"""One operator-approved, reserved synthetic model run; no automatic retry or PSTN."""

import argparse
import asyncio
import json
import os
import re
import stat
from pathlib import Path

from loguru import logger
from pipecat.evals.scenario import EvalScenarioFile

from audio_metrics import AudioCapture, timing_metrics
from bot import Evidence
from run_baseline import conversation

SCOPE = {
    "models": ["gpt-live-1", "gpt-5.6-terra"],
    "max_runs": 1,
    "conversation_timeout_seconds": 60,
    "teardown_timeout_seconds": 20,
    "max_backend_responses": 4,
    "pstn_calls": 0,
    "synthetic": True,
}


def read_key(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Key must be an owner-only regular file")
    return path.read_text().strip()


def reserve(directory, approval):
    # This new experiment must have a separate explicit human approval.
    # It cannot revive or spend from the original frozen audio campaign.
    if {k: approval.get(k) for k in SCOPE} != SCOPE or not approval.get("approval_reference"):
        raise ValueError("Missing human approval or incompatible experiment scope")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "reservation.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({**approval, "state": "reserved", "retry_permitted": False}, stream, indent=2)


def hard_assertions(evidence):
    interrupt = next(
        (
            e["at_ms"]
            for e in evidence.timeline
            if e["kind"] == "turn_sent" and e["turn_index"] == 1
        ),
        None,
    )
    before = [e for e in evidence.timeline if interrupt is not None and e["at_ms"] <= interrupt]
    story = "".join(
        e["text"] for e in before if e["kind"] == "caption" and e["role"] == "assistant"
    )
    overlap = any(
        e["kind"] == "output_audio" and e["speech"] and interrupt - e["at_ms"] <= 300
        for e in before
    )
    close = next((e["at_ms"] for e in evidence.timeline if e["kind"] == "close_requested"), None)
    no_late_events = close is not None and not any(
        e["kind"] == "client_event" and e["at_ms"] > close for e in evidence.timeline
    )
    return {
        "story_caption_before_interruption": len(re.findall(r"\b[a-z]+\b", story.lower())) >= 3,
        "voiced_output_at_interruption": overlap,
        "tool_lookup": {"name": "lookup_hours", "place": "museum"} in evidence.calls,
        "hangup_tool": {"name": "end_call"} in evidence.calls,
        "close_requested": evidence.close_sent,
        "delegations_drained": any(e["kind"] == "delegations_drained" for e in evidence.timeline),
        "no_client_events_after_close": no_late_events,
        "provider_closed": evidence.closed,
        "final_usage_confirmed": evidence.final_seconds is not None,
        "no_provider_errors": not evidence.errors,
    }


async def evaluate(key, scenario, directory, *, record_audio=False):
    evidence = Evidence()
    if record_audio:
        evidence.capture = AudioCapture(directory / "audio")
    result = None
    failure = None
    try:
        result = await conversation(
            scenario,
            key=key,
            base_url="wss://api.openai.com/v1/live/sessions",
            evidence=evidence,
            timeout=60,
        )
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if record_audio:
            evidence.capture.close()
    report = evidence.report()
    assertions = hard_assertions(evidence)
    confirmed = evidence.closed and evidence.final_seconds is not None
    report.update(
        mode="live_synthetic_audio",
        pipecat_version="1.12.0",
        state="completed" if confirmed else "unconfirmed",
        eval_passed=result.passed if result else False,
        eval_failures=[str(f) for f in result.failures] if result else [],
        failure_type=failure,
        pstn_calls=0,
        usage_confirmed=confirmed,
        automatic_retry=False,
        hard_assertions=assertions,
        audio_recorded=record_audio,
        audio_timing=timing_metrics(evidence.timeline) if record_audio else None,
        limits="Caption/tool smoke test; acoustic latency and quality require further evaluation.",
    )
    report["baseline_passed"] = bool(result and result.passed and all(assertions.values()))
    (directory / "result.json").write_text(json.dumps(report, indent=2))
    reservation = json.loads((directory / "reservation.json").read_text())
    reservation.update(state=report["state"], usage_confirmed=confirmed)
    (directory / "reservation.json").write_text(json.dumps(reservation, indent=2))
    print(
        json.dumps(
            {k: report[k] for k in ["state", "baseline_passed", "usage_confirmed", "failure_type"]}
        )
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--approval-file", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--record-audio",
        action="store_true",
        help="Retain synthetic PCM and boundary timing for independent local analysis",
    )
    args = parser.parse_args()
    logger.remove()  # Save selected evidence only; never print provider exceptions.
    try:
        key = read_key(args.key_file)
        scenario = EvalScenarioFile.load(args.scenario).scenarios[0]
        # Validate all local fixtures before consuming the one-run reservation.
        for turn in scenario.turns:
            if not turn.audio or not Path(turn.audio).is_file():
                raise ValueError("Missing speech fixture")
        reserve(args.output_directory, json.loads(args.approval_file.read_text()))
        report = asyncio.run(
            evaluate(key, scenario, args.output_directory, record_audio=args.record_audio)
        )
        raise SystemExit(0 if report["baseline_passed"] else 1)
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "blocked", "error_type": type(exc).__name__}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
