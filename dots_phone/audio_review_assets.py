"""Prepare pinned local speech-recognition weights outside the checkout.

This explicit setup command downloads public model files. Verification is fully
offline; neither operation imports the recognizer or processes call audio.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SMALL_FILES = {
    "model.bin": {
        "bytes": 483_545_366,
        "sha256": "62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a",
    },
    "config.json": {
        "bytes": 2_657,
        "sha256": "666a9605530ac1f61fa8177f3702b4dacec9966749e42610839fcc32661d5fae",
    },
    "tokenizer.json": {
        "bytes": 2_128_466,
        "sha256": "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df",
    },
    "vocabulary.txt": {
        "bytes": 422_309,
        "sha256": "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf",
    },
}
MODELS = {
    "small.en": {
        "repository": "Systran/faster-whisper-small.en",
        "revision": "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
        "files": SMALL_FILES,
    },
    "medium.en": {
        "repository": "Systran/faster-whisper-medium.en",
        "revision": "a29b04bd15381511a9af671baec01072039215e3",
        "files": {
            "model.bin": {
                "bytes": 1_527_904_330,
                "sha256": "11b220779aea4c6f3ce9d2549c8a95ea869ed84066864b999531ef53e594fe5b",
            },
            "config.json": {
                "bytes": 2_643,
                "sha256": "4a1848ebabe7938d9797c15a2e8e4ce1d36e6fd4a43d096ae5955257c67c7962",
            },
            "tokenizer.json": dict(SMALL_FILES["tokenizer.json"]),
            "vocabulary.txt": dict(SMALL_FILES["vocabulary.txt"]),
        },
    },
}
PROVENANCE_NAME = "provenance.json"
NOTICE_NAME = "NOTICE-Whisper.txt"
WHISPER_LICENSE = """MIT License

Copyright (c) 2022 OpenAI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
DOWNLOAD_TIMEOUT_SECONDS = 60
PREPARE_TIMEOUT_SECONDS = 600
CHUNK_BYTES = 65_536


class AudioReviewAssetsError(ValueError):
    """A fixed, non-secret diagnostic suitable for setup output."""


def _model_spec(model):
    if not isinstance(model, str) or model not in MODELS:
        raise AudioReviewAssetsError("Choose a supported audio-review model: small.en or medium.en")
    return MODELS[model]


def default_directory(model="small.en"):
    _model_spec(model)
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "DotsPhone"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        root /= "dots-phone"
    return root / "audio-review" / model


def _directory_path(directory):
    # Do not resolve away a symlink before checking it. No model path component
    # may silently redirect setup writes or a later read-only container mount.
    path = Path(os.path.abspath(Path(directory).expanduser()))
    for parent in reversed((path, *path.parents)):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise AudioReviewAssetsError("Audio-review model directory is unreadable") from None
        if stat.S_ISLNK(mode):
            raise AudioReviewAssetsError("Audio-review model directory must not use symlinks")
        if not stat.S_ISDIR(mode):
            raise AudioReviewAssetsError("Audio-review model directory is not a directory")
        if (parent / ".git").exists():
            raise AudioReviewAssetsError(
                "Choose an audio-review model directory outside a Git checkout"
            )
    return path


def _read_file(path, expected):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise AudioReviewAssetsError("Audio-review model files must be regular files")
            if info.st_size != expected["bytes"]:
                raise AudioReviewAssetsError(
                    "Audio-review model file size does not match the pinned revision"
                )
            actual = hashlib.file_digest(source, "sha256").hexdigest()
    except FileNotFoundError:
        raise AudioReviewAssetsError("Audio-review model files are missing") from None
    except OSError:
        raise AudioReviewAssetsError(
            "Audio-review model files are unreadable or use symlinks"
        ) from None
    if actual != expected["sha256"]:
        raise AudioReviewAssetsError(
            "Audio-review model checksum does not match the pinned revision"
        )


def _provenance(model="small.en"):
    selected = _model_spec(model)
    return {
        "schema": 1,
        "model": model,
        "repository": selected["repository"],
        "revision": selected["revision"],
        "source_url": f"https://huggingface.co/{selected['repository']}/tree/{selected['revision']}",
        "files": {name: dict(expected) for name, expected in selected["files"].items()},
    }


def _verify_provenance(directory, model):
    path = directory / PROVENANCE_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # Manually provisioned pinned upstream files remain verifiable offline.
        return
    except OSError:
        raise AudioReviewAssetsError(
            "Audio-review model provenance is unreadable or uses symlinks"
        ) from None
    try:
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
                raise AudioReviewAssetsError("Audio-review model provenance is invalid")
            value = json.loads(source.read())
    except (OSError, ValueError):
        raise AudioReviewAssetsError("Audio-review model provenance is invalid") from None
    if value != _provenance(model):
        raise AudioReviewAssetsError(
            "Audio-review model provenance does not match the pinned revision"
        )


def _notice(model):
    selected = _model_spec(model)
    return (
        "Whisper model weights by OpenAI; CTranslate2 conversion published by Systran.\n"
        f"Model repository: https://huggingface.co/{selected['repository']}\n"
        f"Pinned revision: {selected['revision']}\n"
        "The model card identifies these weights as MIT licensed.\n"
        "Upstream license: https://github.com/openai/whisper/blob/main/LICENSE\n\n"
        + WHISPER_LICENSE
    )


def _verify_notice_target(directory):
    try:
        info = (directory / NOTICE_NAME).lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise AudioReviewAssetsError("Audio-review model notice must be a regular file")


def verify_assets(directory, *, model="small.en"):
    """Hash the four pinned files without writes, network, or model imports."""
    selected = _model_spec(model)
    path = _directory_path(directory)
    for name, expected in selected["files"].items():
        _read_file(path / name, expected)
    _verify_provenance(path, model)
    return {**_provenance(model), "directory": str(path)}


def _download(name, destination, expected, deadline, selected):
    url = f"https://huggingface.co/{selected['repository']}/resolve/{selected['revision']}/{name}?download=true"
    request = urllib.request.Request(url, headers={"User-Agent": "dots-phone"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as source:
            with destination.open("xb") as target:
                total = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise AudioReviewAssetsError(
                            "Audio-review model download exceeded the time limit"
                        )
                    chunk = source.read(min(CHUNK_BYTES, expected["bytes"] - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected["bytes"]:
                        raise AudioReviewAssetsError(
                            "Audio-review model download exceeds the pinned size"
                        )
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        _read_file(destination, expected)
    except (OSError, urllib.error.URLError):
        raise AudioReviewAssetsError("Audio-review model download failed; retry setup") from None


def prepare_assets(directory=None, *, model="small.en"):
    """Download only missing files; refuse corrupt caches instead of replacing them.

    Downloads remain hidden until every missing file has passed its pinned size
    and checksum. Each completed file and the provenance record are then replaced
    atomically. An interrupted download cannot appear as an installed model file.
    """
    selected = _model_spec(model)
    path = _directory_path(default_directory(model) if directory is None else directory)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _directory_path(path)
        _verify_provenance(path, model)
        _verify_notice_target(path)
        missing = []
        for name, expected in selected["files"].items():
            if (path / name).exists() or (path / name).is_symlink():
                _read_file(path / name, expected)
            else:
                missing.append(name)
        deadline = time.monotonic() + PREPARE_TIMEOUT_SECONDS
        with tempfile.TemporaryDirectory(prefix=".download-", dir=path) as temporary:
            staging = Path(temporary)
            for name in missing:
                _download(name, staging / name, selected["files"][name], deadline, selected)
            _directory_path(path)
            for name in missing:
                os.replace(staging / name, path / name)
            notice = staging / NOTICE_NAME
            notice.write_text(_notice(model))
            os.replace(notice, path / NOTICE_NAME)
            provenance = staging / PROVENANCE_NAME
            provenance.write_text(json.dumps(_provenance(model), indent=2) + "\n")
            os.replace(provenance, path / PROVENANCE_NAME)
        return verify_assets(path, model=model)
    except OSError:
        raise AudioReviewAssetsError("Audio-review model directory is not writable") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=tuple(MODELS), default="small.en")
    parser.add_argument("--output", "--directory", dest="directory", type=Path)
    parser.add_argument(
        "--verify", "--verify-only", action="store_true", help="Verify existing files offline"
    )
    parser.add_argument("--json", action="store_true", help="Print the verified model manifest")
    args = parser.parse_args(argv)
    try:
        directory = args.directory if args.directory is not None else default_directory(args.model)
        manifest = (
            verify_assets(directory, model=args.model)
            if args.verify
            else prepare_assets(directory, model=args.model)
        )
    except AudioReviewAssetsError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True) if args.json else manifest["directory"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
