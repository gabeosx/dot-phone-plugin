"""Require the complete local CI suite for the clean, checked-out commit being pushed."""

from __future__ import annotations

import os
import subprocess
import sys


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def clean_head(expected: str) -> bool:
    return git("rev-parse", "HEAD") == expected and not git(
        "status", "--porcelain", "--untracked-files=all"
    )


def main() -> int:
    updates = [line.split() for line in sys.stdin if line.strip()]
    if any(len(update) != 4 for update in updates):
        print("Push blocked: invalid Git ref update input.", file=sys.stderr)
        return 1
    # Deleting a ref does not introduce code or trigger this repository's CI.
    updates = [update for update in updates if set(update[1]) != {"0"}]
    if not updates:
        return 0
    head = git("rev-parse", "HEAD")
    for local_ref, local_sha, _, _ in updates:
        commit = git("rev-parse", "--verify", f"{local_sha}^{{commit}}")
        if commit != head:
            print(
                f"Push blocked: {local_ref} is not checked-out HEAD. "
                "Check out and validate each pushed commit separately.",
                file=sys.stderr,
            )
            return 1
    if not clean_head(head):
        print("Push blocked: commit or remove pending changes before local CI.", file=sys.stderr)
        return 1
    # Git exports repository-location variables to hooks. They must not leak to
    # CI tests that create their own temporary repositories.
    ci_env = os.environ.copy()
    for name in git("rev-parse", "--local-env-vars").splitlines():
        ci_env.pop(name, None)
    result = subprocess.run(["bash", "scripts/ci.sh"], check=False, env=ci_env)
    if result.returncode:
        print("Push blocked: local CI failed. Fix the failure and retry.", file=sys.stderr)
        return 1
    if not clean_head(head):
        print("Push blocked: source changed during CI. Commit and retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Push blocked: local CI gate could not complete.") from None
