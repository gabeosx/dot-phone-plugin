"""Report newer stable PyPI versions for direct pins in both supported projects."""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

PROJECTS = (Path("pyproject.toml"), Path("experiments/pipecat/pyproject.toml"))


def pinned_dependencies(root: Path) -> dict[str, list[dict]]:
    packages: dict[str, list[dict]] = {}
    for path in PROJECTS:
        config = tomllib.loads((root / path).read_text())
        project = config["project"]
        groups = {
            "runtime": project["dependencies"],
            "build": config.get("build-system", {}).get("requires", []),
            "overrides": config.get("tool", {}).get("uv", {}).get("override-dependencies", []),
            **{f"extra:{k}": v for k, v in project.get("optional-dependencies", {}).items()},
            **{f"group:{k}": v for k, v in config.get("dependency-groups", {}).items()},
        }
        for group, requirements in groups.items():
            for raw in requirements:
                requirement = Requirement(raw)
                specs = list(requirement.specifier)
                if (
                    requirement.url
                    or len(specs) != 1
                    or specs[0].operator != "=="
                    or "*" in specs[0].version
                ):
                    raise ValueError(f"Expected exact dependency pin in {path}: {requirement.name}")
                packages.setdefault(canonicalize_name(requirement.name), []).append(
                    {
                        "project": str(path),
                        "group": group,
                        "requirement": raw,
                        "current": str(Version(specs[0].version)),
                    }
                )
    return packages


def stable_release(metadata: dict) -> tuple[Version, str | None]:
    candidates = []
    for name, files in metadata["releases"].items():
        try:
            version = Version(name)
        except InvalidVersion:
            continue
        usable = [file for file in files if not file.get("yanked", False)]
        if usable and not (version.is_prerelease or version.is_devrelease):
            candidates.append((version, usable[0].get("requires_python")))
    if not candidates:
        raise ValueError("No usable stable release found")
    return max(candidates, key=lambda item: item[0])


def fetch_package(name: str) -> dict:
    with urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as response:
        return json.load(response)


def scan(root: Path, fetch=fetch_package) -> dict:
    checked, updates, errors = [], [], []
    for name, pins in sorted(pinned_dependencies(root).items()):
        try:
            latest, requires_python = stable_release(fetch(name))
            checked.append(name)
            newer = [pin for pin in pins if latest > Version(pin["current"])]
            if newer:
                updates.append(
                    {
                        "name": name,
                        "latest": str(latest),
                        "requires_python": requires_python,
                        "source": f"https://pypi.org/project/{name}/{latest}/",
                        "pins": newer,
                    }
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append({"name": name, "error": type(error).__name__})
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "complete": not errors,
        "checked": checked,
        "updates": updates,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("artifacts/dependency-updates.json"))
    args = parser.parse_args()
    try:
        report = scan(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, "Dependency scan failed: could not read the dependency manifests.\n")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    for update in report["updates"]:
        current = ", ".join(sorted({pin["current"] for pin in update["pins"]}))
        print(f"{update['name']}: {current} -> {update['latest']}")
    print(f"Checked {len(report['checked'])} packages; {len(report['errors'])} errors.")
    raise SystemExit(0 if report["complete"] else 2)


if __name__ == "__main__":
    main()
