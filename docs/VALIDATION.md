# Validation and preview limits

Dots Phone is an experimental, self-hosted preview. Automated checks use fictional
records, authored events, local mock servers and offline containers. They check
implementation behavior; they do not establish reliable completion with an
unfamiliar business, accurate hearing in all conditions, or recipient audibility.

## Automated checks

The GitHub Actions workflow runs the Python and JavaScript suites, lint and
formatting, source/history and credential audits, locked dependency checks,
package builds, and the base and Pipecat container suites. Inspect the workflow
for the exact commit being installed; a passing earlier build does not establish
that a later change passed.

Coverage includes task preparation, authorization, budget-derived duration,
reasoning context, source-tool scope, interruption ordering, keypad controls,
unknown or conflicting information, final report review and voice closure.
These are component and protocol tests, not a measured call success rate.

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
- Concurrency, carrier timing and sustained task reliability require further work.

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
