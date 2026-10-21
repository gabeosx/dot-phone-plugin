# Privacy and data handling

Dots Phone is self-hosted. The project does not operate a shared calling service
or receive your installation's records. Running locally still sends information
to the providers and connections you choose.

## Where information goes

| Component | Information it handles |
| --- | --- |
| Your local service | Prepared task, authorized disclosures, call state, reports, usage, and optional retained transcripts and audio. Sensitive SQLite payloads are encrypted with your installation's key. Record identifiers and timestamps are not all encrypted. |
| OpenAI Realtime | The prepared context, conversation audio and relevant tool results used by the voice model. |
| Signed-in Codex worker | Task context, conversation evidence, decisions and authorized connected-source reads. It uses your ChatGPT Codex account. |
| Twilio and the recipient | Telephone number, call routing and call audio. The recipient hears information disclosed during the conversation. |
| Your tunnel providers | The traffic routed through their owner, media or MCP connections. Public media ingress carries phone audio. |
| Optional connected sources | Requests and records read through tools you have authorized for the task. Connecting the phone plugin alone grants no mailbox access. |
| Optional local audio review | Retained recipient audio is transcribed on your CPU. Its observations can be included in the Codex review context; the local recognizers do not call an additional speech API. Public model downloads are separate from processing call audio. |

The worker exchanges task context through owner-only JSON files in the private
reasoner directory. Those exchange files use filesystem permissions, rather than
the database's encryption. Protect that directory and the machine's account;
local payload encryption does not protect an unlocked or compromised computer.
The host uses ephemeral Codex sessions for the call worker, but this does not
override any provider's handling or retention of requests.

Provider billing, account settings and retention apply separately. Local deletion
does not delete copies held by OpenAI, Twilio, connected-source services, tunnel
providers, the recipient, or your own backups and exports. Review those accounts'
data settings before using sensitive information.

## Local retention and control

The Pipecat path retains transcripts and both audio directions by default.
Terminal call records and their stored audio become eligible for deletion after
24 hours; the running service checks expiry periodically. A stopped service
cannot perform cleanup. This is a retention policy, not a guaranteed secure erase
of every disk, backup or provider copy.

Set these in your private `config.env` before creating calls, then recreate the
service after active calls finish:

```dotenv
DOTS_PHONE_RETAIN_RECORDING=false
DOTS_PHONE_RETAIN_TRANSCRIPT=false
DOTS_PHONE_RETENTION_SECONDS=86400
```

These values disable audio/transcript archiving for new calls. The service still
uses transient conversation context during the call and stores its task, result
and operational evidence until expiry. Changing the flags does not remove an
existing call's archives. Retention accepts 60 to 604800 seconds. See
[operations](RUNBOOK.md#retention) for implementation details.

To stop using an installation, close any active call, stop the service and worker,
disconnect its plugin and optional source connections, revoke its provider
credentials as appropriate, and remove its local database, private configuration,
worker files, exports and backups when you no longer need them. Preserve the
encryption key while any retained database or backup still needs to be readable.
Never post a database or private directory
in a support request; use [the support guide](SUPPORT.md).

## Public repository boundary

The distributable source contains generic code, fictional fixtures and setup
instructions. Each installer's provider keys, owner password hash, encryption key,
app/tunnel registration, phone numbers, mailbox identity and source exclusions
belong in local ignored files or the generated private install package.

Keep call recordings, transcripts, account facts, mail, approval grants and
operator diaries outside tracked files. This also excludes actual-call summaries,
recipient quotations, measurements, event IDs, hashes, and sanitized reenactments
of private conversations. Changing names or removing an account number is not
enough to make a private-call report publishable. Use independently authored
fictional fixtures for public tests. Runtime objects are encrypted locally;
that does not make a Git commit safe to publish. Build artifacts must not include
`.secrets`, `data`, `artifacts` or developer installation mappings.

Run `python scripts/secret_scan.py` before committing. The repository also uses
Gitleaks on full Git history in CI. These scanners catch known patterns, not every
form of personal information; review tracked docs, examples, fixtures and generated
packages before publishing. Private denylist files can supplement the scan without
committing personal values.

Removing a value from HEAD leaves old commits readable. Cleanup must cover all
branches/tags and local checkpoint refs/reflogs, then be verified from a fresh
clone of the rewritten remote. A force-push does not erase other people's clones,
GitHub pull-request refs, cached commit pages or previous CI artifacts. Those
copies require separate removal or host support; do not claim history is clean
until those accessible copies have been audited. Never publish an old backup ref
or merge old commits back into a sanitized repository.
