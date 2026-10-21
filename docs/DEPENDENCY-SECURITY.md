# Dependency security review

Reviewed October 20, 2026 for the 0.3.2 launch candidate. The dependency audit
retains every advisory in its JSON artifact. A passing exception review means
the findings below were assessed for this telephone runtime; it does **not**
mean that the installed dependency has no known vulnerabilities.

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

## Temporary Pillow exception

Pipecat 1.12.0 requires `Pillow>=11.1.0,<13`. The
[latest Pipecat 1.13.0 metadata](https://pypi.org/pypi/pipecat-ai/1.13.0/json)
retains that bound. The findings below are fixed in
[Pillow 13.0.0](https://pypi.org/project/pillow/13.0.0/), so upgrading Pillow
within the supported dependency range cannot clear them. The launch candidate
keeps Pipecat's supported dependency range and Pillow 12.3.0.

The exception expires **November 3, 2026 UTC**, when CI fails unless the findings
have been fixed or a new review explicitly changes the policy. Remove it
earlier when Pipecat supports the fixed Pillow release. Any added image, video,
file upload, document conversion, or PDF workflow invalidates this assessment
and requires removal of the exception before that feature ships.

| Advisory | Operation required by the advisory | Telephone reachability assessment |
| --- | --- | --- |
| [GHSA-p4wg-p73j-c528](https://github.com/advisories/GHSA-p4wg-p73j-c528) | Parse an existing PDF while appending images | No PDF parsing or image append workflow |
| [GHSA-65qm-jvmh-6pqg](https://github.com/advisories/GHSA-65qm-jvmh-6pqg) | Seek a GIF, catch a rejected size, then reuse the image | No GIF decoding or animation processing |
| [GHSA-58r4-6rv3-gfh8](https://github.com/advisories/GHSA-58r4-6rv3-gfh8) | Apply morphology to a decoded zero-width image | No image decoding or morphology calls |
| [GHSA-pchr-8j38-3x4p](https://github.com/advisories/GHSA-pchr-8j38-3x4p) | Decode then re-encode a crafted TIFF with its color map | No TIFF decoder or encoder input |
| [GHSA-q45j-58j2-45rg](https://github.com/advisories/GHSA-q45j-58j2-45rg) | Decode a crafted JPEG-compressed TIFF strip | No TIFF decoding |
| [GHSA-462x-pgg6-55fp](https://github.com/advisories/GHSA-462x-pgg6-55fp) | Decode a crafted tiled TIFF | No TIFF decoding |
| [GHSA-c6hw-5grf-ff96](https://github.com/advisories/GHSA-c6hw-5grf-ff96) | Decode malformed compressed TIFF tiles/RGBA data | No TIFF decoding |
| [GHSA-vx6w-vv9f-6gvg](https://github.com/advisories/GHSA-vx6w-vv9f-6gvg) | Render an EPS with Ghostscript at an increased scale | No EPS rendering or Ghostscript workflow |
| [GHSA-8g5g-8vmr-9782 / CVE-2026-112403](https://github.com/python-pillow/Pillow/security/advisories/GHSA-8g5g-8vmr-9782) | Decode a crafted lossless JPEG after `draft()` or `thumbnail()` requests scaled output | No JPEG decoding, thumbnailing or draft operations |

These are reachability conclusions from the application and pinned dependency
source, not a claim that the vulnerable operations are safe:

- `dots_phone/pipecat_voice.py` installs `TwilioFrameSerializer` with audio input
  and output enabled. Its accepted media protocol produces audio and DTMF
  frames; unknown image, video, document and file event types produce no frame.
- The serializer converts `media.payload` from base64 μ-law to PCM. Even bytes
  resembling a file header are treated as audio samples and never passed to an
  image file decoder.
- `dots_phone/server.py` and `dots_phone/ingress.py` expose the telephone stream,
  signed carrier status, OAuth and owner-control paths. They have no image or
  document upload/decoding route. Briefs and context are text/data, not files
  opened with Pillow.
- The pinned Pipecat Realtime SDK imports Pillow for its general video feature.
  Its video helper uses `Image.frombytes(...).save(..., format="JPEG")`; the
  telephone serializer never produces the image frame that calls that helper.
  None of the nine affected PDF/GIF/morphology/TIFF/EPS/JPEG-decoding operations
  are called. Creating a JPEG from raw pixels does not invoke the scaled JPEG
  decoder described by CVE-2026-112403.

`tests/test_voice_media_boundary.py` exercises the pinned serializer with image,
video, document and file event types and file-like audio bytes, with Pillow
image entry points configured to fail if invoked. Existing signed carrier,
codec, MCP supervision and teardown integration tests cover the audio path.

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

On October 20, GitHub and OSV reported the newly published critical
[GHSA-8g5g-8vmr-9782](https://github.com/advisories/GHSA-8g5g-8vmr-9782), while
PyPI's feed still reported only the other eight Pillow findings. This difference
is why a single passing PyPI review is insufficient. The ninth advisory's
affected operation was reviewed directly against the primary advisory; it is
included explicitly rather than treated as covered by an older audit.

`scripts/dependency_exceptions.json` lists only the nine reviewed advisory IDs
for exactly `pillow==12.3.0`. The evaluator rejects other packages, changed
versions, new IDs, expired exceptions, skipped dependencies, missing reports
and audit tool failures. Raw findings remain in the report alongside an explicit
`exception_review` result. There is no blanket vulnerability suppression.

Owner: the release maintainer. Before each release, rerun the live advisory
audit and check upstream Pipecat's Pillow constraint. External installations
that use Pillow for their own image/document processing are outside this
exception and must use a fixed dependency arrangement.
