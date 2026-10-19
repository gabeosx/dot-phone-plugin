from __future__ import annotations

import json
import os
from pathlib import Path

from dots_phone.bws import MAPPING

path = Path("/run/secrets/runtime")
if path.exists():
    values = json.loads(path.read_text())
    if set(values) != set(MAPPING.values()):
        raise SystemExit("runtime secret allowlist mismatch")
    os.environ.update(values)
os.environ.pop("BWS_ACCESS_TOKEN", None)

import uvicorn  # noqa: E402

uvicorn.run(
    "dots_phone.server:create_app",
    factory=True,
    host="0.0.0.0",
    port=8000,
    access_log=False,
    proxy_headers=False,
)
