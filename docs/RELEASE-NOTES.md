# Dots Phone v0.3.1

Experimental, self-hosted preview. This release improves local audio filtering,
conversational recovery and consistency between the Dots, Realtime and Codex paths.

## Changes

- Optional local DeepFilterNet3 preprocessing handles arbitrary incoming voices
  without enrollment. Models and replay media stay outside the checkout; weights
  are checksum-verified and mounted read-only. An explicit off switch restores
  unfiltered input. Worker failures fall back to delay-matched raw audio.
- Realtime and Codex share conversational guidance, natural brief replies and
  pending-lookup behavior. Missing facts remain unknown while the caller can
  clarify, decline an optional question or pursue an authorized alternative.
- Call context survives longer conversations and eligible transport reconnections.
  Recovery, source lookups, keypad navigation and corrected reports no longer fail
  solely because of the removed lifetime request/attempt counters.
- Reasoning effort and timeouts follow configured policy across production paths.
  Per-call and rolling budget enforcement is shared rather than overridden by a
  second hardcoded worker budget.
- When no user duration cap is supplied, preparation derives the longest duration
  fitting the call budget and installation ceiling. For example, $2 permits
  379 seconds under the current reservation formula. Explicit user time limits
  remain binding. This is estimate-based enforcement, not a live billing meter.
- Canonical preparation, selected connected read-tool checks, scoped keypad
  controls, result review and confirmed voice closure remain part of the workflow.
- Optional IVR-only tests can end silently on a likely human. This is a
  probabilistic test control, not guaranteed human detection.

## Installation and rollback

Follow [the quickstart](QUICKSTART.md) and [local setup](LOCAL-SETUP.md). Installation
starts in dry-run mode. Live calls need the installer's own provider accounts,
credentials and authorization. Connected mail sources are optional.

Current setup guidance recommends enabling DeepFilterNet using
[the filter guide](DEEPFILTER.md). The v0.3.1 bare runtime defaults off until its
native image and external weights are provisioned. Its library is built into the
filter image, while model weights remain external.
Set `DOTS_PHONE_PIPECAT_INPUT_FILTER=off` and restart after active calls finish to
disable it. The ordinary image also runs without the optional library or weights.

## Validation and limits

See [validation and limits](VALIDATION.md) for automated coverage and known limits.
The release contains reusable test code and authored fictional fixtures. Actual-call
contents, summaries, transcripts, recordings, call-specific metadata and derived
replay fixtures are excluded from both source and distribution archives.

Broad unfamiliar-IVR reliability, accurate numeric interpretation and live cost
reconciliation remain limitations. No phone calls or paid model tests are part of
these release checks.
