from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("layer", choices=["offline", "preflight", "audio", "telephony", "dots"])
    args = parser.parse_args()
    artifacts = Path("/artifacts") if Path("/artifacts").exists() else Path("artifacts")
    artifacts.mkdir(parents=True, exist_ok=True)
    if args.layer == "offline":
        checks = [
            [
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml=" + str(artifacts / "offline.xml"),
            ],
            ["ruff", "check", "--no-cache", "dots_phone", "tests", "scripts"],
            ["ruff", "format", "--check", "--no-cache", "dots_phone", "tests", "scripts"],
            ["python", "scripts/secret_scan.py"],
        ]
        statuses = [subprocess.run(command, check=False).returncode for command in checks]
        report = {
            "layer": "A_offline",
            "timestamp": time.time(),
            "passed": not any(statuses),
            "checks": statuses,
            "provider_calls": 0,
            "provider_cost_usd": "0",
            "platform": os.uname().machine,
        }
        (artifacts / "offline.json").write_text(json.dumps(report, indent=2))
        raise SystemExit(int(any(statuses)))
    requirements = {
        "preflight": [
            "BWS machine token",
            "two approved application secrets",
            "owned caller/test endpoints",
            "approved HTTPS/WSS ingress",
        ],
        "audio": [
            "approved bounded audio campaign",
            "runtime OpenAI access",
            "official harness adapter live validation",
        ],
        "telephony": [
            "funded eligible account",
            "owned bot endpoint routing proof",
            "approved campaign",
            "runtime credentials and ingress",
        ],
        "dots": [
            "installed plugin in actual Dots conversation",
            "linked OAuth account",
            "received decision and terminal events",
            "scoped decision and cancellation evidence",
        ],
    }
    result = {
        "layer": args.layer,
        "status": "BLOCKED",
        "required_evidence": requirements[args.layer],
        "provider_calls": 0,
        "provider_cost_usd": "0",
        "timestamp": time.time(),
    }
    if args.layer == "preflight":
        check = subprocess.run(["python", "-m", "dots_phone.bws", "preflight"], capture_output=True)
        result.update(
            status="PASSED" if check.returncode == 0 else "BLOCKED",
            credentials_valid=check.returncode == 0,
        )
        result["note"] = (
            "Credential format/project preflight only; does not prove funded routing or installed Dots."
        )
    elif args.layer == "audio":
        from dots_phone.harness import load_cases

        reports = []
        for folder in (artifacts / "audio").glob("*"):
            if not folder.is_dir():
                continue
            if (folder / "result.json").exists():
                reports.append(json.loads((folder / "result.json").read_text()))
            elif (folder / "conversation.result.json").exists():
                # Older finalization failures retained the upstream trace before
                # raising. They must count as failed trials, never disappear
                # because the application-level result was not written.
                upstream = json.loads((folder / "conversation.result.json").read_text())
                reports.append(
                    {
                        "scenario_id": upstream["scenario_id"],
                        "audio_gate": {"full_acceptance_passed": False},
                        "failed_trace": str(folder / "conversation.result.json"),
                    }
                )
        counts = Counter(r["scenario_id"] for r in reports)
        covered = all(counts[c["id"]] >= 1 for c in load_cases())
        repeated = all(counts[id] >= 2 for id in ("hotel_happy", "shop_privacy", "shop_interrupt"))
        passed = (
            bool(reports)
            and covered
            and repeated
            and all(r["audio_gate"]["full_acceptance_passed"] for r in reports)
        )
        result.update(
            status="PASSED" if passed else "FAILED" if reports else "BLOCKED",
            scenario_counts=dict(counts),
            all_scenarios_covered=covered,
            representative_repeats_covered=repeated,
            runs=len(reports),
        )
        result.pop("provider_calls", None)
        result.pop("provider_cost_usd", None)
        result["note"] = (
            "Saved evidence only; failed/frozen runs and IVR/prerecorded coverage require separate review."
        )
        # Per-case success alone is insufficient: mandatory broader fixtures are
        # not implemented, so do not turn this partial report into a Layer B pass.
        result["required_evidence"].append(
            "prerecorded regression, IVR/hold and complete required coverage"
        )
        if passed:
            result["status"] = "BLOCKED"
    elif args.layer == "dots":
        evidence = artifacts / "dots-evidence.json"
        if evidence.exists():
            result["observed"] = json.loads(evidence.read_text())
            result["note"] = (
                "Operator-recorded actual browser evidence; no scripted MCP client impersonation."
            )
    (artifacts / (args.layer + ".json")).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "PASSED" else 2)


if __name__ == "__main__":
    main()
