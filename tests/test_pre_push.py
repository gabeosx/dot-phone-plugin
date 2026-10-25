"""Exercise the push gate against real temporary Git refs, without running CI."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts/pre_push.py"
ZERO = "0" * 40


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
    (tmp_path / ".gitignore").write_text("artifacts/\n")
    (tmp_path / "source.txt").write_text("original\n")
    (tmp_path / "scripts/ci.sh").write_text(
        "mkdir -p artifacts\necho ran > artifacts/ran\nexit 0\n"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def push(repo, sha=None, extra="", envvars=None):
    sha = sha or git(repo, "rev-parse", "HEAD")
    return subprocess.run(
        [sys.executable, str(GATE)],
        input=f"refs/heads/fixture {sha} refs/heads/fixture {ZERO}\n{extra}",
        cwd=repo,
        text=True,
        capture_output=True,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            **(envvars or {}),
        },
    )


def test_clean_head_runs_full_gate(repo):
    result = push(repo)
    assert result.returncode == 0, result.stderr
    assert (repo / "artifacts/ran").is_file()


@pytest.mark.parametrize("kind", ["tracked", "staged", "untracked"])
def test_dirty_source_blocks_before_running_ci(repo, kind):
    (repo / ("new.txt" if kind == "untracked" else "source.txt")).write_text("changed")
    if kind == "staged":
        git(repo, "add", "source.txt")
    assert push(repo).returncode != 0
    assert not (repo / "artifacts/ran").exists()


def test_other_branch_or_second_ref_cannot_reuse_head_validation(repo):
    old = git(repo, "rev-parse", "HEAD")
    (repo / "source.txt").write_text("new commit")
    git(repo, "commit", "-qam", "second")
    assert push(repo, old).returncode != 0
    assert push(repo, extra=f"refs/heads/old {old} refs/heads/old {ZERO}\n").returncode != 0
    assert not (repo / "artifacts/ran").exists()


def test_annotated_tag_at_head_is_validated(repo):
    git(repo, "tag", "-am", "fixture release", "v1")
    assert push(repo, git(repo, "rev-parse", "v1")).returncode == 0
    assert (repo / "artifacts/ran").exists()


@pytest.mark.parametrize("script", ["exit 1\n", "echo changed >> source.txt\n"])
def test_failed_ci_or_changes_during_ci_block_push(repo, script):
    (repo / "scripts/ci.sh").write_text(script)
    git(repo, "commit", "-qam", "change CI fixture")
    assert push(repo).returncode != 0


def test_deletion_does_not_run_ci(repo):
    assert push(repo, ZERO).returncode == 0
    assert not (repo / "artifacts/ran").exists()


def test_unresolvable_object_fails_closed(repo):
    assert push(repo, "1" * 40).returncode != 0


def test_hook_repository_environment_is_not_inherited_by_ci(repo):
    (repo / "scripts/ci.sh").write_text('test -z "${GIT_DIR:-}" && test -z "${GIT_WORK_TREE:-}"\n')
    git(repo, "commit", "-qam", "check hook environment")
    assert (
        push(repo, envvars={"GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(repo)}).returncode
        == 0
    )


def install(repo):
    hooks = repo / ".github/hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    return subprocess.run(
        ["bash", str(GATE.with_name("install-hooks.sh"))],
        cwd=repo,
        text=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )


def test_installer_enables_executable_hook_and_is_repeatable(repo):
    assert install(repo).returncode == 0
    assert git(repo, "config", "--get", "core.hooksPath") == ".github/hooks"
    assert (repo / ".github/hooks/pre-push").stat().st_mode & 0o111
    assert install(repo).returncode == 0


def test_installer_preserves_existing_hooks_path(repo):
    git(repo, "config", "core.hooksPath", "custom-hooks")
    assert install(repo).returncode != 0
    assert git(repo, "config", "--get", "core.hooksPath") == "custom-hooks"


def test_installer_preserves_other_existing_hooks(repo):
    (repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
    assert install(repo).returncode != 0
