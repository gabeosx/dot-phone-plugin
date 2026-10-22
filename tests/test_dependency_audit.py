import json
import tomllib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from scripts.dependency_audit import evaluate, main


@pytest.mark.parametrize("project", [".", "experiments/pipecat"])
def test_supported_voice_projects_lock_fixed_pillow_without_exceptions(project):
    root = Path(__file__).resolve().parents[1]
    if (
        project == "experiments/pipecat"
        and not (root / project / "pyproject.toml").exists()
        and root == Path("/app")
        and Path("/.dockerenv").is_file()
        and not (root / ".git").exists()
    ):
        # The base Docker image includes worker scripts, not the experimental
        # project's dependency metadata. Full checkouts must validate both.
        pytest.skip("Experimental project metadata is excluded from the base runtime image")
    config = tomllib.loads((root / project / "pyproject.toml").read_text())
    lock = tomllib.loads((root / project / "uv.lock").read_text())
    pillow = next(package for package in lock["package"] if package["name"] == "pillow")
    assert Version(pillow["version"]) >= Version("13.0.0")
    overrides = [Requirement(value) for value in config["tool"]["uv"]["override-dependencies"]]
    override = next(item for item in overrides if item.name == "pillow")
    assert pillow["version"] in override.specifier
    assert json.loads((root / "scripts/dependency_exceptions.json").read_text()) == []


def evidence(name="pillow", version="12.3.0", advisory="reviewed-id"):
    return {"dependencies": [{"name": name, "version": version, "vulns": [{"id": advisory}]}]}


def exception():
    return {
        "name": "pillow",
        "version": "12.3.0",
        "ids": ["reviewed-id"],
        "expires": "2026-11-03",
        "reason": "Reviewed voice-only boundary",
    }


def test_exact_reviewed_finding_remains_visible():
    result = evaluate(evidence(), [exception()], date(2026, 10, 20))
    assert result["passed"]
    assert result["accepted"] == [
        {"name": "pillow", "version": "12.3.0", "id": "reviewed-id", "expires": "2026-11-03"}
    ]


@pytest.mark.parametrize(
    "report,today",
    [
        (evidence(advisory="new-advisory"), date(2026, 10, 20)),
        (evidence(name="another-package"), date(2026, 10, 20)),
        (evidence(version="12.4.0"), date(2026, 10, 20)),
        (evidence(), date(2026, 11, 3)),
    ],
)
def test_exception_cannot_hide_new_or_expired_findings(report, today):
    result = evaluate(report, [exception()], today)
    assert not result["passed"]
    assert result["blocked"] and not result["accepted"]


@pytest.mark.parametrize("report", [{}, {"dependencies": []}])
def test_missing_audit_evidence_cannot_pass(report):
    with pytest.raises(ValueError):
        evaluate(report, [], date(2026, 10, 20))


def test_skipped_package_cannot_pass():
    report = {
        "dependencies": [
            {
                "name": "package",
                "version": "1.0",
                "vulns": [],
                "skip_reason": "unresolved requirement",
            }
        ]
    }
    result = evaluate(report, [], date(2026, 10, 20))
    assert not result["passed"]


@pytest.mark.parametrize(
    "dependency",
    [
        {"vulns": []},
        {"name": "package", "version": "1.0", "vulns": ""},
        {"name": "", "version": "1.0", "vulns": []},
        {"name": "package", "version": " ", "vulns": []},
        {"name": "package", "version": "1.0", "vulns": [{}]},
    ],
)
def test_incomplete_dependency_or_advisory_cannot_pass(dependency):
    with pytest.raises(ValueError):
        evaluate({"dependencies": [dependency]}, [], date(2026, 10, 20))


@pytest.mark.parametrize("service", ["pypi", "osv"])
def test_cli_audits_selected_feed_and_retains_its_findings(monkeypatch, tmp_path, service):
    report_path = tmp_path / "audit.json"
    exceptions_path = tmp_path / "exceptions.json"
    exceptions_path.write_text("[]")
    monkeypatch.setattr(
        "sys.argv",
        [
            "dependency_audit.py",
            "--requirements",
            str(tmp_path / "requirements.txt"),
            "--report",
            str(report_path),
            "--exceptions",
            str(exceptions_path),
            "--vulnerability-service",
            service,
        ],
    )

    def audit(command, **kwargs):
        assert command[command.index("--vulnerability-service") + 1] == service
        assert "--require-hashes" in command
        assert "--ignore-vuln" not in command
        Path(command[command.index("--output") + 1]).write_text(json.dumps(evidence()))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("scripts.dependency_audit.subprocess.run", audit)
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 1
    report = json.loads(report_path.read_text())
    assert report["vulnerability_service"] == service
    assert report["dependencies"] == evidence()["dependencies"]
    assert not report["exception_review"]["passed"]
