# Dependency security review

Reviewed October 22, 2026 for the 0.3.3 launch candidate. The dependency audit
retains every advisory in its JSON artifact. The supported frozen environments
use fixed Pillow 13.0.0 with no vulnerability exceptions. Audit results describe
the advisory feeds at review time and do not promise that dependencies will
remain free of future findings.

## Certificate trust update

Both lockfiles upgrade certifi from 2026.7.22 to 2026.10.19, resolving
[PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2).
Rebuild containers from the updated lockfile before release.

## Experimental dependency reduction

The experimental lockfile previously enabled Pipecat's `cli` extra, which pulled
in `pipecat-ai-context-hub` and ChromaDB. The scripts use Pipecat's core evaluation,
pipeline, transport and worker modules; they do not invoke that CLI or its
context-hub database. Removing the unused extra removes ChromaDB and its dependency
chain rather than suppressing its four reported advisories:
[GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf),
[GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr),
[GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) and
[GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c).
Pipecat remains pinned at 1.12.0. Offline protocol tests run with the reduced
environment to check that the core evaluation harness still works.

## Fixed Pillow dependency

The October 22, 2026 release review found 24 Pillow 12.3.0 advisories in the
PyPI feed, including new findings that the previous exception did not cover.
The fixed release is [Pillow 13.0.0](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).
Both production and experimental lockfiles now use it, and
`scripts/dependency_exceptions.json` is empty. The previous November 3 exception
has been removed rather than expanded.

[Pipecat 1.12.0](https://pypi.org/pypi/pipecat-ai/1.12.0/json) and
[1.13.0](https://pypi.org/pypi/pipecat-ai/1.13.0/json) declare
`Pillow>=11.1.0,<13`. Updating Pipecat alone therefore keeps the vulnerable
release. Each project's `[tool.uv]` section explicitly overrides that dependency
to `pillow==13.0.0`. Pipecat remains pinned at 1.12.0 to avoid unrelated voice
runtime changes. Remove the override when a validated Pipecat release accepts
the fixed Pillow version, keeping both lockfiles on a fixed release.

Use the documented frozen `uv` setup or the supplied Docker builds. They honor
the override and the locked package hashes. A plain `pip install` of a wheel
with the `pipecat` extra does **not** apply a project's uv overrides and can
resolve Pipecat's older Pillow range; that is not the supported voice
installation. Do not substitute it for the quickstart commands.

Compatibility review covers the application's audio-only telephone path and
the Pipecat APIs it imports. Pillow 13 removes Python 3.10 support; Dots Phone
requires Python 3.12 or newer and its documented runtime is Python 3.13. The
Pipecat modules used by this path do not use the removed Pillow APIs. Importing
the Realtime service, LLM context and Twilio serializer with Pillow 13 succeeds;
its `Image.frombytes`, JPEG `save` and `resize` operations remain available.
This is application compatibility evidence, not a claim of upstream Pipecat
support for Pillow 13 or validation of every Pipecat image/video feature.

`tests/test_voice_media_boundary.py` continues to reject image, video, document
and file event types at the Twilio serializer while preserving PCM audio. Its
Pillow entry points fail the test if telephone input invokes an image decoder.
The signed carrier, codec, MCP supervision and teardown integration tests cover
the audio path without making provider calls. Lockfile regression checks also
ensure both supported projects keep a fixed Pillow release and no vulnerability
exception is restored silently.

## Audit enforcement

CI exports all locked production extras and all locked experimental extras with
hashes, then audits each export against **both PyPI and OSV**:

```sh
uv run --no-sync python scripts/dependency_audit.py \
  --requirements artifacts/dependency-requirements.txt \
  --report artifacts/dependency-audit.json
uv run --no-sync python scripts/dependency_audit.py \
  --vulnerability-service osv \
  --requirements artifacts/dependency-requirements.txt \
  --report artifacts/dependency-audit-osv.json
```

The same two commands run against `artifacts/experimental-dependency-requirements.txt`,
with separate `experimental-dependency-audit*.json` reports. With `--disable-pip`,
both audits require pinned requirements with hashes but do not verify the hash
values or download the package artifacts. The separate frozen `uv` installation
checks artifact integrity against the lockfile. The two feeds provide advisory
coverage; all four reports retain their findings and identify the feed used.

The two feeds can publish findings at different times, so a passing result
from one feed is insufficient. Every production and experimental audit must
complete successfully. The evaluator fails new findings, skipped dependencies,
missing or incomplete reports and audit tool failures. Reports retain the raw
findings and the explicit `exception_review` result; no advisory IDs are
suppressed by the current policy.

Owner: the release maintainer. Before each release, rerun all four live advisory
audits and check Pipecat's Pillow constraint. Rebuild existing environments and
containers from these lockfiles before relying on the dependency update.
