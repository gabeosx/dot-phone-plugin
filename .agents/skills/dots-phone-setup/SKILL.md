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

Keep `DOTS_PHONE_DRY_RUN=true`. Setup alone does not authorize `start_call`, paid
model evaluations, email queries or purchases. Existing explicit authorization
still applies, but do not invent a live test as an installation requirement.
Keep models, recordings, transcripts and private test results outside the checkout;
use external assets only when supplied, without fetching private fixtures.

Finish with the installation location, configured filter and verification result,
worker/service/link status, and any remaining user sign-in or credential steps.
State what was actually checked and what still needs a separately authorized
live test. Leave a clear restart/rollback path without exposing private values.
