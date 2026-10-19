# Operations

Installation is local; see [local setup](LOCAL-SETUP.md). The machine must remain
awake, connected and running its containers/tunnels for active calls.

Recommended local caller-audio preprocessing is described in the
[DeepFilterNet guide](DEEPFILTER.md). Normal setup enables it after native-image
and external-weight checks; the bare runtime stays off until provisioned. It runs
before native VAD. A native failure switches the current call to delay-matched raw
audio; `DOTS_PHONE_PIPECAT_INPUT_FILTER=off` removes the adapter entirely.

The [IVR-only test mode](IVR-ONLY-TEST-MODE.md) can silently stop at a likely human.

## Requests and supervision

New local setups use `autonomous_local_reasoner`: Dots prepares the scope and
starts once, then the signed-in Codex call agent owns live reasoning. Realtime
handles communication and keypad tools; Codex handles planning, ambiguous
verification, authorized connected read tools and semantic review of completed
reports. The frozen scope and server controls still govern all actions. New calls use
`direct_with_codex`: Realtime replies directly from prepared/current evidence, handles
ordinary confirmations and validated local keypad controls, and waits silently through
announcements. It consults Codex for missing facts or live plugin lookups, ambiguous
evidence, nontrivial decisions and failed routing. It must consult before declaring
task-relevant information unavailable. Once the conversation is finished,
`finish_call` saves the proposed report and closes the carrier immediately.
Independent review starts only after confirmed voice closure. It can correct a
claim's citation using observed evidence, but cannot change the original claim's
amount, date, unit, status or polarity. Material disagreement stays unresolved;
the reviewer cannot reopen the call or request another phone action. Each claim
is reviewed independently, so an unsupported diagnostic claim does not erase a
supported balance. Completion still requires every approved required criterion.
Use business outcomes for preparation criteria; usage and conversation diagnostics
are separate runtime observations, not recipient claims.

`get_call_result.report_review_status` is `pending`/`running` while verification is
unfinished, `reviewed` after a valid review, or `unverified` on failure. Pending
proposals are never reported as confirmed facts. Review attempts are durable and
bounded to one; an interrupted attempt remains unverified after restart. A failed
review never redials or keeps the line open. New calls queue before dialing while
the single Codex worker finishes a prior closed-call review. Legacy supervised
runtimes retain their existing live report protocol.
`DOTS_PHONE_LOCAL_REASONER_RESPONSE_POLICY=always_consult` retains the previous
tool-only first response and suppresses speech until that turn's consultation.
Saved calls lacking a policy retain that behavior. Voice effort is independent:
`DOTS_PHONE_LOCAL_REASONER_VOICE_EFFORT=medium` controls every Realtime call path,
including general inquiries and hotel calls. Call type and consultation policy
do not override it. Codex uses its separate `DOTS_PHONE_LOCAL_REASONER_EFFORT`.
Unknowns
stay unknown when tools cannot resolve them; synchronous owner questions are not
required to keep the call running. Automated self-service is preferred until it
cannot advance the goal, the owner explicitly requests a person, or the recipient
requires human handling.

Run `node experiments/pipecat/codex_call_reasoner.mjs serve /your/private/reasoner`
on the signed-in host and share that owner-only directory with the phone service
using `DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY`. The runtime checks a fresh worker
heartbeat before dialing, then prepares the per-call thread with a bounded model
turn before contacting the carrier. Failed, cancelled or stale preparation prevents
dialing. Restart older host workers after upgrading; they must advertise compact-decision
prewarming and source-selection support (`decision_protocol: 1` and
`source_selection_protocol: 1`) and `post_call_review_protocol: 1`. The worker verifies each explicitly
selected source tool before its preparation inference. Dots creates the canonical
preparation document from structured facts, excerpts, unknowns, permissions and limits;
the runtime saves it in encrypted call state and validates its call/revision/hash before
start. Authenticated `get_call` exposes the readable document. This document creation
is separate from Codex's prewarm inference. Requests/results are nonce-bound to call, revision and
brief hash, expire within the decision timeout and are deleted after delivery.
Each call gets a separate ephemeral Codex session, reused throughout that call,
with one preparation turn and no fixed live consultation count. Later model inputs
use context deltas, falling back to complete snapshots when those are smaller.
Evidence replacements and removals supersede prior state. Full evidence is still
checked locally; the voice frontend receives only the next step and validated answer.
Preparation and handoff byte/timing metrics are recorded under call usage.
The preparation turn uses the same output schema, instructions, tools, model and
reasoning effort as live decisions and report reviews. Changes to the schema,
instructions, model or effort within one revision are refused. A compact response
selects preparation, waiting, factual speech, guidance, report review, menu control,
briefing-fact entry or retrieved-source entry. The runtime expands that response
into full advice before the existing evidence and control validation. Preparation
cannot speak, execute controls or perform lookups; its response is never live advice.
Usage records retain reported input/cache/output token counts, output bytes, time
to first token and final-message completion time. Reported cache usage must be
measured; reusing a thread alone is not proof of a cache hit. The worker explicitly
uses default service tier with Fast mode disabled.
`DOTS_PHONE_LOCAL_REASONER_EFFORT` selects reasoning effort; the default remains
`medium`. The call worker disables inherited coding
skills, unrelated plugins/MCP servers, shell, web search and agent delegation in
its own configuration. These overrides do not change the user's normal Codex setup.
For a narrower connected-source inventory, add
`--source-tools=gmail.search_emails,gmail.read_email` to the worker command, using
the authenticated runtime's exact tool names. The named tools must already be
available and read-only. The brief's optional `source_tools` selects exact
`namespace.tool` names within this worker selection. Omitted/null inherits it; `[]`
disables live tools. An unavailable or disallowed explicit selection prevents dialing.
Preparation records the effective tool manifest; the voice model sees available
connector identities and routes requests through Codex. This applies to connected
apps generally, including Gmail and Yahoo when selected and authenticated. Custom
MCP-only plugins remain disabled rather than being inherited without a scope.
An omitted worker list retains the available read-only set;
`--no-connected-tools` disables connected sources entirely. Calls without
`read_call_sources` always have zero connected tools enabled. Discovery catalogues
may still list disabled tools; the catalogue count is distinct from configured
availability. Approval-requiring actions are refused. No Dots decision handler or
native cleanup tasks are needed in this mode. Terminal notifications are optional.
Legacy deployments with independent reasoning disabled use the workflow below.

Start ordinary calls through Dots. Dots gathers known task facts from its memory
and authorized connectors, verifies identity/provenance, and supplies the shared
preparation document. The Codex delegate supplements that document through the
call's selected read tools when new questions arise. Neither agent's connector
access proves the other's access, and the delegate does not inherit Dots memory.
For source-enabled calls, a worker advertising zero read tools blocks start before
model preparation; an empty inherited manifest also fails prewarming. Explicit
`source_tools: []` remains the way to request supplied-facts-only behavior.
Do not leave `--no-connected-tools` enabled after a role-play when returning to
normal account inquiries. Verify the worker heartbeat's `connected_read_tools`
and the scoped per-call manifest, rather than relying on catalogue counts.

Read-only general conversations can use this same local supervisor when their
actions include `ask_question` and `record_call_result`, optionally with
`read_call_sources` and `navigate_phone_menu`. They have a separate general scope; they do not inherit
account permissions. A saved `local_reasoner` flag alone cannot waive event
subscriptions: the runtime must have a registered autonomous worker, a fresh
heartbeat and successful per-call preparation before dialing. This route works
with connector gateways that expose call tools but cannot register event streams.
It needs no native host task IDs or webhook subscriptions. Other action sets
retain their existing supervision requirements.

General conversations retain the configured native general-call audio profile,
512 output tokens and the configured voice reasoning effort. Account tasks retain their task
profile below. `--no-connected-tools` is appropriate for a supplied-facts role-play
that requires no personal sources.

The existing `wait_for_recipient` tool covers non-addressed music, TV, side
conversations and other background audio as well as hold announcements. It
acknowledges silently without requesting another model response; an unclear
request addressed to the assistant still calls for a brief clarification.
This does not prevent native VAD from initially cancelling audio on a false
speech start. Rejected final-report feedback is delivered immediately through
the acknowledged data queue, with any subsequent speech gated separately.
Its validated clarification question is preserved across unchanged retries.
Accepted final results retain their evidence and freshness checks.

Shared task calls default to `DOTS_PHONE_PIPECAT_TURN_DETECTION=semantic_vad` and
`DOTS_PHONE_PIPECAT_VAD_EAGERNESS=high`. Semantic VAD determines completion from
the utterance, with high eagerness's documented maximum wait of two seconds;
this is not a guarantee of faster responses or a two-second end-to-end limit.
The selected response policy also applies to automatically created responses.
To restore the prior configuration, set `DOTS_PHONE_PIPECAT_TURN_DETECTION=server_vad`
and `DOTS_PHONE_PIPECAT_VAD_SILENCE_MS=1000`. Silence duration applies only to server
VAD. No automatic fallback or additional session is created on a provider error.
Offline tests verify serialization and existing interruption/consultation guards.
Text benchmarks do not validate VAD accuracy, audio latency or IVR pause handling.
Provider audio validation remains a separately authorized test.

Dots starts with a plain goal, retrieves relevant authorized context, asks only
for missing information, and builds a bounded prepare_call request internally.
Normal local installations use connected_owner authorization. The owner signs in
once when linking the phone connection; an authenticated prepare_call then issues
a client-bound grant for the exact brief within configured call limits. No per-call
password page is needed. Dots owns interpreting the user's request and obtaining
any new permission; OAuth identifies the delegated owner, not evidence of a new
human click for every call. Preparation never dials or reserves daily spending.
A start atomically consumes its grant and reserves the brief's budget for 24 hours.
Client mismatches, scope changes, expiry, revoked destinations, reduced limits and
exhausted daily reservations cannot start. Reads never refresh or create a grant.
owner_page is the explicit legacy mode; its password belongs on the owner page,
never in a tool argument. No real task should be marked synthetic.

Before dialing register call.decision_required, call.completed and call.failed
subscriptions filtered to the call ID. Keep terminal reporting separate from
live decisions; on terminal wake first retrieve get_call_result. After acknowledged terminal delivery
and confirmed voice closure, the runtime retires that client’s call subscriptions
automatically while preserving the call history. Failed deliveries keep retrying
before cleanup. Dots registers its native decision/terminal task IDs with register_call_handlers
before starting. The terminal handler retrieves these IDs, pauses both native
tasks, reads back disabled settings and calls report_call_cleanup. This separate
host attestation is exposed as host_lifecycle.state=reported_closed; server
subscription retirement does not prove native tasks were paused. Use the current
authorization_id and exact revision from get_call for one start. Unknown start
status is not permission to redial. Source text and caller statements never grant
new permissions. The host Dot can supervise through its connected tools; a new
agent and a long handoff are not required.

An observed abnormal model socket close confirms closure even when final usage
is incomplete; it does not confirm billing or a successful inquiry. A late closure
emits another terminal event so Dots can finish cleanup without polling or an
operator message. Subscription retirement waits for the latest terminal delivery.
Startup reconciles persisted socket-close evidence from older versions and emits
that closure event once. Missing evidence remains unconfirmed.

An unexpected model transport disconnect gets a bounded four-second reconnect
on the existing carrier call when no source decision or finalization is pending.
The approved scope, tools and retained observed conversation seed the new session;
no dialing, keypad input or tool calls are replayed. Failed recovery ends the call
and follows normal cleanup. Recovery metadata records close codes and exception
classes, never raw provider error messages. Later independent transport losses
may recover under the same checks; concurrent recovery is suppressed. Recovery
replaces its previous context block instead of accumulating stale copies.

When `max_seconds` is omitted or null, preparation derives the longest affordable
whole-second duration within `DOTS_PHONE_MAX_CALL_SECONDS` (default 600). The shared
reservation formula is `(seconds + 30) / 60 * $0.22 + $0.50`, rounded up to four
decimal places. A $2 budget permits 379 seconds under that estimate; $3 fits the
default 600-second installation ceiling. These are conservative reservations,
not confirmed spending or a live dollar meter. Explicit user duration caps remain
binding and are refused if they exceed installation policy or the authorized
budget. Dots must not substitute an assistant-chosen three-minute test cap.

The optional `experiments/pipecat/approved_call_worker.mjs` controller handles a
single approved call and its optional connected Gmail/Yahoo relay. Neither mail
connector is required: zero authenticated mailboxes yield unavailable source
responses without blocking supplied briefing facts or call supervision.
Configuration and wire contracts are in the bundled skill's
[calling reference](../plugins/dots-phone/skills/dots-phone/references/calling.md).
Preflight authenticates access without searching mail. The controller does not
create grants, subscriptions or enable dialing. It saves a private persistent
start marker; do not remove that marker to retry after uncertainty.

The local caller owns account-task reporting; the source supervisor relays its
selected queries and actual records. Account mode can also request_call_information for verified evidence from the
host's memory or other plugins. The host uses the exact lookup request ID/revision
and evidence envelope from the calling reference; unavailable sources remain
unknown. Other tasks can use the general pending question/decision mechanism.
Owner voice joining, transfer and conferencing are not implemented; preparation
reports `interaction_capabilities.owner_voice_join=false`. A recipient requiring
the account holder personally creates a real blocker unless the recipient accepts
an available alternative. The caller should report that requirement through the
host lookup before ending, within the existing limits. Never promise a bridge or
redial to evade verification. An unavailable owner leaves the inquiry unresolved.
Report missing data or declined verification honestly. A clean hangup is not
proof that the task succeeded. Retrieve the bound terminal result and distinguish
confirmed facts, commitments, unresolved questions and estimated/confirmed costs.

## State and recovery

`get_call` is authoritative. It supports bounded long polls for live supervision.
`end_call` is idempotent; a stopping state means teardown still needs confirmation.
After a server restart, known calls are ended and uncertain starts remain reserved
for reconciliation. Do not launch two replicas against the same SQLite volume.
Do not automatically retry ambiguous dialing or reset spending reservations.

The unauthenticated /healthz endpoint reports configuration, not end-to-end phone
or OAuth readiness. Owner/media gateways exclude MCP. The private MCP server
requires single-owner OAuth with the phone:control scope. Use exact redirects and
callback hosts from your own registration, and never print runtime secrets.

## Retention

SQLite encrypts call objects and outbox payloads. Pipecat retains role-labeled
transcripts and both carrier audio directions by default for 24 hours. Configure
DOTS_PHONE_RETAIN_RECORDING, DOTS_PHONE_RETAIN_TRANSCRIPT and
DOTS_PHONE_RETENTION_SECONDS (60 to 604800) to suit your installation. Retained
carrier audio records what the adapter received/sent, not an independent remote
microphone. The legacy TAC backend lacks that recording hook.

For silent or clipped Realtime calls, compare `usage.realtime_speech_delivery`
model-output and forwarded PCM bytes (24 kHz, 16-bit mono),
`usage.carrier_playback` received/forwarded/suppressed milliseconds, and
`recording.assistant_bytes` (8 kHz PCMU). These are different boundaries; a
transcript or forwarded frame does not prove carrier delivery or audibility.
`model_input_audio_packets_sent` counts recipient audio sent to the model;
`model_audio_packets_sent` is its retained legacy name. Realtime uses server
VAD through the user aggregator as its single turn-interruption authority;
the Live engine retains its local speech gate. Playback diagnostics are saved
when the pipeline closes and are unavailable on older call records.

The model receive loop waits for that native interruption to finish at output
before admitting later response audio. Truncation of the interrupted item happens
after the clear attempt, so a slow provider send cannot delay clearing or erase
the following reply. A missing local output acknowledgement ends the voice path
with `realtime_path_failure.phase=interruption_ordering`; it does not trigger a
model reconnect. Local clear completion is not a carrier playback acknowledgement.

Ordinary general-inquiry Realtime calls default to a native server-VAD threshold
of 0.5, configurable with `DOTS_PHONE_GENERAL_VAD_THRESHOLD` (0 through 1).
Higher thresholds can miss quieter callers; validate any override against both
background noise and quiet directed speech before using it.
Prefix padding and end-of-turn silence remain 300 ms; the silence setting does
not delay interruption onset. Task calls use the separately configured task detector.
Native interruption, response cancellation and continuous input dispatch remain
authoritative; there is no second detector or acknowledgment-duration filter.
Short acknowledgments and short deliberate stop requests cannot be distinguished
reliably by duration. Offline scripted-event tests verify transport/recovery
invariants, not noise rejection by the remote native VAD.

`DOTS_PHONE_GENERAL_TURN_DETECTION` selects `server_vad` (the unchanged default)
or `semantic_vad` for ordinary general calls. Semantic mode uses the configured
`DOTS_PHONE_PIPECAT_VAD_EAGERNESS`; its server threshold and silence fields are
omitted. Registered worker and keypad routes use the configured task detector,
including legacy hotel and mail workers; they do not hardcode a different VAD mode.
`DOTS_PHONE_PIPECAT_NOISE_REDUCTION` selects `off` (the unchanged default),
`near_field`, or `far_field` for Realtime input. `off` sends an explicit JSON null;
omitting a field would not reliably disable a previously configured filter.
Inspect the accepted configuration before interpreting a trial.

`DOTS_PHONE_GENERAL_VAD_IDLE_TIMEOUT_MS` exposes OpenAI's native idle check-in
for ordinary general calls using `server_vad`. The default `0` disables it;
enabled values must be 5000–30000 ms. The adapter sends an explicit null when
disabled, preserves the field through Pipecat's nested session serialization,
and handles `input_audio_buffer.timeout_triggered` without creating a local
turn or response. Accepted settings and the count of timeout events are retained
in call diagnostics. Semantic, manual-turn, registered worker and keypad routes
do not acquire this timeout.

OpenAI starts the timeout after the last response plus its audio playback
duration. This is a check-in for inactivity, not a maximum duration for an open
speech turn. Noise that keeps triggering VAD can still prevent the timeout.
See the [official idle timeout guidance](https://developers.openai.com/blog/realtime-api)
and [VAD configuration guide](https://developers.openai.com/api/docs/guides/realtime-vad).

These options are experimental controls, not validated noise fixes. Keep the
native threshold at its configured default and the idle timeout disabled unless
your installation has validated a different setting. Evaluate noisy input, quiet
speech and directed interruptions together. A timeout is a check-in, not proof
that a missing turn endpoint has recovered. Local DeepFilterNet preprocessing is
configured separately; see the [filter guide](DEEPFILTER.md).

Ordinary general Realtime calls ask the approved substantive question or request
first, then clearly identify the speaker as an AI assistant in the same opening
turn. This policy excludes registered Gmail, mail and context/task integrations,
all keypad routes, and the legacy Live engine. A repeated human greeting can
prompt the still-unanswered question without another introduction; recipient
speech, answers, refusals, hold requests and requests to stop remain controlling.
The shared IVR instructions and identity policy for other call routes are
unchanged. This is a prompt mitigation for a question queued behind an
introduction; it does not restore discarded speech or guarantee model phrasing,
spoken duration, recipient audibility or successful completion.

With native VAD enabled, the provider owns creation of a fresh response after an
incoming turn ends, using the conversation after truncation. The application
does not force a retry or replay the discarded audio tail. A completed model
response means generation finished, not that the recipient heard every word.
A transcript or completed model response does not establish recipient audibility.
Use your own authorized test inputs to check output delivery and interruption
behavior; keep recordings, event traces and result summaries outside the repository.

`usage.accepted_voice_configuration.turn_detection` and `.noise_reduction`
retain only explicitly acknowledged provider settings. Missing fields are not
inferred defaults. `usage.realtime_interruption_diagnostics` retains the latest
128 native speech/response lifecycle events and 128 response audio counters,
with dropped-record counts. It records cancellation status/reason, hashed
provider identifiers, local monotonic offsets with an epoch anchor, and provider
audio offsets on their separate clock. It excludes speech, transcripts, prompts,
and free-form error messages. The per-response received/forwarded byte counters
measure model-to-pipeline PCM audio, not carrier delivery or playback.
Diagnostic events and counters accumulate in memory. Existing usage saves,
including response completion and finalization, persist the snapshots; diagnostic
observation adds no storage access before interruption handling.

The same report's `native_turn_state` retains the last observed start, stop and
commit independently of the rolling timeline, including the model session count.
No observed event means unknown; an old session's last event is not evidence of
the current session's speaking state. `connection_lifecycle` records bounded
local observations of carrier stop/disconnect, local close requests, pipeline
stop, model receive-loop termination and model socket closure. Its order is an
observation order, not proof of which remote party initiated the hangup. A
receive loop can end through local cancellation without a remote close.
Input snapshots contain queue, in-flight and resampler-delay counters only;
pending samples are neither a delivery acknowledgement nor proof of lost audio.

Ordinary Realtime carrier observations are made after authenticated binding.
Keypad routes observe the logical carrier transport instead: intentional stream
replacement remains connected, and a generated logical disconnect is not logged
as a remote carrier close code. The legacy Live engine is unchanged. Metadata
does not flush input, append silence, commit a turn, request a response or delay
teardown. Existing usage/finalization saves persist the Realtime snapshot.

The media gateway emits one content-free `media_gateway_teardown` JSON log record
per accepted voice bridge through the existing Uvicorn logger. It records the
first boundary observed by the relay tasks, bounded message/byte counts and
relative times, plus cleanup outcome. When both relay tasks are ready, ordering
is explicitly ambiguous. Gateway `upstream` means the phone application's
socket, not necessarily the model connection. Only standard numeric close codes
are retained; close reasons, raw messages, headers, URLs and identifiers are not.
A cleanup close against an already disconnected peer is handled without a second
exception replacing the first observation. These diagnostics provide future
evidence; they do not recover missing historical lifecycle events.

When correlating speech with clears, use archived packet arrival times as well as
the rendered WAV. The renderer serializes buffered incoming packets, which can
shift recipient waveform positions later than the observation clock. A quiet
WAV sample at a clear timestamp alone does not establish a false interruption.

Export a call privately with `python -m dots_phone.export_evidence CALL_ID DIRECTORY`
inside the runtime. WAV channels are recipient left, assistant right. Keep exports,
mail, source traces and approval material out of Git. Rotate credentials deliberately;
changing the encryption key makes existing records unreadable.

## Optional Bitwarden

The default setup writes local runtime credentials directly. If you prefer BWS,
install the vendor's BWS CLI and set DOTS_PHONE_BWS_PROJECT_ID to your own project
UUID. Create the seven named
entries in dots_phone.bws.MAPPING, and use a read-only machine token stored in an
owner-only file. `python -m dots_phone.bws preflight` checks names/project/formats;
`serve` runs locally and `compose` uses the older production Compose launcher.
BWS never has a developer-owned default project. Its bootstrap token is not
passed to the phone application. These optional commands do not configure the
new local connection profile or create your ChatGPT registration.

Only the optional `python -m dots_phone.bws setup --approved-save` path, which creates missing
application secrets, needs the Bitwarden SDK and a token allowed to write those
secrets. The SDK is excluded from default installations and app images. Review
the [Bitwarden SDK license](https://github.com/bitwarden/sdk-sm/blob/main/LICENSE)
and obtain it directly from the vendor through the explicit extra if its terms
permit your use:

```sh
uv sync --frozen --extra pipecat --extra bws
```

The project’s MIT license does not grant rights to redistribute this SDK. Do not
include it in redistributed images or packages without appropriate vendor rights.
The recommended local-file setup needs neither BWS nor its SDK.
