---
name: dots-phone-setup
description: Set up or repair a local Dots Phone installation from this repository, including credentials, tunnels, the signed-in Codex worker, plugin registration and local audio filtering. Use for installation and no-dial readiness, not for placing calls.
---

Help the user reach a working local installation with dialing disabled. Complete
routine reversible setup within the request; collect only genuinely missing
sign-ins, credentials or choices. A successful health check is not proof of funded
carrier routing, working paid models, mailbox access or acoustic quality.

Read [the quickstart](../../../docs/QUICKSTART.md) for prerequisites and the setup
sequence, then [local setup](../../../docs/LOCAL-SETUP.md) for the actual deployment
and registration details. Resolve commands from the repository root. The README
prompt can load this file directly before the phone plugin is installed; the
installed `dots-phone` skill handles calls after setup.

Check that the user's own account supports custom MCP/plugin registration and
Secure MCP Tunnels before asking them to obtain paid provider resources. If those
features are unavailable, complete the credential-free offline checks and report
the default connection path as unavailable. Do not promise a hosted signup or
public marketplace installation. Use [troubleshooting](../../../docs/SUPPORT.md)
for common failures and the local setup guide's upgrade procedure for an existing
installation.

Inspect existing tools, checkout and running services first. Reuse a suitable
installation without overwriting its files. Preserve encryption keys, owner
identity, budgets, source permissions, registration and explicit user choices.
Check for active calls before recreating any existing service. If the user wants
an upgrade or repair, do not rerun initialization over their private directory.

Use the hidden prompts in `python -m dots_phone.setup init` for new provider
credentials and the owner password. Have the user complete account sign-ins
locally. Secrets never belong in chat, command arguments or tracked files. Keep
all Compose commands tied to the same private `--env-file`, including custom
paths containing spaces. Do not dump runtime JSON or complete environments into
logs. Follow the installation's actual tunnel and OAuth registration; example
IDs and origins are placeholders.

Recommend DeepFilterNet3 enabled for a new installation unless the user opts out.
Follow [install and verify](../../../docs/DEEPFILTER.md#install-and-verify-without-dialing):
provision checksum-verified weights outside Git, build the native image, mount
only the weights read-only and run the isolated native streaming checks without
network access or provider credentials. Then enable `deepfilter` in the private
configuration and use the filter overlay for service startup and recreation.
The ordinary runtime stays `off` until these prerequisites exist. Do not silently
replace an explicit opt-out, claim filtering works from an environment flag alone,
or hide a failed build/check. Explain a genuine prerequisite failure and the
available unfiltered fallback. Preserve any filter setting on an existing install
unless the task calls for changing it. Rollback is `INPUT_FILTER=off` between calls;
the guide also explains removing the overlay's model mount.

Optional [local audio review](../../../docs/LOCAL-SETUP.md#optional-local-audio-review)
is a separate, explicit choice and stays off by default. If requested, use
`python -m dots_phone.audio_review_assets` and its `--model medium.en` option to
prepare checksum-verified primary and confirmation files outside the checkout
(about 2 GB total), verify them offline and include
`compose.audio-review.yaml` consistently with a read-only mount. It requires
retained recording data; preserve recording opt-outs. Do not imply that independent
recognition guarantees correct numeric reports or resolves every transcript conflict.

Start the host Codex worker using the generated private reasoner directory and
ChatGPT sign-in. Keep that directory consistent with the service mount. Dots and
the worker have separate tool access: mail and other read plugins are optional,
and availability must be checked rather than inferred from the parent chat.
Generate a separate private plugin package with this installer's registered app
mapping; never edit the shared plugin template to include personal identifiers.

Verify the application and both gateways, the effective filter selection and its
read-only mount, the worker's fresh heartbeat, and authenticated phone tool/skill
discovery where linking is available. `get_phone_workflow` reads the static call
instructions without dialing. Distinguish a native skill import from that fallback.
Check the installed bundle version; editing a checkout does not refresh an
already installed plugin snapshot.

Use `python -m dots_phone.setup check` with the same `--env-file` and repeated
`--compose-file` options as service startup, plus `--running` after startup.
It renders configuration privately and reports actionable local checks without
credential or path values. For frozen rendered Compose, pass that file directly;
the running check can inspect its existing project tunnel container. `--json`
returns structured results. Resolve incomplete items without rotating keys or
changing explicit choices. A passing local diagnostic does not establish remote
registration eligibility, authenticated linking, paid model/carrier behavior,
acoustic quality or mailbox access.

Keep `DOTS_PHONE_DRY_RUN=true`. Setup alone does not authorize `start_call`, paid
model evaluations, email queries or purchases. Existing explicit authorization
still applies, but do not invent a live test as an installation requirement.
Keep models, recordings, transcripts and private test results outside the checkout;
use external assets only when supplied, without fetching private fixtures.

Finish with the installation location, configured filter and verification result,
worker/service/link status, and any remaining user sign-in or credential steps.
State what was actually checked and what still needs a separately authorized
live test. Leave a clear restart/rollback path without exposing private values.
