"""Keep the certificate-trust security baseline in runtime and CI tooling."""

import re
from importlib.metadata import PackageNotFoundError, requires
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version


def test_ci_and_container_uv_pins_include_the_fixed_certificate_store():
    root = Path(__file__).resolve().parents[1]
    if root == Path("/app") and Path("/.dockerenv").is_file() and not (root / ".git").exists():
        pytest.skip("Runtime images omit workflow and Docker build metadata")
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    # New CI checks belong in the shared runner so the pre-push gate cannot drift.
    assert set(workflow["jobs"]) == {"offline"}
    assert [step["run"] for step in workflow["jobs"]["offline"]["steps"] if "run" in step] == [
        "bash scripts/ci.sh"
    ]
    setup = next(
        step
        for step in workflow["jobs"]["offline"]["steps"]
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    )
    version = Version(setup["with"]["version"])
    assert version >= Version("0.13.4")
    runner = (root / "scripts/ci-run.sh").read_text()
    assert f"uv=={version}" in runner
    native_python = re.search(r"--python (3\.\d+\.\d+)", runner)
    assert native_python and Version(native_python[1]) >= Version("3.13.18")
    for name in (
        "Dockerfile",
        "experiments/pipecat/Dockerfile",
        "experiments/pipecat/Dockerfile.phone",
    ):
        source = (root / name).read_text()
        pin = re.search(r"astral-sh/uv:([0-9.]+)@sha256:([a-f0-9]{64})", source)
        assert pin and Version(pin[1]) == version
    for name in (
        "Dockerfile",
        "experiments/pipecat/Dockerfile",
        "experiments/pipecat/Dockerfile.phone",
        "experiments/pipecat/Dockerfile.tunnel",
    ):
        source = (root / name).read_text()
        pins = re.findall(r"FROM python:([0-9.]+)-slim@sha256:([a-f0-9]{64})", source)
        assert pins and all(Version(pin) >= Version("3.13.18") for pin, _ in pins)


def test_pipecat_supports_the_fixed_pillow_without_an_override():
    try:
        requirements = requires("pipecat-ai")
    except PackageNotFoundError:
        pytest.skip("Pipecat is an optional dependency in the base image")
    pillow = next(
        Requirement(raw) for raw in requirements if Requirement(raw).name.lower() == "pillow"
    )
    assert "13.0.0" in pillow.specifier
