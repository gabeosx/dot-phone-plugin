"""Run the complete acceptance suite in an isolated copy of clean, committed HEAD."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def clean(root: Path, head: str) -> bool:
    return git(root, "rev-parse", "HEAD") == head and not git(
        root, "status", "--porcelain", "--untracked-files=all"
    )


def validate(root: Path) -> int:
    head = git(root, "rev-parse", "HEAD")
    if not clean(root, head):
        print("Local CI requires clean, committed source. Commit changes first.", file=sys.stderr)
        return 1
    report = root / "artifacts/local-ci" / f"{head[:12]}-{time.time_ns()}"
    with tempfile.TemporaryDirectory(prefix="dots-phone-ci-") as temporary:
        checkout = Path(temporary) / "source"
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        # Fetch just the proposed commit and its COMPLETE ancestry. Import no
        # unrelated private backup refs, stashes, ignored state or local secrets.
        subprocess.run(
            ["git", "-C", str(checkout), "fetch", "--quiet", "--no-tags", str(root), head],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", head], check=True
        )
        print(f"Validating committed source {head} in an isolated checkout.", flush=True)
        try:
            result = subprocess.run(["bash", "scripts/ci-run.sh"], cwd=checkout, check=False)
            passed = result.returncode == 0 and clean(checkout, head) and clean(root, head)
        finally:
            artifacts = checkout / "artifacts"
            if artifacts.is_dir():
                report.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(artifacts, report, symlinks=True)
                print(f"Local CI reports: {report}", flush=True)
        if not passed:
            print("Local CI failed or source changed during validation.", file=sys.stderr)
        return 0 if passed else 1


def main() -> int:
    # Also support direct invocation from a Git hook without contaminating the
    # isolated repository with the invoking repository's GIT_DIR/index settings.
    for name in subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"], text=True
    ).splitlines():
        os.environ.pop(name, None)
    return validate(Path(git(Path.cwd(), "rev-parse", "--show-toplevel")))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Local CI could not complete; pushing remains blocked.") from None
