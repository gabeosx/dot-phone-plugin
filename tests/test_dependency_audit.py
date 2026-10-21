from datetime import date

import pytest

from scripts.dependency_audit import evaluate


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
