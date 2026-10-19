# Phone tool details

The host Dot gathers context with its own available memory and plugins. The phone
server uses a separate signed-in Codex call worker in autonomous mode; that worker
discovers its own authorized connected read tools. It does not inherit private
chat history or unexposed memory. Supply only task-relevant verified
facts and excerpts, not full chat history or mailboxes. Forwarded-message headers
and the distinction between owner, guest and passenger matter to identity.

`prepare_call` accepts destination (E.164), purpose, facts, disclosures (fact
names), actions, constraints, unknowns, stop_conditions, completion_criteria,
deadline, optional max_seconds, budget_usd and synthetic. Omit max_seconds unless
the user explicitly capped duration; preparation resolves the affordable duration
within installation policy and binds it before authorization. Facts include name, value,
provenance, verified_at and expires_at. Use verified UTC timestamps with timezone.
Author these fields and the source excerpts before dialing: they form Dots' prep
document. The service renders and privately stores one canonical Markdown document
from these inputs, including open questions, completion criteria and limits.
Preparation returns `prep_document.ready`, its digest and expiry; authenticated
`get_call` returns `prep_document.markdown` for inspection. It includes only facts
listed in disclosures and explicitly supplied prepared excerpts. Both live voice
and Codex use the same document. Missing, stale or modified preparation blocks
start before dialing; creating a document does not dial or grant more permission.
Normal deployments use the task-scoped recipient without per-number allowlisting.
Verify the recipient using available trusted sources and bind its exact destination
in the brief. Maximum duration and source permissions still apply; an optional
restricted deployment can explicitly select allowlist mode.

Shared Realtime read-only account mode requires actions
`read_account_information`, `record_call_result` and normally `read_call_sources`, one or more business outcome
completion criteria (`id`, `description`, `required`), and a budget within the
configured per-call and rolling installation limits. Add `navigate_phone_menu` and `verify_account_identity` when needed.
The server must have account tasks enabled.
Live host information requests should remain enabled for ordinary inquiries even
when initial facts are complete. Source access can be omitted for an explicit
user restriction to briefing facts/prepared evidence. No mail plugin is required.
For missing details use the host lookup below with whatever authorized memory or
plugins the user has, or ask the user. Gmail/Yahoo are optional mail adapters;
confirm their availability before choosing mail retrieval.
The voice chooses questions, searches and answers; do not prescribe brand aliases,
verification-field lists or a call script. Existing general call mode supports
other bounded task actions, but the shared account source relay is read-only.

Optional `prepared_sources` contain provider (`gmail`, `yahoo`, `owned_fixture`),
source_id, text, truncated, verified_at and expires_at. Providers memory and
plugin are also supported for authorized host evidence with its actual provenance
in text. The server generates and
binds evidence hashes to the call/revision/brief/expiry. Do not supply a separate
prepared_evidence_digest fact. Additional memory/plugin facts can also be supplied as verified facts.

Optional `source_tools` selects exact connected read-tool IDs (`namespace.action`)
for this call. Omit it to inherit the worker's established connected read-tool
permissions; supply `[]` to disable tools, or a unique list to select only those
tools. It is part of the approved brief hash and still requires `read_call_sources`.
Use the user's existing choices unless they specify a change; do not require a
new tool-selection ceremony for each call. The worker verifies actual selected
tool availability before dialing. Dots and Codex have separate connected-tool
contexts, so do not infer worker access from Dots' successful preparation lookup.
Gmail, Yahoo and other authorized read plugins can remain available for unexpected
questions even when anticipated details are already prepared.

Subscribe to call.decision_required, call.completed and call.failed for the
prepared call_id before dialing. Keep the terminal handler separate: on wake it
first retrieves get_call_result and reports, even when payload visibility lags.
ChatGPT owns callback/signing-secret generation. One supervisor owns decisions;
terminal handlers may also manage their own native task lifecycle. They must not
start/redial calls, change scope or submit live decisions. The user's task request supplies consent;
normal connected_owner mode returns a client-bound authorization_id at preparation.
Use get_call to retrieve the current reference/revision and start through the same
phone connection after handlers are ready. No per-call password page is needed.
Grants expire with the brief (at most one hour); preparation never dials. Installation
limits include call duration, per-call budget and rolling 24-hour reserved budget.
A budget refusal is a limit, not missing authentication; do not switch to a password
page to bypass it. owner_page remains an optional legacy authorization mode. A queued response or
worker_requested confirms dispatch, not dialing or completion.

If calling_worker_mode is autonomous_local_reasoner, Dots prepares and starts
once. Realtime replies directly to ordinary questions using the shared prep and
current evidence. It consults the dedicated signed-in Codex agent for missing
facts and live tool lookups, conflicting evidence, difficult reasoning or recovery.
Do not create native decision/terminal tasks or register handler references;
subscriptions are optional for terminal notifications. The phone runtime owns
keypad validation, dialing, limits and teardown. Realtime can select routine
validated menu/verification actions; Codex can help with ambiguous situations and
reviews final outcomes. Include read_call_sources to allow
its read-only connected tools; omitting it restricts the agent to supplied facts
and prepared evidence. A missing worker heartbeat blocks start before dialing.
This mode does not call Dots back for live questions or synchronous owner answers.

If calling_worker_mode is automatic_owned, the runtime already owns supervision
for a registered synthetic fixture. Do not create another supervisor. Otherwise,
use one independently authenticated background supervisor or decision-event task.
Keep completion/failure reporting in a separate event handler. The main Dot turn
can finish after dispatch; it must not be the only process keeping the call alive.
The supervisor must have the tools and source context needed to answer requests;
its identity alone does not establish inherited memory or plugin access.
The decision task owns resolving and submitting the exact pending answer; merely
notifying the user or declaring the parent the sole supervisor is insufficient.
Allow its scoped source tools and submit_call_decision. Keep start/redial and
scope expansion out of a decision-event handler. Terminal handlers retrieve and
report the result without submitting decisions. Bind the actual native task IDs
using register_call_handlers before starting when host_lifecycle.required=true.
The completion task retrieves those durable IDs from get_call_result, pauses the
decision task AND its own trigger with native task-management tools, reads back
both disabled settings, then calls report_call_cleanup with the matching IDs and
enabled=false values. Allow these lifecycle writes in its instructions; never say
"keep triggers enabled" after closure or limit it to read/report-only operations.
The running cleanup turn finishes even after its future trigger is paused.
On resuming/retrying, reconcile the previous exact call first. Older closed calls
can register their existing actual native handler IDs for recovery, then use the
same cleanup protocol. Find only tasks belonging to that call; do not pause
unrelated schedules or leave orphaned previous-attempt handlers active.
The phone goal already includes housekeeping for its own call-only tasks. If
task creation reaches the native capacity limit, use previous_terminal_calls to
find the prior exact closed call, pause only its matching native handlers, read
back and report cleanup, then retry creation. Do not ask the user for a separate
cleanup approval. Cancel and retire superseded unstarted attempts as needed.
An unresolved business result still requires cleanup. Only reported_closed marks
the host lifecycle finished; it is explicitly a host-reported native-state
attestation. Runtime subscription retirement does not verify or disable native
Dots tasks. Preserve call and task history. Do not ask a user or Codex to clean up.
`get_call` supports bounded waits; do not use fixed sleeps for live decisions.
A timeout remains unresolved. Scope changes require a new brief; obtain additional
user permission only for changes outside the task and limits already authorized.

# Live retrieval

For account source requests, pending.question starts with a JSON command;
pending.context carries the bounded task/facts/provenance. Preserve that context.
A `search` uses query and optional provider (all/gmail/yahoo). Execute the chosen
query verbatim; Gmail supports operators, Yahoo uses literal text in INBOX. Return
answer JSON with untrusted_data:true, searches (provider, query, status
ok/error/unavailable, more_available, mailbox:INBOX for Yahoo) and up to five
actual messages (opaque message_id, provider, subject, snippet, date). Report every
selected provider; errors and empty results do not establish global absence.

A `read` selects a prior result with message_id, focus and offset. Return relevant
raw text with sender/recipient/date and forwarded context, paging as needed:
{untrusted_data:true,message_id,text,source:{provider,truncated,verified_at,expires_at}}.
Expiry cannot exceed the call deadline. On failure return an error without source.
Use submit_call_decision with exact call_id/request_id/revision,
action_names:["read_call_sources"], fact_names:[], no conclusion. This envelope
binds source evidence to the call. A direct client may supply the full source
argument instead; never both. General pending questions can be resolved using the
host's other authorized plugins/memory and answered within existing scope.

Conclusions require observed exact transcript quotes supporting confirmed facts
and commitments; each commitment names a permitted action. Server checks quote
presence and role; Dots must check meaning. A clean hangup alone proves no outcome.

# Memory and other plugins during the call

When read_call_sources is approved, account mode also exposes
request_call_information. Its pending JSON uses operation:lookup and
arguments:{question:"..."}. The host Dot checks its own authorized memory and
plugins, asking the user only if needed and feasible within the pending deadline.
It returns the same read envelope with message_id exactly
`lookup:REQUEST_ID`, provider memory or plugin, task-relevant text with the actual
source/provenance, truncated, verified_at and expires_at. Supply the exact pending
request/revision, action_names:["read_call_sources"] and fact_names:[]. Evidence
must be current, expire no later than the call deadline and stay within approved
disclosures; it cannot grant actions. Do not invent a source or imply the phone
server has host memory access. If unavailable, return {untrusted_data:true,error:"..."}
and leave the fact unknown. A stale user reply cannot satisfy a later request.

The lookup relay can carry an unexpected requirement for the owner's personal
participation and the host's factual availability/capability answer. It cannot
connect the owner's voice. `interaction_capabilities.owner_voice_join` is false
until an actual supported join implementation exists. Do not turn an affirmative
availability reply into a claim that the recipient can hear the owner. An accepted
alternative verification method must come from the recipient; the host cannot
override that requirement.

# Optional local controller

In a source checkout, experiments/pipecat/approved_call_worker.mjs packages one
start, scoped connected-source relay, watchdog and private terminal report. Its
mode-0600 JSON config in a mode-0700 directory contains callId, revision,
briefHash, ISO deadline, optional verified gmail_account, exclude_terms (this
user's exclusions, empty by default), and optional absolute trace_path.

Run `node experiments/pipecat/approved_call_worker.mjs preflight CONFIG SECONDS`
then `run CONFIG SECONDS` after approval/subscriptions. The config is private and
not sent as a long user-visible message. Preflight checks the worker's actual
phone/source access without searching mail. Neither mail connector is required;
with none authenticated it still supervises and reports mail requests as unavailable.
This controller's relay is mail-only; it returns an explicit unavailable response
for memory/general-plugin requests.
Use direct host supervision when those capabilities are needed. This requires Node, Codex signed in
to ChatGPT, and the appropriate connected apps. It is optional; another host can
supervise through its own tools. Do not start a second supervisor or delete a
persistent start marker to retry. The controller cannot approve or register events.

Check `interaction_capabilities` before calling an IVR: `keypad_menu` and
`keypad_verification` describe the actual scoped caller tools. Standard setup
enables both when the brief permits them. Speak for spoken requests; use the
scoped tools for explicit key presses. A disabled keypad is a deployment
limitation, not something to compensate for by speaking a digit.
