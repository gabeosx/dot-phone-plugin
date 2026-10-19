"""Scan tracked files or reachable Git history; report locations, never values."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:auth_token|api_secret|bws_access_token)\s*[=:]\s*['\"][A-Za-z0-9+/_.-]{24,}['\"]"
    ),
    re.compile(r"(?:asdk_app|tunnel)_[a-f0-9]{24,}"),
    re.compile(r"\+1(?![2-9][0-9]{2}55501[0-9]{2})(?!212555020[0-2])[2-9][0-9]{9}"),
    re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
    re.compile(r"connector/oauth/[A-Za-z0-9_-]{8,}"),
    re.compile("/" + r"Users/[A-Za-z0-9_.-]+/"),
    re.compile(r"(?i)BWS_PROJECT_ID\s*=\s*[a-f0-9-]{36}"),
]


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def tracked():
    try:
        paths = [Path(x) for x in git("ls-files", "-z").decode().split("\0") if x]
    except (FileNotFoundError, subprocess.CalledProcessError):
        paths = []
    if not paths:
        roots = (
            "dots_phone",
            "tests",
            "scripts",
            "plugins",
            "vendor",
            ".github",
            ".agents",
            "evals",
            "docs",
        )
        paths = [
            p
            for root in roots
            for p in Path(root).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ]
        paths += [p for p in Path(".").iterdir() if p.is_file()]
    for path in paths:
        if path.is_file():
            yield str(path), path.read_bytes()


def history():
    objects = git("rev-list", "--objects", "--all").decode().splitlines()
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        for row in objects:
            oid, _, name = row.partition(" ")
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().decode().split()
            if len(header) != 3:
                raise RuntimeError("cannot read complete Git history")
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            if header[1] in {"blob", "commit", "tag"}:
                yield oid + (":" + name if name else ""), data
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    if process.returncode:
        raise RuntimeError("Git history scan failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--denylist", type=Path, help="private JSON list of literal personal values"
    )
    args = parser.parse_args()
    try:
        denied = json.loads(args.denylist.read_text()) if args.denylist else []
        if not isinstance(denied, list) or any(
            not isinstance(s, str) or not s.strip() for s in denied
        ):
            raise ValueError("invalid denylist")
        failures = []
        count = 0
        for location, data in history() if args.history else tracked():
            count += 1
            content = data.decode(errors="replace")
            for line, text in enumerate(content.splitlines(), 1):
                if any(p.search(text) for p in PATTERNS) or any(
                    v.casefold() in text.casefold() for v in denied
                ):
                    failures.append(
                        f"{location}:{line}: possible credential or private configuration"
                    )
            if any(v.casefold() in location.casefold() for v in denied):
                # Filenames can themselves contain personal values; suppress the location.
                failures.append("Git object/path: private denylist match")
        print(
            "\n".join(failures)
            if failures
            else f"Privacy/credential scan passed ({count} objects/files)."
        )
        raise SystemExit(bool(failures))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError):
        parser.exit(
            2, "Privacy scan failed; verify repository and private denylist. Values suppressed.\n"
        )


if __name__ == "__main__":
    main()
