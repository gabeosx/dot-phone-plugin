# Validation

Validation includes offline suites, an isolated installation, and fictional
conversations through the actual Realtime voice model and signed-in Codex worker.
The website walkthrough is a separate, scripted illustration.

## Voice and reasoning checks

The acoustic checks use newly authored synthetic speech with known expected
answers, the shared production voice factory, actual keypad tool execution,
playback interruption, and the production review of the closed call. Independent
local recognition checks captured audio, including spoken identifiers with
leading zeros. No people are called and no connected mailbox is accessed.

| Scenario | Evidence from the October 20 launch preparation |
| --- | --- |
| Supplied verification details and requested answer | Completed with supported final evidence; keypad input and spoken verification checked. |
| Missing verification details | Asked for an alternative, then reported the unresolved requirement without inventing a member number. |
| Wrong number | Declined the unrelated appointment question, withheld identifiers, and honored the request to end. |
| Spoken correction during playback | Interrupted queued speech and retained the corrected amount in the reviewed result. |
| Hold | Stayed silent during the hold, resumed, and returned the supported answer. The original digit-omission failure passes with explicit `gpt-transcribe` input transcription and the SDK compatibility fix, with the result-review rules unchanged. |
| Telephone audio encoding | Completed through the production 8 kHz mu-law conversion. The original digit-omission failure passes with the replacement transcription model and unchanged review rules. |

Model sessions rejected before audio, empty generated fixtures, and harness
adapter failures are retained as test failures rather than counted as completed
behavioral checks. Results and generated audio remain outside release archives.
These acoustic checks isolate voice and reasoning from carrier transport.

The hold and telephone regressions verify the provider-acknowledged transcription
model and pass without the optional local audio-review models. Local recognition
remains a separate, optional source of evidence rather than a requirement for
these results.

## Installation

An isolated Compose installation with invented credentials and an empty database
reached authenticated phone tools through OAuth with PKCE and returned the
expected workflow version, with zero calls. This checks installation mechanics;
custom plugin registration still depends on the installer's ChatGPT access.

## Automated checks

The GitHub Actions workflow runs the Python and JavaScript suites, lint and
formatting, source/history and credential audits, locked dependency checks,
package builds, and the base and Pipecat container suites. Inspect the workflow
for the exact commit being installed; a passing earlier build does not establish
that a later change passed.

Coverage includes task preparation, authorization, budget-derived duration,
reasoning context, source-tool scope, interruption ordering, keypad controls,
unknown or conflicting information, final report review and voice closure.
The release workflow checks the exact published commit.

Optional synthetic-audio tests skip when their externally stored assets are
absent. Native DeepFilter checks require its separately provisioned library and
read-only weights. No personal recordings or private-call fixtures are fetched
by the tests. A scripted model event does not exercise a provider's acoustic
classifier. Transcripts and completed generation do not prove audible delivery.

## Limits

- Unfamiliar IVRs can choose unexpected paths or misinterpret speech.
- Noise, accents, short answers, speaker changes and interruptions need acoustic
  evaluation in the intended deployment.
- Connected sources require the installer's own authenticated tools and policy.
- Duration enforcement uses a configured cost estimate, not a live billing meter.
- Carrier timing and sustained task reliability need evaluation in the intended
  deployment. The acoustic checks above use a local simulated recipient.

Publish only authored synthetic fixtures and reproducible test harnesses. Keep
actual-call contents, summaries, excerpts, timings, identifiers and derived replay
fixtures outside the repository and distribution archives. See [privacy](PRIVACY.md).

## Reproduce offline checks

```sh
uv sync --frozen --extra pipecat --python 3.13
uv run --no-sync python -m dots_phone.checks offline
uv run --no-sync python scripts/secret_scan.py --history
uv run --no-sync python scripts/public_release_audit.py --history --report artifacts/public-source-audit.json
node --test experiments/pipecat/tests/*.test.mjs experiments/pipecat/test_automatic_call_worker.mjs
uv build
```

These commands do not authorize outbound calls or paid model evaluations.
