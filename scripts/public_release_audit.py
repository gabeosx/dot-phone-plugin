"""Audit/export a reviewed public snapshot without copying Git or private local state.

This supplements credential scanning and human review; it is not a guarantee that
arbitrary personal information can be recognized automatically. Reports contain
locations and hashes, never matched values. Run Gitleaks on both history and the
export before changing repository visibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from secret_scan import PATTERNS, history

PUBLIC_ROOTS = {
    "dots_phone",
    "plugins",
    "tests",
    "scripts",
    "evals",
    "vendor",
    "experiments",
    "docs",
    "website",
    ".github",
}
PUBLIC_FILES = {
    ".agents/plugins/marketplace.json",
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "Dockerfile",
    "README.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "MILESTONES.md",
    "pyproject.toml",
    "uv.lock",
}
PRIVATE_PARTS = {
    ".git",
    ".secrets",
    "data",
    "artifacts",
    "recordings",
    "transcripts",
    "private-evidence",
    "private-tests",
    "research",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".openai",
    ".coverage",
    ".DS_Store",
}


PRIVATE_SUFFIXES = {
    ".wav",
    ".mp3",
    ".mp4",
    ".ogg",
    ".flac",
    ".m4a",
    ".webm",
    ".pcm",
    ".pcmu",
    ".mulaw",
    ".ulaw",
    ".onnx",
    ".kef",
    ".pt",
    ".safetensors",
}


def public_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(p in PRIVATE_PARTS for p in path.parts):
        return False
    if not path.parts or name.startswith("docs/research/"):
        return False
    if any(p.startswith(".env") and p != ".env.example" for p in path.parts):
        return False
    if path.suffix.lower() in PRIVATE_SUFFIXES | {
        ".pyc",
        ".pyo",
        ".pem",
        ".key",
        ".sqlite",
        ".sqlite3",
        ".db",
    }:
        return False
    if "dist" in path.parts and not name.startswith("website/dist/"):
        return False
    return (
        name in PUBLIC_FILES
        or name.startswith(".agents/skills/dots-phone-setup/")
        or (len(path.parts) == 1 and path.match("compose*.yaml"))
        or path.parts[0] in PUBLIC_ROOTS
    )


def display_path(name: str, denylist: list[str]) -> str:
    if any(value.casefold() in name.casefold() for value in denylist) or any(
        pattern.search(name) for pattern in PATTERNS
    ):
        return "[private path]"
    return name


def scan_content(name: str, content: bytes, denylist: list[str]) -> list[dict]:
    issues = []
    for line, text in enumerate(content.decode(errors="replace").splitlines(), 1):
        if any(pattern.search(text) for pattern in PATTERNS):
            issues.append(
                {"location": name, "line": line, "category": "credential_or_private_config"}
            )
        if any(value.casefold() in text.casefold() for value in denylist):
            issues.append({"location": name, "line": line, "category": "private_denylist"})
    if display_path(name, denylist) != name:
        # A private filename must not be echoed into the report.
        issues.append({"location": "[private path]", "category": "private_path"})
        for issue in issues:
            issue["location"] = "[private path]"
    return issues


def candidates() -> list[str]:
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        stderr=subprocess.DEVNULL,
    )
    return sorted(set(name for name in raw.decode().split("\0") if name))


def archive_members(path: Path):
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("distribution contains a link")
                if not item.is_dir():
                    yield item.filename, archive.read(item)
    else:
        with tarfile.open(path, "r:*") as archive:
            for item in archive:
                if item.issym() or item.islnk():
                    raise ValueError("distribution contains a link")
                if item.isfile():
                    source = archive.extractfile(item)
                    if source is None:
                        raise ValueError("cannot read distribution member")
                    yield item.name, source.read()


def audit_distribution(path: Path, denylist: list[str]) -> dict:
    issues, members = [], []
    for name, content in archive_members(path):
        parts = PurePosixPath(name).parts
        unsafe = (
            PurePosixPath(name).is_absolute()
            or ".." in parts
            or any(p in PRIVATE_PARTS for p in parts)
            or PurePosixPath(name).suffix.lower() in PRIVATE_SUFFIXES
            or any(p.startswith(".env") and p != ".env.example" for p in parts)
        )
        if unsafe:
            issues.append(
                {"location": display_path(name, denylist), "category": "private_distribution_path"}
            )
        issues.extend(scan_content(name, content, denylist))
        members.append(
            {
                "path": display_path(name, denylist),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    return {
        "archive": display_path(path.name, denylist),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "members": members,
        "issues": issues,
    }


def audit(
    denylist: list[str], include_history: bool, distributions: list[Path]
) -> tuple[dict, dict[str, bytes]]:
    issues, excluded, files = [], [], []
    source = {}
    for name in candidates():
        path = Path(name)
        if not public_path(name):
            excluded.append(display_path(name, denylist))
            continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            issues.append(
                {"location": display_path(name, denylist), "category": "symlink_not_exported"}
            )
            continue
        if not path.is_file():
            continue  # A tracked deletion is not part of the working snapshot.
        content = path.read_bytes()
        issues.extend(scan_content(name, content, denylist))
        source[name] = content
        files.append(
            {
                "path": display_path(name, denylist),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    history_count = 0
    if include_history:
        for name, content in history():
            history_count += 1
            issues.extend(scan_content(name, content, denylist))
    packages = [audit_distribution(path, denylist) for path in distributions]
    report = {
        "schema": 1,
        "scanner": "project_patterns_and_optional_private_denylist",
        "files": files,
        "excluded": excluded,
        "history_objects": history_count,
        "distributions": packages,
        "issues": issues,
        "passed": not issues and not any(p["issues"] for p in packages),
        "limitations": "Requires separate Gitleaks and human confidentiality review; no network or real account access.",
    }
    return report, source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--history", action="store_true")
    parser.add_argument(
        "--denylist", type=Path, help="private JSON array of literal values; never copied"
    )
    parser.add_argument("--distribution", type=Path, action="append", default=[])
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="new destination; refuses findings and never copies Git history",
    )
    args = parser.parse_args()
    try:
        denied = json.loads(args.denylist.read_text()) if args.denylist else []
        if not isinstance(denied, list) or any(
            not isinstance(s, str) or not s.strip() for s in denied
        ):
            raise ValueError("invalid denylist")
        if args.export_dir and args.export_dir.exists():
            raise ValueError("export destination already exists")
        report, source = audit(denied, args.history, args.distribution)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        if not report["passed"]:
            parser.exit(
                1,
                "Public snapshot audit found issues. See the redacted report; no export created.\n",
            )
        if args.export_dir:
            args.export_dir.mkdir(parents=True)
            try:
                for name, content in source.items():
                    dest = args.export_dir / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                    dest.chmod(Path(name).stat().st_mode & 0o777)
            except Exception:
                shutil.rmtree(args.export_dir)
                raise
        print(
            f"Public snapshot audit passed ({len(source)} files, {report['history_objects']} history objects, {len(args.distribution)} distributions)."
        )
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ):
        parser.exit(
            2, "Public snapshot audit failed; verify paths and repository. Values suppressed.\n"
        )


if __name__ == "__main__":
    main()
