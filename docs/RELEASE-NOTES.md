# Dots Phone v0.3.3

Experimental, self-hosted preview.

## Changes since v0.3.2

- Human-answer openings stay focused on the approved purpose and who the caller
  assists, without a routine unsolicited AI introduction. Identity answers remain
  truthful, and explicit call-specific disclosure instructions remain binding.
  IVR navigation stays listen-first and answers only the menu or question asked.
- Added regression coverage across general, task, autonomous and source-enabled
  routes, including quiet IVR startup and explicitly requested disclosures.
- Added an independent-project notice and [usage responsibilities](USAGE.md),
  covering provider costs, outcome review, calling/recording permissions and the
  existing MIT warranty terms.
- Updated the README and launch guide for the approved actual-call demo. The
  fictional order and test recipient are disclosed; the demonstration does not
  claim a Gmail or mid-call source lookup.

Existing installations must rebuild the service and refresh their private plugin
snapshot. Follow the [upgrade procedure](LOCAL-SETUP.md#upgrade-an-existing-installation)
and preserve credentials, budgets, source permissions and filter choices.

# Dots Phone v0.3.2

Experimental, self-hosted preview. This candidate prepares the installation and
support experience for technical early adopters.

## Changes since v0.3.1

- Committed-turn transcription is explicitly configured and defaults to
  `gpt-transcribe` for task and general sessions. The previous
  `gpt-realtime-whisper` model remains an explicit rollback. Recognition quality
  and numeric interpretation still require audio evidence.
- Call admission now waits for outstanding final review and voice closure before
  another call can use the shared worker. An earlier call's review can no longer
  retire a newer call's reasoning session. Restart recovery marks an interrupted
  closure-wait review unverified instead of inventing closure or leaving the queue
  blocked indefinitely.
- Setup now identifies custom registration and tunnel prerequisites before paid
  account setup, and separates credential-free offline checks from sign-in.
- Added no-call troubleshooting, private vulnerability reporting guidance, and
  explicit upgrade, backup, restart and rollback procedures.
- Privacy documentation explains provider data flow, worker exchange files,
  default transcript/audio retention and the limits of local deletion.
- Added `setup check`, a read-only actionable checklist for exact Compose
  configuration, private credential formats, registration settings, host worker,
  verified filter assets, local health and running version. It verifies that the
  running app's image, environment and mounts actually match the selected config
  and that the tunnel container is running. It suppresses private values and paths
  and states the external behavior it cannot establish.
- Added opt-in checksum-verified local recipient-audio model preparation and a
  read-only Compose overlay. It stays off by default, requires retained recording
  data and does not guarantee recognition accuracy or resolution of conflicts.
- Updated locked `certifi` from 2026.7.22 to 2026.10.19. See
  [dependency security](DEPENDENCY-SECURITY.md) for eight reviewed temporary
  Pillow advisory exceptions required by Pipecat's current dependency constraint.
  The exceptions expire on November 3, 2026; they do not mean the advisories are fixed.
- Source and plugin metadata identify this candidate independently of v0.3.1;
  existing installations must rebuild and refresh their private plugin snapshot.

## Existing preview capabilities

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
[the filter guide](DEEPFILTER.md). The v0.3.2 bare runtime defaults off until its
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
