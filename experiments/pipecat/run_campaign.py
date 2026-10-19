"""Five reserved synthetic sessions; preserve failures and stop on unknown usage."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from loguru import logger
from pipecat.evals.scenario import EvalScenarioFile

from run_live import SCOPE, evaluate, read_key, reserve

CAMPAIGN_SCOPE = {**SCOPE, "max_runs": 5, "record_audio": True}


def reserve_campaign(directory, approval):
    if {k: approval.get(k) for k in CAMPAIGN_SCOPE} != CAMPAIGN_SCOPE or not approval.get(
        "approval_reference"
    ):
        raise ValueError("Missing operator authorization or incompatible campaign scope")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "reservation.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({**approval, "state": "reserved"}, stream, indent=2)


async def run_campaign(key, scenario, directory, approval, evaluator=evaluate):
    reserve_campaign(directory, approval)
    reports = []
    for index in range(CAMPAIGN_SCOPE["max_runs"]):
        run_directory = directory / f"run-{index + 1:02d}"
        reserve(run_directory, {**approval, **SCOPE, "parent_campaign": directory.name})
        report = await evaluator(key, scenario, run_directory, record_audio=True)
        reports.append(report)
        print(
            json.dumps(
                {
                    "run": index + 1,
                    "passed": report["baseline_passed"],
                    "usage_confirmed": report["usage_confirmed"],
                }
            ),
            flush=True,
        )
        if not report["usage_confirmed"]:
            break
    summary = {
        "mode": "live_synthetic_repeatability",
        "runs": len(reports),
        "passed": sum(r["baseline_passed"] for r in reports),
        "usage_confirmed": all(r["usage_confirmed"] for r in reports),
        "final_live_seconds": sum(r.get("final_seconds") or 0 for r in reports),
        "backend_responses": sum(r["backend_responses"] for r in reports),
        "baseline_passed": len(reports) == CAMPAIGN_SCOPE["max_runs"]
        and all(r["baseline_passed"] for r in reports),
        "pstn_calls": 0,
        "audio_audit": "pending independent local transcription",
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2))
    reservation = json.loads((directory / "reservation.json").read_text())
    reservation.update(
        state="completed" if summary["usage_confirmed"] else "unconfirmed", runs=summary["runs"]
    )
    (directory / "reservation.json").write_text(json.dumps(reservation, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--approval-file", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    key = read_key(args.key_file)
    scenario = EvalScenarioFile.load(args.scenario).scenarios[0]
    if any(not turn.audio or not Path(turn.audio).is_file() for turn in scenario.turns):
        raise SystemExit("Missing local speech fixture; no session started")
    approval = json.loads(args.approval_file.read_text())
    summary = asyncio.run(run_campaign(key, scenario, args.output_directory, approval))
    print(json.dumps(summary))
    raise SystemExit(0 if summary["baseline_passed"] else 1)


if __name__ == "__main__":
    main()
