from pathlib import Path

import pytest
from packaging.version import Version

from scripts.check_dependency_updates import pinned_dependencies, scan, stable_release


def releases(*versions):
    return {"releases": {version: [{"requires_python": ">=3.12"}] for version in versions}}


def test_stable_release_ignores_prereleases_empty_and_yanked_files():
    metadata = releases("1.12.0", "1.14.0", "2.0.0rc1", "2.1.dev1")
    metadata["releases"].update({"3.0": [{"yanked": True}], "4.0": [], "invalid": [{}]})
    assert stable_release(metadata) == (Version("1.14.0"), ">=3.12")


def test_no_stable_release_is_an_error():
    with pytest.raises(ValueError):
        stable_release(releases("2.0rc1"))


def test_scan_covers_both_pipecat_pins_build_groups_extras_and_overrides():
    root = Path(__file__).resolve().parents[1]
    if not (root / "experiments/pipecat/pyproject.toml").exists():
        pytest.skip("Experimental metadata is excluded from the runtime Docker images")
    pins = pinned_dependencies(root)
    assert {pin["project"] for pin in pins["pipecat-ai"]} == {
        "pyproject.toml",
        "experiments/pipecat/pyproject.toml",
    }
    assert pins["hatchling"][0]["group"] == "build"
    assert any(pin["group"] == "overrides" for pin in pins["pillow"])
    assert any(pin["group"] == "group:dev" for pin in pins["pytest"])
    assert any(pin["group"] == "extra:bws" for pin in pins["bitwarden-sdk"])
    report = scan(root, lambda name: releases("999.0"))
    assert report["complete"]
    pipecat = next(update for update in report["updates"] if update["name"] == "pipecat-ai")
    assert len(pipecat["pins"]) == 2


def manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.check_dependency_updates.PROJECTS", (Path("pyproject.toml"),))
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["example==2.0"]\n')


def test_scan_never_downgrades(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch)
    report = scan(tmp_path, lambda name: releases("1.0", "2.0"))
    assert report["complete"] and not report["updates"]


def test_network_failure_cannot_look_like_no_updates(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch)

    def unavailable(name):
        raise TimeoutError("unavailable")

    report = scan(tmp_path, unavailable)
    assert not report["complete"]
    assert report["errors"] == [{"name": "example", "error": "TimeoutError"}]


def test_non_exact_pins_fail_instead_of_silently_skipping(tmp_path, monkeypatch):
    manifest(tmp_path, monkeypatch)
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text().replace("==2.0", ">=2.0"))
    with pytest.raises(ValueError, match="exact dependency pin"):
        pinned_dependencies(tmp_path)
