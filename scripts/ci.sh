#!/usr/bin/env bash
# Validate committed source and its ancestry without unrelated local Git refs.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec uv run --no-project --python 3.13 python scripts/local_ci.py
