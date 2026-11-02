#!/usr/bin/env bash
# The complete acceptance suite, shared by local pushes and GitHub Actions.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

for tool in uv node docker; do
  command -v "$tool" >/dev/null || { echo "Missing CI prerequisite: $tool" >&2; exit 1; }
done
docker info >/dev/null
docker compose version >/dev/null
mkdir -p artifacts

# Match setup-uv in CI even when the developer has a newer uv installed.
if [[ "$(uv --version)" == "uv 0.13.6"* ]]; then
  UV=(uv)
else
  UV=(uvx --from uv==0.13.6 uv)
fi
# GitHub's ubuntu-latest runner is x86_64. Exercise the same image platform on Macs.
export DOCKER_DEFAULT_PLATFORM=linux/amd64
# Never reuse a running phone deployment's Compose project or service containers.
project="dots-ci-$$"
export DOTS_PHONE_CI_PROJECT="$project"
cleanup() {
  docker compose -p "${project}-base" -f compose.yaml -f scripts/compose.ci-base.yaml down --remove-orphans >/dev/null 2>&1 || true
  docker compose -p "${project}-pipecat" -f experiments/pipecat/compose.phone.yaml -f scripts/compose.ci-pipecat.yaml down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Linked worktrees store their Git metadata outside the checkout. Preserve that
# absolute path inside the scanner so its .git pointer remains usable.
git_common="$(git rev-parse --path-format=absolute --git-common-dir)"

"${UV[@]}" sync --frozen --extra pipecat --python 3.13.18
"${UV[@]}" run --no-sync python -m dots_phone.checks offline
"${UV[@]}" run --no-sync python scripts/secret_scan.py --history
"${UV[@]}" run --no-sync python scripts/public_release_audit.py --history --report artifacts/public-source-audit.json
node --test experiments/pipecat/tests/*.test.mjs experiments/pipecat/test_automatic_call_worker.mjs
"${UV[@]}" export --frozen --no-dev --all-extras --no-emit-project --format requirements-txt --output-file artifacts/dependency-requirements.txt
"${UV[@]}" run --no-sync python scripts/dependency_audit.py --requirements artifacts/dependency-requirements.txt --report artifacts/dependency-audit.json
"${UV[@]}" run --no-sync python scripts/dependency_audit.py --vulnerability-service osv --requirements artifacts/dependency-requirements.txt --report artifacts/dependency-audit-osv.json
"${UV[@]}" export --project experiments/pipecat --frozen --no-dev --all-extras --no-emit-project --format requirements-txt --output-file artifacts/experimental-dependency-requirements.txt
"${UV[@]}" run --no-sync python scripts/dependency_audit.py --requirements artifacts/experimental-dependency-requirements.txt --report artifacts/experimental-dependency-audit.json
"${UV[@]}" run --no-sync python scripts/dependency_audit.py --vulnerability-service osv --requirements artifacts/experimental-dependency-requirements.txt --report artifacts/experimental-dependency-audit-osv.json
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/repo:ro" -v "$git_common:$git_common:ro" -v "$PWD/artifacts:/report" ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f git /repo --redact --report-format json --report-path /report/gitleaks.json --no-banner
"${UV[@]}" build
docker compose -p "${project}-base" -f compose.yaml -f scripts/compose.ci-base.yaml --profile test run --build --rm --user "$(id -u):$(id -g)" test-runner
docker compose -p "${project}-pipecat" -f experiments/pipecat/compose.phone.yaml -f scripts/compose.ci-pipecat.yaml run --build --rm --no-deps -T tests
echo "All local CI checks passed."
