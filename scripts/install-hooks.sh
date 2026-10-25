#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
existing="$(git config --get core.hooksPath || true)"
if [[ -n "$existing" && "$existing" != ".github/hooks" ]]; then
  echo "Refusing to replace existing core.hooksPath=$existing; integrate the pre-push gate first." >&2
  exit 1
fi
if [[ -z "$existing" ]]; then
  for legacy in "$(git rev-parse --git-path hooks)"/*; do
    [[ "$legacy" == *.sample ]] && continue
    if [[ -f "$legacy" ]]; then
      echo "Existing Git hook at $legacy; integrate it before installing this gate." >&2
      exit 1
    fi
  done
fi
chmod +x .github/hooks/pre-push
git config --local core.hooksPath .github/hooks
echo "Installed pre-push gate: every code/tag push must pass bash scripts/ci.sh."
