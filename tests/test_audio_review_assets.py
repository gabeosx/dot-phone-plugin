"""Pinned model provisioning stays offline when cached and rejects partial data."""

import hashlib
import io
import json
import urllib.error

import pytest

from dots_phone import audio_review_assets as assets


@pytest.fixture
def model_files(monkeypatch):
    contents = {name: ("synthetic " + name).encode() for name in assets.MODELS["small.en"]["files"]}
    models = {
        model: {
            **selected,
            "files": {
                name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for name, value in contents.items()
            },
        }
        for model, selected in assets.MODELS.items()
    }
    monkeypatch.setattr(assets, "MODELS", models)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(assets.urllib.request, "urlopen", forbidden)
    return contents


def write_cache(directory, contents):
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in contents.items():
        (directory / name).write_bytes(value)


def fake_downloads(monkeypatch, contents, model="small.en"):
    calls = []

    def fetch(request, *, timeout):
        assert timeout == assets.DOWNLOAD_TIMEOUT_SECONDS
        url = request.full_url
        assert (
            f"/{assets.MODELS[model]['repository']}/resolve/{assets.MODELS[model]['revision']}/"
            in url
        )
        assert request.get_header("Authorization") is None
        name = url.split("/")[-1].split("?")[0]
        calls.append(name)
        value = contents[name]
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)

    monkeypatch.setattr(assets.urllib.request, "urlopen", fetch)
    return calls


def test_verification_is_read_only_and_preparation_adds_cached_model_notices_offline(
    tmp_path, model_files
):
    write_cache(tmp_path, model_files)
    before = {name: (tmp_path / name).stat().st_mtime_ns for name in model_files}
    manifest = assets.verify_assets(tmp_path)
    assert manifest["revision"] == assets.MODELS["small.en"]["revision"]
    assert manifest["files"] == assets.MODELS["small.en"]["files"]
    assert not (tmp_path / assets.PROVENANCE_NAME).exists()
    assert not (tmp_path / assets.NOTICE_NAME).exists()
    assert assets.prepare_assets(tmp_path) == manifest
    notice = (tmp_path / assets.NOTICE_NAME).read_text()
    assert assets.WHISPER_LICENSE in notice
    assert "Systran" in notice and manifest["revision"] in notice
    assert (tmp_path / assets.PROVENANCE_NAME).exists()
    assert {name: (tmp_path / name).stat().st_mtime_ns for name in model_files} == before


def test_preparation_pins_origin_and_retains_verified_provenance(
    tmp_path, model_files, monkeypatch
):
    calls = fake_downloads(monkeypatch, model_files)
    output = tmp_path / "models"
    manifest = assets.prepare_assets(output)
    assert set(calls) == set(model_files)
    assert {name: (output / name).read_bytes() for name in model_files} == model_files
    assert manifest["repository"] == "Systran/faster-whisper-small.en"
    assert (
        json.loads((output / assets.PROVENANCE_NAME).read_text())["revision"]
        == assets.MODELS["small.en"]["revision"]
    )
    assert not list(output.glob(".download-*"))
    assert assets.prepare_assets(output) == manifest
    assert len(calls) == len(model_files)


def test_existing_corruption_is_not_overwritten_or_downloaded(tmp_path, model_files):
    write_cache(tmp_path, model_files)
    (tmp_path / "model.bin").write_bytes(b"x" * len(model_files["model.bin"]))
    with pytest.raises(assets.AudioReviewAssetsError, match="checksum"):
        assets.prepare_assets(tmp_path)
    assert (tmp_path / "model.bin").read_bytes() == b"x" * len(model_files["model.bin"])


def test_resuming_partial_verified_cache_fetches_only_missing_files(
    tmp_path, model_files, monkeypatch
):
    write_cache(tmp_path, {"model.bin": model_files["model.bin"]})
    existing_mtime = (tmp_path / "model.bin").stat().st_mtime_ns
    calls = fake_downloads(monkeypatch, model_files)
    assets.prepare_assets(tmp_path)
    assert "model.bin" not in calls
    assert len(calls) == len(model_files) - 1
    assert (tmp_path / "model.bin").stat().st_mtime_ns == existing_mtime


def test_network_failure_does_not_publish_any_new_files(tmp_path, model_files, monkeypatch):
    contents = {**model_files, "config.json": urllib.error.URLError("private network detail")}
    fake_downloads(monkeypatch, contents)
    with pytest.raises(
        assets.AudioReviewAssetsError, match="^Audio-review model download failed; retry setup$"
    ):
        assets.prepare_assets(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["checksum", "truncated", "oversized"])
def test_unverified_downloads_never_become_installed_files(
    tmp_path, model_files, monkeypatch, failure
):
    value = model_files["config.json"]
    damaged = {
        "checksum": b"x" * len(value),
        "truncated": value[:-1],
        "oversized": value + b"extra",
    }[failure]
    fake_downloads(monkeypatch, {**model_files, "config.json": damaged})
    with pytest.raises(assets.AudioReviewAssetsError, match="pinned"):
        assets.prepare_assets(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["model.bin", assets.PROVENANCE_NAME])
@pytest.mark.parametrize("broken", [False, True])
def test_model_and_provenance_symlinks_are_rejected(tmp_path, model_files, name, broken):
    output = tmp_path / "models"
    write_cache(output, model_files)
    target = tmp_path / "elsewhere"
    if not broken:
        target.write_bytes(model_files.get(name, b"{}"))
    (output / name).unlink(missing_ok=True)
    (output / name).symlink_to(target)
    with pytest.raises(assets.AudioReviewAssetsError, match="symlinks"):
        assets.prepare_assets(output)
    assert (output / name).is_symlink()


def test_symlinked_directory_is_rejected_before_writes(tmp_path, model_files):
    target = tmp_path / "actual"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(assets.AudioReviewAssetsError, match="must not use symlinks"):
        assets.prepare_assets(alias / "models")
    assert list(target.iterdir()) == []


def test_fifo_model_is_rejected_without_blocking(tmp_path, model_files):
    import os

    write_cache(tmp_path, model_files)
    (tmp_path / "model.bin").unlink()
    os.mkfifo(tmp_path / "model.bin")
    with pytest.raises(assets.AudioReviewAssetsError, match="regular files"):
        assets.verify_assets(tmp_path)


def test_changed_revision_metadata_cannot_claim_a_verified_cache(tmp_path, model_files):
    write_cache(tmp_path, model_files)
    metadata = {**assets._provenance(), "revision": "unreviewed-upstream"}
    (tmp_path / assets.PROVENANCE_NAME).write_text(json.dumps(metadata))
    with pytest.raises(assets.AudioReviewAssetsError, match="provenance does not match"):
        assets.verify_assets(tmp_path)


def test_preparation_refuses_git_checkout(tmp_path, model_files):
    (tmp_path / ".git").mkdir()
    with pytest.raises(assets.AudioReviewAssetsError, match="outside a Git checkout"):
        assets.prepare_assets(tmp_path / "models")
    assert not (tmp_path / "models").exists()


def test_notice_symlink_is_not_followed_or_overwritten(tmp_path, model_files):
    output = tmp_path / "models"
    write_cache(output, model_files)
    other = tmp_path / "other"
    other.write_text("unrelated file")
    (output / assets.NOTICE_NAME).symlink_to(other)
    with pytest.raises(assets.AudioReviewAssetsError, match="notice must be a regular file"):
        assets.prepare_assets(output)
    assert other.read_text() == "unrelated file"
    assert assets.verify_assets(output)["model"] == "small.en"


def test_download_time_limit_discards_staged_files(tmp_path, model_files, monkeypatch):
    fake_downloads(monkeypatch, model_files)
    monkeypatch.setattr(assets, "PREPARE_TIMEOUT_SECONDS", 0)
    with pytest.raises(assets.AudioReviewAssetsError, match="time limit"):
        assets.prepare_assets(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_verify_never_creates_or_fetches_missing_files(tmp_path, model_files, capsys):
    output = tmp_path / "absent"
    assert assets.main(["--output", str(output), "--verify"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Audio-review model files are missing\n"
    assert not output.exists()


def test_cli_prints_usable_directory_or_explicit_json(tmp_path, model_files, capsys):
    write_cache(tmp_path, model_files)
    assert assets.main(["--output", str(tmp_path), "--verify"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path)
    assert assets.main(["--output", str(tmp_path), "--verify", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["directory"] == str(tmp_path)


def test_mac_default_uses_external_application_support(monkeypatch, tmp_path):
    monkeypatch.setattr(assets.sys, "platform", "darwin")
    monkeypatch.setattr(assets.Path, "home", lambda: tmp_path)
    assert (
        assets.default_directory()
        == tmp_path / "Library/Application Support/DotsPhone/audio-review/small.en"
    )


def test_medium_selection_downloads_and_verifies_its_own_pinned_revision(
    tmp_path, model_files, monkeypatch
):
    medium_contents = {name: b"medium " + value for name, value in model_files.items()}
    assets.MODELS["medium.en"]["files"] = {
        name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        for name, value in medium_contents.items()
    }
    calls = fake_downloads(monkeypatch, medium_contents, model="medium.en")
    manifest = assets.prepare_assets(tmp_path, model="medium.en")
    assert len(calls) == len(medium_contents)
    assert manifest["repository"] == "Systran/faster-whisper-medium.en"
    assert manifest["revision"] == "a29b04bd15381511a9af671baec01072039215e3"
    assert assets.verify_assets(tmp_path, model="medium.en") == manifest
    assert assets.prepare_assets(tmp_path, model="medium.en") == manifest
    assert len(calls) == len(medium_contents)
    with pytest.raises(assets.AudioReviewAssetsError, match="pinned revision"):
        assets.verify_assets(tmp_path)


def test_cli_medium_selection_uses_medium_default_directory(
    tmp_path, model_files, monkeypatch, capsys
):
    monkeypatch.setattr(assets.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(assets.sys, "platform", "darwin")
    output = assets.default_directory("medium.en")
    write_cache(output, model_files)
    assert assets.main(["--model", "medium.en", "--verify", "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["model"] == "medium.en"
    assert manifest["directory"] == str(output)


def test_api_rejects_unknown_model_before_filesystem_or_network(tmp_path, model_files):
    output = tmp_path / "absent"
    with pytest.raises(assets.AudioReviewAssetsError, match="supported audio-review model"):
        assets.prepare_assets(output, model="unreviewed-model")
    assert not output.exists()
