# Pipecat development tools

This directory contains the Pipecat transport, signed-in Codex workers, synthetic
recipient simulators and evaluation harnesses. Some installation commands use
these files directly; the directory name does not mean all its contents are
optional research artifacts.

For installation use [the quickstart](../../docs/QUICKSTART.md). Runtime settings,
source tools, voice configuration and task supervision are documented in
[operations](../../docs/RUNBOOK.md). See [local filtering](../../docs/DEEPFILTER.md)
for the optional native image and external model weights.

## Offline checks

From the repository root:

```sh
uv sync --frozen --extra pipecat --python 3.13
uv run --no-sync python -m dots_phone.checks offline
node --test experiments/pipecat/tests/*.test.mjs experiments/pipecat/test_automatic_call_worker.mjs
```

The public scenarios under `cases/` use fictional people and records. Protocol
tests use mocks or authored events. They establish component behavior, not a
provider's ability to hear speech or finish an unfamiliar telephone task.

## Optional evaluations

The harnesses can produce recordings, transcripts, result documents and provider
usage records. Store those outputs outside the checkout. Paid model sessions and
carrier tests require the operator's own credentials, explicit scope and budget;
installation and offline validation do not authorize them.

Synthetic recipient examples include hotel, appointment, support and rewards
inquiries. Source simulations exercise successful, delayed, conflicting and
unavailable lookups. Connected-mail variants require the installer's approved
source tools; no mailbox contents or private-call evidence ship here.

Use the [developer evaluation commands](../../docs/QUICKSTART.md#developer-checks)
for reproducible invocation and the [validation guide](../../docs/VALIDATION.md)
for measurement boundaries. Keep actual-call details and internal experiment
reports private, including anonymized summaries and recorded timing metadata.
