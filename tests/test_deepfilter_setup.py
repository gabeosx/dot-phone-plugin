"""Atomic, pinned downloads to an external cache; no network in these tests."""

import hashlib
import io
import json

import pytest

from dots_phone import deepfilter_assets as assets
from dots_phone import deepfilter_setup as setup


@pytest.fixture
def downloads(monkeypatch):
    model = b"synthetic model fixture"
    digest = hashlib.sha256(model).hexdigest()
    for module in [assets, setup]:
        monkeypatch.setattr(module, "MODEL_BYTES", len(model))
        monkeypatch.setattr(module, "MODEL_SHA256", digest)
    monkeypatch.setattr(
        setup, "LICENSE_HASHES", {"LICENSE-MIT": hashlib.sha256(b"license").hexdigest()}
    )
    calls = []

    def fetch(request, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        calls.append(url)
        return io.BytesIO(model if url == setup.MODEL_URL else b"license")

    monkeypatch.setattr(setup.urllib.request, "urlopen", fetch)
    return model, calls


def test_verified_install_retains_origin_and_is_offline_when_cached(tmp_path, downloads):
    model, calls = downloads
    path = setup.install(tmp_path)
    assert path.read_bytes() == model
    assert len(calls) == 2
    assert json.loads((tmp_path / "provenance.json").read_text())["sha256"] == assets.MODEL_SHA256
    assert setup.install(tmp_path) == path and len(calls) == 2
    assert not list(tmp_path.glob(".download-*"))


def test_bad_checksum_never_becomes_a_model(tmp_path, downloads, monkeypatch):
    monkeypatch.setattr(assets, "MODEL_SHA256", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum"):
        setup.install(tmp_path)
    assert not (tmp_path / assets.MODEL_NAME).exists()
    assert not list(tmp_path.glob(".download-*"))


def test_install_refuses_a_git_checkout(tmp_path, downloads):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside a Git checkout"):
        setup.install(tmp_path / "models")
    assert not downloads[1]
