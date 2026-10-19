"""Opt-in replay media lives outside the source tree; never fetch it during tests."""

import os
import sys
from pathlib import Path

import pytest


def audio_asset(relative):
    configured = os.environ.get("DOTS_PHONE_TEST_ASSETS")
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/DotsPhone/test-assets"
    else:
        root = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "dots-phone/test-assets"
        )
    path = root / "test-fixtures" / relative
    if not path.is_file():
        pytest.skip("External audio fixture unavailable; set DOTS_PHONE_TEST_ASSETS")
    return path
