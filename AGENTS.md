# Project rules

## Validate before every push

Install the repository hook with `bash scripts/install-hooks.sh` in each clone
or worktree before pushing. Run **all** CI locally using `bash scripts/ci.sh`.
GitHub Actions uses that same script. The pre-push hook reruns it against a clean
checkout of the actual commit being pushed and blocks failures, unavailable
prerequisites, dirty source, other branch tips, or source changes during checks.

Never use `--no-verify`, disable the hook, skip a failing check, add a success
override, or push to GitHub to discover failures that can be reproduced locally.
Fix and rerun locally first. Missing Docker access, network access, or advisory
service availability is a blocker, not a pass. Keep GitHub CI as independent
verification; a local pass does not replace its required status checks.

## Dependency maintenance and releases

Follow [the maintenance runbook](docs/DEPENDENCY-MAINTENANCE.md). Update related
pins and both lockfiles together, review upstream release notes and security
constraints, and retain exact pins and image/action digests. Do not remove a
security override or relax a test merely to make an upgrade pass.

Release only after local CI, final-commit GitHub checks, package audits, and all
applicable behavior validation pass. Incomplete validation means an unreleased
candidate with the specific blocker recorded. Keep private data and artifacts
out of Git and release assets. A scheduled maintenance run does not authorize
phone calls, paid model evaluations, production deployment, or credential changes.
