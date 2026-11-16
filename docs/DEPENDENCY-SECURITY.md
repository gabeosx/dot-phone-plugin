# Dependency security review

Reviewed October 22, 2026 for the 0.3.3 launch candidate. The dependency audit
retains every advisory in its JSON artifact. The supported frozen environments
use fixed Pillow 13.0.0 with no vulnerability exceptions. Audit results describe
the advisory feeds at review time and do not promise that dependencies will
remain free of future findings.

## November 16 candidate review

The root environment upgrades Authlib to 1.9.8. The 1.9.6–1.9.8 releases
reject unsupported key-bound ID-JAG grants, cap JWE recipient lists at 16,
reject unauthenticated ECDH-1PU encryption modes, and refuse OAuth 1.0 signatures
from clients without a client secret. The JWE regression accepts a valid
single-recipient message and rejects 17 independent recipients. It fails against
the prior Authlib 1.9.5.
[Authlib changelog](https://docs.authlib.org/en/stable/upgrades/changelog.html#changelog-1-9-8)

Both locks upgrade opentelemetry-api from 1.45.1 to 1.45.2 for
GHSA-mf5v-w3w3-f6cg. A crafted traceparent inside JSON metadata can reach a
quadratic regex even when ordinary HTTP headers reject embedded newlines.
The regression requires oversized metadata to be rejected before regex matching
and retains a valid remote-context case. It fails on the previous library.
[OpenTelemetry advisory](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-mf5v-w3w3-f6cg)

The tunnel runtime source advances to checksum-verified 0.0.21 and Go 1.27.4.
The official full-client report retains 15 findings per Linux binary. Its
runtime report retains four, including GHSA-q7gw-24fr-5p69 in OpenTelemetry
SDK and trace. The prior patched Dots Phone locks used 1.45.0 and are also
within this new advisory's affected range. All linked OpenTelemetry core modules
now resolve to 1.47.0, the advisory's first patched version. Preserve upstream
findings and verify the actual rebuilt binaries for both Linux architectures.
[Upstream report](https://github.com/openai/tunnel-client/releases/download/v0.0.21/tunnel-client-v0.0.21-vulnerability-report.json)
[OpenTelemetry Go advisory](https://github.com/advisories/GHSA-q7gw-24fr-5p69)

CI and the uv images pin 0.13.8. Python remains 3.13.18 with refreshed verified
image digests. Current Action versions, Gitleaks 8.30.1 and DeepFilterNet 0.5.6
are retained. The Agent Plugins 1.0.0 schemas still match upstream byte for byte.
The vendored Cookbook directory changed only its standalone lockfile.

Pipecat 1.16.0 was tested in both projects and is deferred. It changes the default
stream resampler and removes the core SOXR dependency. Even with explicit SOXR
input selection, five existing media probes detected extra carrier output
samples after completion or interruption. The tests retain their exact waveform
and duration requirements. Both frozen projects remain on 1.15.0 with Pillow
13.0.0. A coordinated output-resampler migration and acoustic validation are
required before adopting 1.16.0.
[Pipecat release](https://github.com/pipecat-ai/pipecat/releases/tag/v1.16.0)

Other direct updates remain separate coherent upgrades. The optional Rust
builder migration to 1.100.0 still requires native ABI and asset validation.
The release also awaits completion of the six synthetic voice scenarios. The
previous Realtime credential expired, and the user-authorized replacement setup
still needs its secure target selection and local destination confirmation.

## Patched tunnel runtime

The connection image builds the official 0.0.21 runtime source export from
commit `714f2d67844b660af25754f1d19b0360b346b13b`. The archive digest and Go
compiler image digest are pinned in the Dockerfile. Dots Phone checks in its
reviewed Go module locks and compiles with network access disabled after module
verification. The runtime uses the existing static-key Compose flags.

The upstream full client findings below remain part of the record. The narrow
runtime drops the full client's affected compress, go-jose, chi, and x/crypto
modules. Its upstream runtime report retains OpenTelemetry findings. The new locks
resolve x/net 0.62.0 and OpenTelemetry core modules and SDK 1.47.0. Both final
Linux image binaries passed govulncheck 1.9.0.
The image retains the upstream notices, every linked module's notices, and
Go build metadata. See the [upstream report](https://github.com/openai/tunnel-client/releases/download/v0.0.21/tunnel-client-v0.0.21-vulnerability-report.json).

For a release, build both Linux architectures, extract the actual image
binaries, and scan each with `govulncheck -mode=binary`. Preserve the results
with each binary's SHA-256 and module metadata. The shared CI also builds the
tunnel image and checks its Compose flags with networking disabled.

## November 9 candidate review

The root lock now uses Authlib 1.9.5 and joserfc 1.8.0. Authlib 1.9.5
rejects OpenSSH public keys containing a tab separator as HMAC secrets. A
regression reproduces acceptance on 1.9.2 and requires rejection after the
upgrade. The 1.9.3 and 1.9.4 releases also contain OAuth and identity assertion
fixes. joserfc 1.8.0 tightens public-key import, authenticated data, and JWT
claim validation, and supports Python 3.11 and newer. The experimental project
resolves neither dependency. See the
[Authlib changelog](https://docs.authlib.org/en/stable/upgrades/changelog.html#changelog-1-9-5)
and [joserfc 1.8.0 release](https://github.com/authlib/joserfc/releases/tag/1.8.0).

The candidate updates the shared CI and digest-pinned uv image to 0.13.7.
Both projects retain Pipecat 1.15.0 and the Pillow 13.0.0 constraint.
The Agent Plugins upstream changes are confined to draft 1.1.0 schemas and
specification. The vendored 1.0.0 files retain their original provenance.
The Cookbook changes in the vendored harness directory affect its standalone
lockfile. Dots Phone installs its own frozen dependencies for that code.

The checksum-verified
[tunnel-client 0.0.20 report](https://github.com/openai/tunnel-client/releases/download/v0.0.20/tunnel-client-v0.0.20-vulnerability-report.json)
contains 13 matches per Linux client binary, including five high-severity
matches. The earlier findings persist, and GO-2026-6865 adds another match for
klauspost/compress 1.18.0. The
[upstream VEX](https://github.com/openai/tunnel-client/releases/download/v0.0.20/tunnel-client-v0.0.20.openvex.json)
classifies these findings as under investigation. Keep 0.0.19 in the draft
and preserve its release blocker until a patched binary or documented
reachability and fix validation clears the findings.

GitHub Actions run 41465146488 completed the shared CI step successfully.
GitHub marked the job failed after the hosted runner lost communication
during cleanup. Independent GitHub acceptance remains required for the
updated candidate.

## November 2 candidate review

Both frozen projects now pin [Pipecat 1.15.0](https://github.com/pipecat-ai/pipecat/releases/tag/v1.15.0),
which still declares support for the existing exact Pillow 13.0.0 constraint.
The targeted lock update changes only Pipecat. Python requirements remain
compatible with the application's Python 3.12 minimum and the experimental
project's Python 3.12–3.13 range.

The refreshed local audit blocked nine findings in Authlib 1.8.0 and joserfc 1.7.5.
The root environment now pins [Authlib 1.9.2](https://docs.authlib.org/en/stable/upgrades/changelog.html#changelog-1-9-2)
and locks [joserfc 1.7.7](https://github.com/authlib/joserfc/releases/tag/1.7.7),
which includes the [compact-JWS size guard fix](https://github.com/advisories/GHSA-9pg5-8c7x-5hfx).
No exceptions were added. The experimental project does not resolve either
package. Three regression cases reproduce acceptance of an empty signature list
and splitting oversized attacker-controlled input before checking its limit on
the old versions, then require rejection on the patched versions. The application's
existing opaque-token OAuth/PKCE flow remains covered by its tests and installation
check; no grant, credential or registration policy is relaxed for the update.

Pipecat's new default requires all configured turn-stop strategies to agree.
The application's manual Realtime path uses the SDK's single external stop
strategy, not a combined strategy list. Keep the existing committed-turn,
interruption, signed-carrier and exactly-once tool-result checks; optional alpha
expression/perception features and new provider defaults are not enabled by this
upgrade. Applicable model-backed acoustic validation remains a release gate.

All Python image stages and the managed native CI interpreter now pin
[Python 3.13.18](https://www.python.org/downloads/release/python-31318/).
This expedited security release blocks compromised eMudhra intermediate keys in
the ssl module and fixes Expat and tarfile issues. uv 0.13.6 supplies the new
managed interpreter; registry digests are verified and CI uses the same uv pin.
The certificate-store fix from uv 0.13.4 remains required. The host uv was already
updated to 0.13.5 before this maintenance run; no host software was changed here.

The connection image now pins the official tunnel-client 0.0.19 binary and its
verified checksum manifest. The application uses HTTP MCP; the release's new
stdio-child recovery policy is not configured here. No tunnel is started during
offline build/CLI checks, and credentials and deployment configuration are unchanged.

**Release blocker:** the checksum-verified upstream
[vulnerability report](https://github.com/openai/tunnel-client/releases/download/v0.0.19/tunnel-client-v0.0.19-vulnerability-report.json)
contains 12 dependency matches for each Linux client binary, including two high
severity matches: `github.com/klauspost/compress` 1.18.0
([GHSA-259r-337f-4rfw](https://github.com/advisories/GHSA-259r-337f-4rfw), patched in 1.18.7)
and `github.com/go-jose/go-jose/v4` 4.1.4
([GHSA-mgcc-cww5-fp2q](https://github.com/advisories/GHSA-mgcc-cww5-fp2q), patched in 4.1.6).
The matching upstream [VEX statements](https://github.com/openai/tunnel-client/releases/download/v0.0.19/tunnel-client-v0.0.19.openvex.json)
are all `under_investigation`; they do not establish non-impact or remediation.
Preserve every finding. Do not merge or publish this candidate until a patched
upstream binary or documented reachability/fix validation clears the findings.
The four Python audits do not cover the Go binary's dependencies. Offline CLI
compatibility and verified checksums do not clear this security gate.

## October 26 candidate review

The 0.3.4 candidate moves both projects to
[Pipecat 1.14.0](https://github.com/pipecat-ai/pipecat/releases/tag/v1.14.0).
Its declared `Pillow>=11.1.0,<14` range supports the existing fixed Pillow 13.0.0.
Both projects now use an exact uv constraint instead of overriding Pipecat's
requirements. Regression checks still reject vulnerable Pillow/filelock locks,
require the Pillow constraint, and forbid restoring the override. Historical
sections below describe the earlier 1.12 installation boundary.

The candidate also fixes the build tool's own trust store, beyond the previously
updated Python certifi package. [GHSA-hq85-5cpj-mr62](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62)
affects uv versions below 0.13.4. CI and all uv container stages pin 0.13.5, and
the Python stages pin 3.13.17. Their registry digests were verified before editing;
native CI requests the same Python patch version. Hosts should also use uv
0.13.4 or newer; a project image pin does not upgrade a host's installed uv.

The tunnel binary moves from 0.0.16 to the official 0.0.18 release. The downloader
checks the release's pinned SHA256SUMS manifest digest before accepting any
platform archive, retains license/SPDX files, and does not change credentials.
All four frozen-requirements audits initially passed with zero findings and zero
exceptions. This does not replace final-commit CI or required behavior validation.

## October 25 maintenance check

The new local push gate found three advisories affecting the transitive
`filelock==4.0.12` pin. Both lockfiles now use the upstream patched
[filelock 4.2.1](https://github.com/tox-dev/filelock/releases/tag/4.2.1), resolving
[GHSA-v4x8-3wr5-3wq8](https://github.com/advisories/GHSA-v4x8-3wr5-3wq8),
[GHSA-9vhv-jxww-xfm5](https://github.com/advisories/GHSA-9vhv-jxww-xfm5) and
[GHSA-567q-5qvw-pf92](https://github.com/advisories/GHSA-567q-5qvw-pf92).
This is a targeted transitive update; no vulnerability exceptions were added.
All four production/experimental PyPI and OSV audits passed with zero findings
on October 25. Regressions guard the patched baseline in both supported projects. Release
remains conditional on all local and final-commit GitHub checks passing.

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
