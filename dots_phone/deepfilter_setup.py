"""Explicit installation of verified weights outside the repository; never on a call."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from dots_phone.deepfilter_assets import (
    COMMIT,
    MODEL_BYTES,
    MODEL_NAME,
    MODEL_SHA256,
    MODEL_URL,
    VERSION,
    verify_model,
)

LICENSE_HASHES = {
    "LICENSE-MIT": "24e6bb09c928af8d8e56268082f87413247ce36b39dd5d33add2f9893968065e",
    "LICENSE-APACHE": "1eaee808c5fb6b4e895ba30425285a5cdc5dd25bba2cd230f264c2200c331aec",
}


def retain_provenance(directory):
    for license_name, expected in LICENSE_HASHES.items():
        path = directory / license_name
        if path.exists():
            content = path.read_bytes()
        else:
            url = (
                f"https://raw.githubusercontent.com/Rikorose/DeepFilterNet/{COMMIT}/{license_name}"
            )
            with urllib.request.urlopen(url, timeout=30) as source:
                content = source.read(65536)
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("DeepFilterNet license checksum mismatch")
        if not path.exists():
            path.write_bytes(content)
    (directory / "provenance.json").write_text(
        json.dumps(
            {"version": VERSION, "commit": COMMIT, "url": MODEL_URL, "sha256": MODEL_SHA256},
            indent=2,
        )
        + "\n"
    )


def default_cache():
    if os.getenv("XDG_CACHE_HOME"):
        root = Path(os.environ["XDG_CACHE_HOME"])
    else:
        root = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    return root / "dots-phone" / "deepfilter" / f"v{VERSION}"


def install(directory: Path):
    directory = directory.expanduser().resolve()
    # Explicit paths must honor the same no-assets-in-the-checkout contract.
    for ancestor in (directory, *directory.parents):
        if (ancestor / ".git").exists():
            raise ValueError("Choose a model cache outside a Git checkout")
    directory.mkdir(parents=True, exist_ok=True)
    model = directory / MODEL_NAME
    if model.exists():
        verify_model(model)
        retain_provenance(directory)
        return model
    fd, name = tempfile.mkstemp(prefix=".download-", dir=directory)
    temporary = Path(name)
    try:
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "dots-phone"})
        with os.fdopen(fd, "wb") as target, urllib.request.urlopen(request, timeout=60) as source:
            total = 0
            while chunk := source.read(65536):
                total += len(chunk)
                if total > MODEL_BYTES:
                    raise RuntimeError("Model download exceeds pinned size")
                target.write(chunk)
        verify_model(temporary)
        temporary.replace(model)
        retain_provenance(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=default_cache())
    args = parser.parse_args()
    print(install(args.directory))


if __name__ == "__main__":
    main()
