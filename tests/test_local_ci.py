import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "scripts/local_ci.py"


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Fixture Contributor")
    git(tmp_path, "config", "user.email", "contributor@example.test")
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".gitignore").write_text("artifacts/\n.secrets/\n")
    (tmp_path / "scripts/ci-run.sh").write_text(
        "mkdir -p artifacts\ngit rev-list --count HEAD > artifacts/ancestry\n"
        "git for-each-ref > artifacts/refs\ntest ! -e .secrets/private\n"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "first")
    (tmp_path / "source.txt").write_text("source")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "second")
    return tmp_path


def run(repo):
    return subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=repo,
        text=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


def test_isolation_preserves_all_candidate_ancestry_and_excludes_unrelated_refs_and_secrets(repo):
    # An unrelated orphan commit represents privately retained backup history.
    private = subprocess.check_output(
        ["git", "-C", str(repo), "commit-tree", "HEAD^{tree}", "-m", "private backup"],
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).strip()
    git(repo, "update-ref", "refs/remotes/private-backup/main", private)
    (repo / ".secrets").mkdir()
    (repo / ".secrets/private").write_text("private fixture")
    assert git(repo, "rev-list", "--all", "--count") == "3"
    result = run(repo)
    assert result.returncode == 0, result.stderr
    reports = list((repo / "artifacts/local-ci").iterdir())
    assert len(reports) == 1
    assert (reports[0] / "ancestry").read_text().strip() == "2"
    assert (reports[0] / "refs").read_text() == ""
    assert git(repo, "rev-parse", "refs/remotes/private-backup/main") == private


def test_uncommitted_source_cannot_be_validated_as_head(repo):
    (repo / "source.txt").write_text("uncommitted")
    assert run(repo).returncode != 0
    assert not (repo / "artifacts/local-ci").exists()


@pytest.mark.parametrize("script", ["exit 1\n", "echo changed >> source.txt\n"])
def test_failed_suite_or_mutated_checkout_cannot_pass(repo, script):
    (repo / "scripts/ci-run.sh").write_text(script)
    git(repo, "commit", "-qam", "change CI fixture")
    result = run(repo)
    assert result.returncode != 0
    assert (repo / "source.txt").read_text() == "source"
