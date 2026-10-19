from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCANNER = Path(__file__).resolve().parents[1] / "scripts/secret_scan.py"


def run(repo, *args):
    return subprocess.run(
        [sys.executable, str(SCANNER), *args], cwd=repo, capture_output=True, text=True
    )


def test_private_denylist_detects_historical_values_without_printing_them(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git CLI is needed only for the history scanner test")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Fictional contributor")
    git("config", "user.email", "contributor@example.invalid")
    private = "fictional-owner-private-value"
    (tmp_path / "notes.txt").write_text(private)
    git("add", "notes.txt")
    git("commit", "-qm", "Initial fixture")
    (tmp_path / "notes.txt").write_text("Generic public notes")
    git("commit", "-am", "Remove fixture")
    denylist = tmp_path / "private-denylist.json"
    denylist.write_text(json.dumps([private]))
    assert run(tmp_path, "--denylist", str(denylist)).returncode == 0
    historical = run(tmp_path, "--history", "--denylist", str(denylist))
    assert historical.returncode == 1
    assert "notes.txt" in historical.stdout
    assert private not in historical.stdout + historical.stderr


def test_missing_denylist_does_not_silently_pass(tmp_path):
    result = run(tmp_path, "--denylist", str(tmp_path / "missing.json"))
    assert result.returncode == 2
    assert "Values suppressed" in result.stderr
