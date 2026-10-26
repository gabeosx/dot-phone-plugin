# Weekly dependency maintenance

The weekly Codex job runs Mondays at 09:00 America/New_York, starting October 26,
2026. It checks upstream versions, prepares upgrades, tests locally, and releases
only after validation. The Mac and Codex app must be running for this local job.
Do not add a weekly GitHub Actions workflow: candidate failures should use local
compute, not Actions minutes.

## Discover and prepare

1. Read `AGENTS.md`, this runbook, `DEPENDENCY-SECURITY.md`, `VALIDATION.md` and
   `PUBLIC-RELEASE.md`. Inspect Git status and existing maintenance branches,
   PRs and releases first. Resume an existing candidate rather than duplicating
   it. Leave unrelated work intact; use an isolated checkout for the candidate.
2. Run `uv sync --frozen --extra pipecat --python 3.13`, then
   `uv run --no-sync python scripts/check_dependency_updates.py`. Its ignored
   `artifacts/dependency-updates.json` covers exact runtime, optional, development,
   build, constraint and override pins in both Python projects. It excludes prereleases and
   fully yanked releases and fails if any package lookup is incomplete.
3. Also inspect transitive updates with `uv tree --outdated` for both projects,
   pinned images in all Dockerfiles, GitHub Action SHAs, the tunnel-client
   version/checksum and vendored upstream revisions. Check official release notes,
   migration guidance and security advisories. A failed lookup is not evidence
   that dependencies are current. Report findings without compatible updates.
4. Prioritize Pipecat and security fixes. Keep the root `pipecat` extra and the
   experimental project's Pipecat pin aligned. Review the Pillow override against
   the new Pipecat metadata; remove it only when a secure supported resolution and
   compatibility checks pass. Do not restore or widen vulnerability exceptions.
5. Use `codex/dependency-maintenance-YYYY-MM-DD`, update exact constraints and
   regenerate both locks with `uv lock` and `uv lock --project experiments/pipecat`.
   Upgrade selected transitive packages with `--upgrade-package NAME`; avoid an
   unexplained blanket lock refresh. Verify supported Python versions and extras.
   Preserve immutable image/action pins and verify new checksums from upstream.
   Make necessary compatibility changes with regression coverage. Keep incompatible
   upgrades separate and explain blockers; do not silently omit them.

## Validate before pushing

Install the hook with `bash scripts/install-hooks.sh`. Commit candidate changes
before running `bash scripts/ci.sh`: this is the complete CI suite including all
four live advisory audits, credential
and source/history audits, build, JavaScript checks and both Linux amd64 container
suites. It uses the same pinned uv as Actions. Docker, network/advisory access,
Node, and uv are prerequisites. The runner fetches the candidate commit and its
complete ancestry into a temporary checkout, without unrelated private backup
refs or ignored local state. Every existing history/security check runs there.
Reports are copied to ignored `artifacts/local-ci/<commit>-<run>/` before cleanup.
Docker runs use separate Compose project names and do not start phone services.
GitHub runs the same suite for pull requests and pushes to main, avoiding a
second identical feature-branch push run. The local hook still gates every push.

Review upstream changes for behavioral risk. Voice SDK, transport, codec, model
and audio processing upgrades need the applicable synthetic voice/installation
validation in `VALIDATION.md` as well as offline CI. Do not count skipped optional
tests as completed acoustic validation. Reuse evidence only for the exact candidate
and applicable behavior. If required validation needs additional authorization or
unavailable assets, prepare a candidate and record the blocker; do not publish.

Commit the finished candidate, including an appropriate project version bump and
release notes, then push through the hook. Version-bearing files currently include
`pyproject.toml`, `dots_phone/server.py`, `plugins/dots-phone/plugin.json`, the
bundled skill metadata and related version assertions. Regenerate the root lock
after a project version change. Use a patch release for compatible dependency
maintenance; use a minor release when the resulting public behavior requires it.
Treat breaking changes as a separate review decision. Do not bump a release when
there are no changes.

Push only after every local check passes on the clean committed source. The hook
validates HEAD again, including annotated tags pointing to HEAD; it rejects other
tips in a multi-ref push. Never bypass it or use GitHub to debug local failures.
Open/update one PR with old/new versions, upstream links, compatibility changes,
validation and remaining limits. Attach the PR to the maintenance chat. Independent
GitHub checks must pass before merge. Fix failures locally before the next push.

## Release after validation

For a compatible, fully validated maintenance update, merge only after required
GitHub checks pass. Check out the final merge commit, install the hook, rerun full
local CI and verify the GitHub checks for that final commit. A tested PR tip does
not establish that its merge commit passed.

Follow `PUBLIC-RELEASE.md`: build the final wheel/sdist and existing plugin bundle
format, audit the distributions with `scripts/public_release_audit.py
--distribution ...`, review the public contents and licenses, and create checksums.
Publish the next unused `vX.Y.Z` GitHub release from that exact validated commit,
with audited assets and concise release notes. Check the published tag, download
links and checksums. Never overwrite an existing tag/release or upload raw local
artifacts. Do not deploy to production or update installed plugin snapshots as part
of this job. If any gate fails, leave a clearly described candidate pending
validation, with no published release.

Stay quiet when no actionable change exists. Notify on a validated release,
ready candidate, new meaningful blocker or required decision, with concise evidence.
