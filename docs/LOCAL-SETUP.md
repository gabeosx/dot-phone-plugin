# Run Dots Phone on your own machine

Each installer runs their own single-owner instance. Credentials and the encrypted
SQLite database remain local; the project does not operate a shared phone service.
You still use OpenAI for voice, Twilio for telephony, ChatGPT for Dots, and your
chosen tunnels to reach the local machine. Keep it awake while calls are active.

The [setup skill](../.agents/skills/dots-phone-setup/SKILL.md) guides an LLM through
this installation without needing the phone plugin first. It links to these
procedures instead of maintaining a separate installer script.

Normal setup recommends **DeepFilterNet3 enabled**. Complete
[its install and no-dial verification](DEEPFILTER.md#install-and-verify-without-dialing)
after creating the private configuration and before starting services. Its native
image is installed separately and its weights stay in an external read-only cache.
The bare runtime default remains off until those prerequisites exist. To opt out,
set the private filter value to `off` and omit the filter overlay from commands.

## Local credentials and audio service

Install Docker Compose, uv, Node.js LTS and the Codex CLI. Sign Codex in with
ChatGPT using `codex login`. From a clone of this repository:

```sh
uv sync --frozen --extra pipecat --python 3.13
uv run python -m dots_phone.setup init --origin https://your-owner-tunnel.example
```

Use your own OpenAI key, Twilio Account SID/auth token/API key SID/API key secret,
and a new owner password of at least 20 characters. Enter secrets only at the
hidden terminal prompts. Setup generates the encryption key and password hash;
the destination directory must not already exist. Reuse an existing installation
instead of deleting its credentials to rerun setup. Use `init --directory /your/private/path`
for a different location; its generated config.env binds Compose to that exact
runtime.json, including paths containing spaces. Pass that config.env with
`--env-file` in the Compose commands below. Back up the encryption key privately: losing
it loses access to stored call data. `.secrets` is ignored by Git.

Set the ordinary values in `.secrets/config.env`. Start with dry-run enabled:

- PUBLIC_BASE_URL: your HTTPS owner-gateway origin.
- MEDIA_BASE_URL: your HTTPS media-gateway origin with WebSocket support. Use
  `https://`; the service derives the `wss://` URL.
- TWILIO_PHONE_NUMBER: your own voice-capable E.164 number.
- OAUTH_REDIRECT_URIS: exact callback URLs shown by your ChatGPT registration.
- ALLOWED_TEST_NUMBERS: optional comma-separated destinations for the `allowlist`
  policy or owned test campaigns. The default `task_scope` policy does not need
  a number allowlist. No numbers are bundled.
- CALLBACK_HOSTS: only needed for optional ChatGPT event subscriptions; use their
  exact hosts. Independent Codex supervision works without subscriptions.

All keys have the `DOTS_PHONE_` prefix. The example template lists other options.
The local Compose file uses Pipecat Realtime for phone communication and a
signed-in Codex worker for reasoning and connected read tools. The voice model
must be available to your API project; the reasoning agent uses your Codex sign-in.
The reasoning agent consumes your ChatGPT plan's Codex allowance. Realtime uses
separate OpenAI API billing, including live text evaluations; phone calls also
incur carrier charges. Running Realtime from Codex does not change its billing.
The generated private `reasoner/` directory is shared with the phone container.
Start the independent worker on your host before dialing:

```sh
node experiments/pipecat/codex_call_reasoner.mjs serve "$PWD/.secrets/reasoner"
```

For `init --directory /your/private/path`, use the matching generated directory:

```sh
node experiments/pipecat/codex_call_reasoner.mjs serve "/your/private/path/reasoner"
```

Keep that process running with the phone service. It creates ephemeral reasoning
sessions and filters their tools to connected read operations. It does not call
the main Dots conversation. Each authorized queued job warms its own model context
before dialing and reuses the same thread for incremental context updates. This
preparation consumes Codex allowance, with no Realtime or carrier session yet.
The worker uses one compact response schema for preparation and the whole call,
and expands responses into the existing evidence checks locally. Restart the
host worker after this upgrade so it advertises `decision_protocol: 1` and
`source_selection_protocol: 1` before
dialing. Fast mode is explicitly disabled for this worker.
New calls default to `DOTS_PHONE_LOCAL_REASONER_RESPONSE_POLICY=direct_with_codex`:
Realtime answers ordinary questions from the prepared document and conversation,
consulting Codex for missing facts, live retrieval, ambiguity and recovery. Completed
reports still receive semantic review. `always_consult` restores the required
consultation before every reply. `DOTS_PHONE_LOCAL_REASONER_VOICE_EFFORT=medium`
controls every Realtime call path independently of that policy. Call types do
not silently lower it. Codex uses `DOTS_PHONE_LOCAL_REASONER_EFFORT` separately.
Keep effort fixed when comparing the policies.
Existing saved calls without a policy retain always-consult behavior.

Dots fills the structured `prepare_call` inputs; the service renders and saves their
canonical readable preparation document before start. Retrieve it with authenticated
`get_call` when needed. Both models use the same bound evidence. Do not put a second,
independently editable script into the document.

`source_tools` in the brief can select exact `namespace.tool` names for this call.
Omitting it inherits the worker's configured read-only connected-tool selection;
`[]` disables live sources. Explicit selections must be available, read-only and
inside any worker `--source-tools` restriction. Include `read_call_sources` to enable
retrieval. Missing explicit tools prevent prewarming/dialing. Gmail, Yahoo and other
connected apps are optional; custom MCP-only plugins are not implicitly inherited.
Dots' access does not prove that the signed-in worker has the same connections.
Dots initiates ordinary calls and uses its own memory and authorized connectors
to prepare known account details with provenance before dialing. The delegate
receives that canonical preparation and task-appropriate connected read tools
for unexpected questions; it does not inherit Dots' private memory implicitly.
For an AA inquiry, verify whose account a number belongs to before including it;
a passenger or forwarded-message recipient is not necessarily the owner.
An inherited source selection with no available read tools now blocks start
before inference/dialing. An explicit `source_tools: []` still permits a deliberate
supplied-facts-only call. Select relevant tools explicitly when unrelated apps
cannot operate under the delegate's read-only isolation.
`DOTS_PHONE_LOCAL_REASONER_EFFORT=low` enables a lower-effort comparison; the default
remains `medium`. Model support must be verified through a successful signed-in
worker turn. Preparation reports `autonomous_local_reasoner` and
does not require native Dots decision tasks or subscriptions. Optional terminal
subscriptions can notify Dots; otherwise retrieve the result later. Disable
`DOTS_PHONE_LOCAL_REASONER_ENABLED` explicitly to use legacy Dots supervision.

Shared task calls use semantic VAD with high eagerness. Configure it using
`DOTS_PHONE_PIPECAT_TURN_DETECTION=semantic_vad` and
`DOTS_PHONE_PIPECAT_VAD_EAGERNESS=high` (`low`, `medium` and `auto` are also supported).
For the previous silence detector, set `DOTS_PHONE_PIPECAT_TURN_DETECTION=server_vad`
and `DOTS_PHONE_PIPECAT_VAD_SILENCE_MS=1000`. These values pass through local Compose.
Accepted settings and text benchmarks do not establish acoustic performance.
See [validation](VALIDATION.md) for the actual audio/carrier evidence and its limits.

The same Realtime session builder supplies task/factory and nonfactory calls.
General calls use `DOTS_PHONE_GENERAL_TURN_DETECTION` (default `server_vad`,
300 ms end silence); task calls, including registered hotel/mail and keypad
routes, use `DOTS_PHONE_PIPECAT_TURN_DETECTION`. The general threshold/idle
controls apply only to general server VAD; semantic VAD uses the configured
eagerness. General replies have a 512-token ceiling and task replies 1024;
the saved-audio harness retains its factory's ceiling. These profiles do not
change either model's reasoning effort. Experimental external turn ownership
is a separately admitted mode and explicitly disables native VAD.
Adding keypad tools to a shared general inquiry preserves its general profile.

Local Compose forwards test-mode flags, trace settings, decision/polling timeouts,
retention, both optional post-call model directories, and voice backend/engine
selection. Optional model directories must still be mounted read-only. The
reasoner directory inside the container follows `DOTS_PHONE_LOCAL_REASONER_DIRECTORY`;
its host directory is selected separately by `DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY`.
The IVR test overlay preserves the selected audio filter. Changing the timeout
does not extend a call's approved duration or monetary budget.

Account and general task workers use the centrally authorized call budget, with
no additional task-specific dollar ceiling. Connected-owner calls must fit the
configured per-call ceiling and rolling reservation budget; each start grant
binds the approved brief, including its budget. There is no fixed count limit on in-call
Codex consultations, live source lookups, keypad steps or report corrections.
Requests are counted for audit; authorization, approved call
duration, cancellation and individual request deadlines still apply under either
response policy. Signed-in Codex uses the account's Codex allowance, separately
from the call's Realtime/carrier budget. Preparation and closed-call review are
separate operations. Changing model effort does not expand call authorization.

Active-call history preserves earlier turns and source evidence without silent
entry-count or character trimming. The Codex transport still validates the full
serialized request against its 64 KB live / 256 KB closed-review envelope, and
output schemas retain field and token bounds. Oversized requests return an error;
they do not authorize dropping evidence or ending a call. Transient history is
cleared at closure even when transcript archiving is disabled. Reasoning uses the
configured decision window with two seconds reserved for delivery, rather than
an additional 55-second ceiling.

Connection setup still has separate transport deadlines: both telephony adapters
currently use a fixed 20-second ringing timeout, and carrier reattachment/model
recovery must finish within their audio-buffer windows. These are connection
policies, independent of the approved conversation duration. Historical synthetic
fixture registries also retain their own narrow budgets, durations and lookup
counts; account-task mode cannot load those registries.

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml up -d phone-app owner-ingress media-ingress
```

This runs only local containers. Gateways bind to loopback: owner on 8852, media
on 8851. Point separate HTTPS/WSS tunnels at those ports, using any compatible
local tunnel provider. They expose only the permitted OAuth/approval or carrier
routes, respectively; neither gateway exposes `/mcp`. If you do not yet have
origins, configure placeholders at setup, start the gateways, obtain your tunnel
origins, update config.env and recreate phone-app before connecting ChatGPT.
Temporary tunnel origins must stay running and unchanged through OAuth/calls.

## Connect ChatGPT to the local MCP server

Create a private Secure MCP Tunnel in
[OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels),
then select it when adding your custom MCP server in ChatGPT. Associate it with
the ChatGPT workspace and Platform organization that will use it. Creating a
tunnel requires Tunnels Read + Manage; running it requires Read + Use. Follow the
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for the tunnel ID and runtime API key. Store that key in `.secrets/tunnel-key`
with mode 0600 and use the exact MCP resource identifier from your connection.
Do not copy another installation's IDs, callback URL or registration.
For a custom setup directory, store it at `/your/private/path/tunnel-key` and add
`DOTS_PHONE_TUNNEL_KEY_FILE="/your/private/path/tunnel-key"` to that directory's
config.env. Continue using that same config.env with Compose's `--env-file`.

This OpenAI tunnel carries private MCP traffic. It is separate from the public
owner and media tunnels above, whose provider you choose. Cloudflare is one
possible public-ingress provider, not a required account; its credentials cannot
replace the OpenAI tunnel credentials. Secure MCP Tunnel supports private
connections, not public plugin marketplace submission.

Add `DOTS_PIPECAT_TUNNEL_ID` and `DOTS_PHONE_MCP_RESOURCE_URL` to config.env,
along with the exact owner origin and OAuth callback URLs discovered during
registration. The tunnel client uses `http://phone-app:8000/mcp` on the local
Docker network and allows metadata discovery for that exact private host.

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml --profile connection up --build -d
```

The registration supplies an `asdk_app_...` ID. Build your private install package:

```sh
uv run python -m dots_phone.setup package --app-id asdk_app_YOUR_REGISTRATION --directory "$HOME/.local/share/dots-phone-install"
codex plugin marketplace add "$HOME/.local/share/dots-phone-install"
codex plugin add dots-phone@dots-phone --json
```

Refresh the plugin directory/restart the client, install the package and complete
OAuth using your owner password. The generic repository template has no app
mapping and is not a connected installation by itself. A package can alternatively
use `--mcp-url https://your-local-tunnel.example/mcp` when the host supports direct
HTTP MCP and your tunnel deliberately routes that endpoint; the default owner/media
gateways do not. That option still connects to your machine, not a central service.

The generated package follows the official
[plugin packaging format](https://developers.openai.com/plugins/build/plugins).
Create the tunnel in Platform and register the plugin in ChatGPT; local setup
cannot mint these credentials or claim an install succeeded before you link it.

## Verify setup without dialing

Keep `DOTS_PHONE_DRY_RUN=true`. Check the local gateways and application:

```sh
curl -fsS http://127.0.0.1:8852/healthz
curl -fsS http://127.0.0.1:8851/healthz
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml exec -T phone-app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read().decode())"
uv run --no-sync python -m dots_phone.checks offline
```

After OAuth linking, confirm tool discovery and call `get_phone_workflow`, which
returns the packaged instructions without starting a call. Successful health
checks, offline tests and linked-tool access establish no-dial setup readiness.
They do not establish paid model access, funded carrier routing or audio quality.
Do not invoke `start_call`, paid evaluations or live test flags as part of this check.

## Keep the installed skill current

The generated plugin includes the phone skill and its references. The MCP server
also advertises the same packaged files through skills/list, skills/get and
resources/read, with SHA-256 hashes for import verification. It serves only that
static bundle, never local credentials or call data.

Editing the checkout does not update a cloud plugin's installed skill. For a
personal cloud plugin, upload a new plugin ZIP through its “Upload new version”
action, retaining your own app mapping. For server-based skill import, deploy the
updated image, run Scan Tools and review the imported skill before updating the
plugin version. Imports are snapshots. Verify the installed skill's metadata
version and content against the package; do not infer an update from working
phone tools alone. See the official
[skill import documentation](https://developers.openai.com/plugins/build/mcp-server#import-skills-from-the-mcp-server).

Some hosts can display an installed skill but fail to load its package at runtime.
In that case, get_phone_workflow returns the same bundled skill and reference
through the authenticated phone connection, including version and file hashes.
It accepts no input and exposes no call data or credentials. Verify and report
which delivery path worked; a successful fallback does not prove the host's
native skill loader works. No email connection is required by either path.

## Enable calls and optional sources

Confirm the linked tools work and keep dry-run on until the caller and provider
credentials are correct. Call-event subscriptions are optional with the independent
Codex worker. When you explicitly choose to enable real calls, set
DOTS_PHONE_DRY_RUN=false in config.env and recreate phone-app. This permits the
runtime to dial. Generated installations use
`DOTS_PHONE_CALL_AUTHORIZATION_MODE=connected_owner`: the linked
owner OAuth connection can authorize the exact prepared task without a per-call
password page. Dots prepares and starts within that scope; autonomous calls use the registered
Codex worker without requiring native Dots decision tasks.
The default connected limits are $3 per call and $10 reserved over a rolling
24 hours, configurable with `DOTS_PHONE_CONNECTED_CALL_BUDGET_USD` and
`DOTS_PHONE_CONNECTED_DAILY_BUDGET_USD`. Reservations are committed atomically
when starting, survive restarts/retention and are not refunded after uncertain
dialing. Preparation does not dial or consume that spending reservation.
Existing deployments can explicitly select this mode in their private environment;
`owner_page` retains the legacy per-call page when deliberately selected.
OAuth linking still uses the installer's owner password once, and revoked/expired
phone connections must reconnect normally. Host confirmation settings still apply.
Keep one instance on the database. Do not stop containers during an active call.

No Gmail, Yahoo or other source plugin is required to use Dots Phone. Dots starts
with supplied facts and can ask for missing details. Connect additional sources
separately if you want Dots to consult them; it uses only available authorized
connections. To use the optional Node relay, install Node and Codex, sign Codex
in to the same ChatGPT account, and verify Dots Phone is callable. Mail connectors
are discovered when present; zero connected mailboxes also works, with explicit
unavailable responses for mail requests. Its Gmail identity is discovered
from the authenticated profile; an optional private gmail_account pin can further
restrict it. Yahoo alone also works. Private exclude_terms defaults to an empty
list; set your own exclusions rather than distributing them in the skill.

No one else's credentials, mailbox, company exclusions or cloud registration are
included. Offline tests and dry-run make no provider calls. Live text evaluations
incur Realtime text API charges, without audio or carrier usage. Actual model
availability, funded carrier routing, OAuth and notifications must be verified in
your own installation before relying on a live call.

Standard setup enables `DOTS_PHONE_PIPECAT_ACCOUNT_DTMF=true` for heard menu
choices and disclosed numeric verification facts within the account task scope.
Preparation reports the actual `interaction_capabilities` and warns if keypad
controls are disabled; saying a digit aloud cannot replace a requested key press.

Normal calls use `DOTS_PHONE_DESTINATION_POLICY=task_scope`; Dots verifies the
recipient from the task and binds its exact number into the call authorization.
No per-number allowlisting is required. The current calling format is US/Canada
E.164. `allowlist` is an optional restricted deployment policy;
`DOTS_PHONE_ALLOWED_TEST_NUMBERS` also remains the owned test campaign registry.
