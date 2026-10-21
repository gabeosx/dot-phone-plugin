"""Run pip-audit while retaining reviewed, expiring exceptions in its JSON report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path


def evaluate(report: dict, exceptions: list[dict], today: date) -> dict:
    """Fail new findings, package/version changes, expired exceptions and incomplete audits."""
    accepted, blocked = [], []
    if not isinstance(report.get("dependencies"), list) or not report["dependencies"]:
        raise ValueError("missing audited dependencies")
    for dependency in report["dependencies"]:
        if (
            not isinstance(dependency, dict)
            or any(
                not isinstance(dependency.get(field), str) or not dependency[field].strip()
                for field in ("name", "version")
            )
            or not isinstance(dependency.get("vulns"), list)
        ):
            raise ValueError("incomplete dependency evidence")
        if dependency.get("skip_reason"):
            blocked.append({"name": dependency["name"], "reason": "dependency_not_audited"})
            continue
        for vulnerability in dependency["vulns"]:
            if (
                not isinstance(vulnerability, dict)
                or not isinstance(vulnerability.get("id"), str)
                or not vulnerability["id"].strip()
            ):
                raise ValueError("incomplete advisory evidence")
            finding = {
                "name": dependency["name"],
                "version": dependency["version"],
                "id": vulnerability["id"],
            }
            exception = next(
                (
                    item
                    for item in exceptions
                    if item["name"] == finding["name"]
                    and item["version"] == finding["version"]
                    and finding["id"] in item["ids"]
                    and today < date.fromisoformat(item["expires"])
                    and item["reason"].strip()
                ),
                None,
            )
            if exception:
                accepted.append({**finding, "expires": exception["expires"]})
            else:
                blocked.append(finding)
    return {"passed": not blocked, "accepted": accepted, "blocked": blocked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--vulnerability-service", choices=("pypi", "osv"), default="pypi")
    parser.add_argument(
        "--exceptions", type=Path, default=Path(__file__).with_name("dependency_exceptions.json")
    )
    args = parser.parse_args()
    try:
        exceptions = json.loads(args.exceptions.read_text())
        if not isinstance(exceptions, list):
            raise ValueError("invalid exception manifest")
        with tempfile.TemporaryDirectory(prefix="dots-phone-dependency-audit-") as temporary:
            raw = Path(temporary) / "pip-audit.json"
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "--disable-pip",
                    "--require-hashes",
                    "--vulnerability-service",
                    args.vulnerability_service,
                    "--format",
                    "json",
                    "--output",
                    str(raw),
                    "-r",
                    str(args.requirements),
                ],
                check=False,
            )
            if process.returncode not in {0, 1} or not raw.is_file():
                raise ValueError("dependency audit did not complete")
            report = json.loads(raw.read_text())
        report["vulnerability_service"] = args.vulnerability_service
        report["exception_review"] = evaluate(report, exceptions, datetime.now(UTC).date())
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        result = report["exception_review"]
        print(
            f"Dependency audit: {len(result['blocked'])} blocked, "
            f"{len(result['accepted'])} findings with reviewed temporary exceptions."
        )
        raise SystemExit(0 if result["passed"] else 1)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Dependency audit failed or returned incomplete evidence.\n")


if __name__ == "__main__":
    main()
