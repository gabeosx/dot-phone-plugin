# Set up Dots Phone

Use the [Codex setup prompt](../README.md#set-up-with-codex) to work through this
with an assistant, or follow the steps below. The result is a service on your own
computer connected to your own accounts. Keep the computer and its tunnels running
while using it. The repository's [setup skill](../.agents/skills/dots-phone-setup/SKILL.md)
helps an LLM carry out this guide; it is available before the phone plugin is
installed. In Codex you can invoke `$dots-phone-setup` from the checkout, or ask
any capable coding assistant to read that file directly.

## Install these tools

| Required tool | Version / setup | Official download or guide |
| --- | --- | --- |
| Git | For cloning the project. | [Git downloads](https://git-scm.com/downloads/) |
| Docker with Compose | Docker Desktop includes Compose; start Docker before setup. | [Docker Compose installation](https://docs.docker.com/compose/install/) |
| uv | Installs Python **3.13** and the repository’s locked Python dependencies. | [Install uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Node.js | Use the current LTS release. The project does not declare a minimum Node version. | [Node.js downloads](https://nodejs.org/en/download) |
| Codex CLI | Use the current CLI and sign in with **ChatGPT**. API-key sign-in does not support this worker. | [Install Codex](https://developers.openai.com/codex/cli/) · [Sign-in options](https://developers.openai.com/codex/auth/) |

The containers pin Python 3.13.15 and uv 0.10.6. There is no separate database to
install; the service uses local SQLite.

## Get your accounts and credentials

| Required item | What to obtain | Where to get it |
| --- | --- | --- |
| OpenAI API project | An API key, funded API billing, and access to the configured Realtime model. | [API keys](https://platform.openai.com/api-keys) · [API billing](https://platform.openai.com/settings/organization/billing/overview) |
| Twilio account | Your **Account SID** and **Auth Token**. | [Find them in Twilio Console](https://help.twilio.com/articles/223136027-Auth-Tokens-and-How-to-Change-Them) |
| Twilio API key | A **Standard API key SID** and its **secret**. Save the secret when creating the key; it is shown once. | [Create an API key](https://www.twilio.com/docs/iam/api-keys/keys-in-console) |
| Twilio phone number | An owned, voice-capable US/Canada number, written in `+1` format. Use a funded account for ordinary calls. | [Twilio phone numbers](https://www.twilio.com/docs/phone-numbers) · [Trial limits](https://www.twilio.com/docs/usage/trials/try-out-voice) |
| ChatGPT account | Access to Codex and custom MCP/plugin registration. These features must be available in your account. | [Connect a custom MCP server](https://developers.openai.com/plugins/deploy/connect-chatgpt) |
| Secure MCP Tunnel | Your own tunnel ID, runtime key, and exact resource URL. The default connection uses a private, developer-mode registration. | [Tunnel settings](https://platform.openai.com/settings/organization/tunnels) · [Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) |
| Local owner password | Create a new password of at least **20 characters** during setup. | The local setup wizard below. |

The wizard collects exactly five provider values: `OPENAI_API_KEY`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY` and `TWILIO_API_SECRET`.
Your phone number and tunnel configuration are entered separately. The secure
tunnel key needs the permissions described in the tunnel guide; it is not a
credential issued by your public tunnel provider.

| Optional connection | When you need it |
| --- | --- |
| Gmail, Yahoo Mail or another read-tool plugin | When you want Codex to retrieve information during a call. Connect it to the signed-in worker and select its allowed read tools. No mailbox connection is needed for supplied facts. |
| Bitwarden Secrets Manager | If you prefer it to the default local secrets file. Its SDK is excluded from the default install; optional secret creation uses the explicit `bws` extra under [vendor terms](https://github.com/bitwarden/sdk-sm/blob/main/LICENSE). See [the runbook](RUNBOOK.md#optional-bitwarden). |

**Costs:** Realtime uses OpenAI API billing, even when Codex starts the request.
Codex reasoning uses your ChatGPT plan’s Codex allowance. Twilio charges for its
number and phone usage; your tunnel provider may also charge. [ChatGPT and API
billing are separate](https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform).

## Set it up without making a call

Run commands from the repository root unless noted otherwise.

### 1. Get the code and sign in

```sh
git clone https://github.com/gabeosx/dot-phone-plugin.git
cd dot-phone-plugin
uv sync --frozen --extra pipecat --python 3.13
codex login
codex login status
```

If you already have a checkout, inspect and reuse it rather than overwriting it.
Choose ChatGPT sign-in for Codex. The separate OpenAI API key is for Realtime.

### 2. Create the private configuration

```sh
uv run python -m dots_phone.setup init --origin https://your-owner-tunnel.example
```

Replace the example with your owner HTTPS origin when available. Enter provider
secrets and the new owner password at the hidden terminal prompts, never in chat
or a shell command. Setup creates `.secrets/runtime.json` and `.secrets/config.env`
with owner-only permissions, generates the encryption key, and hashes the owner
password. It does not contact a provider or dial a number.

The destination must be new. If `.secrets/` already exists, follow
[local setup](LOCAL-SETUP.md) to use a different private directory; do not delete
existing credentials. Back up the generated encryption key privately.

Keep `DOTS_PHONE_DRY_RUN=true`. Add your own number, origins and registration values
to the private configuration as described in [local setup](LOCAL-SETUP.md).

For a new install, **enable the recommended DeepFilterNet3 filtering**. Complete
[install and verify without dialing](DEEPFILTER.md#install-and-verify-without-dialing)
before starting the service below. That procedure downloads verified weights to
an external cache, builds the native image, checks streaming locally and sets the
filter in the same private configuration. It requires no recipient sample or
paid denoising service. If you choose unfiltered input, set
`DOTS_PHONE_PIPECAT_INPUT_FILTER=off` and omit `-f compose.deepfilter.yaml` from
the following commands. Report a failed filter prerequisite rather than silently
claiming the recommended setup is ready.

### 3. Connect the local service

The default installation has three network connections:

| Connection | Where it goes |
| --- | --- |
| Owner HTTPS origin | A public tunnel to `127.0.0.1:8852` for owner login and OAuth. |
| Media HTTPS/WSS origin | A public tunnel to `127.0.0.1:8851` for Twilio callbacks and phone audio. It must support WebSockets and preserve Twilio’s signature header. |
| Secure MCP Tunnel | Connects ChatGPT to `http://phone-app:8000/mcp` inside Docker, using your registration. |

Choose a compatible provider for the first two tunnels. These are separate from
the OpenAI Secure MCP Tunnel. Neither public gateway exposes `/mcp`. If you have
not obtained the public origins yet, the [local setup guide](LOCAL-SETUP.md)
explains how to start the gateways first and update the placeholders afterward.

Start the host reasoning worker and keep it running:

```sh
node experiments/pipecat/codex_call_reasoner.mjs serve "$PWD/.secrets/reasoner"
```

In another terminal, start the service:

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml up -d phone-app owner-ingress media-ingress
```

Then follow [Connect ChatGPT](LOCAL-SETUP.md#connect-chatgpt-to-the-local-mcp-server)
to configure the secure tunnel, generate your personal plugin package and link it
with the owner password. Registration supplies your own app ID and exact callback
URLs. The repository’s generic plugin template is not a connected installation.
Keep the tunnel key in the private file specified by that guide, with mode `0600`.

### 4. Verify and stop

Check the local gateways:

```sh
curl -fsS http://127.0.0.1:8852/healthz
curl -fsS http://127.0.0.1:8851/healthz
```

Check the application inside its container:

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml exec -T phone-app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
```

After linking, discover the phone tools and use `get_phone_workflow` to check the
bundled workflow. It returns static instructions, not call records. Do not invoke
`start_call` as a setup check. You can also run the offline acceptance tests:

```sh
uv run --no-sync python -m dots_phone.checks offline
```

Stop here with dry-run enabled. These checks establish local health and connection
readiness; they do not prove that paid models, carrier audio or mailbox retrieval
work. Record anything still unverified. Before a real call, explicitly approve its
recipient, purpose and budget, then follow
[Enable calls](LOCAL-SETUP.md#enable-calls-and-optional-sources).

After owner linking and enabling calls, a normal task request authorizes the
bounded call; there is no new password page for each call. Dots prepares the brief
and checks its scope before dialing. Normal calls do not require a per-number
allowlist. See [operations](RUNBOOK.md) for steering, recovery and retention.

## Developer checks

The [validation record](VALIDATION.md) summarizes current results and limits.
The detailed evaluation commands below are for development, not initial setup.

<details>
<summary>Evaluation commands, scenario coverage and paid-test limits</summary>


See the [operations guide](RUNBOOK.md) for the runtime architecture and configuration.

```sh
uv run python -m dots_phone.checks offline
node --test experiments/pipecat/tests/*.test.mjs experiments/pipecat/test_automatic_call_worker.mjs
uv build
```

Offline tests use fictional data and mocks, with no provider calls. They verify
protocols and controls, not arbitrary-call reliability. The system is experimental;
source availability, recipient verification and network access affect outcomes.
See [operations](RUNBOOK.md) for call supervision, recovery and retention.

Text evaluation comes before audio or phone calls:

```sh
eval_root="$HOME/.local/share/dots-phone/test-assets/text-evals"
uv run python -m dots_phone.text_eval --output "$eval_root/offline.json"
uv run python -m dots_phone.text_eval --live --codex-eval-worker --runtime-file .secrets/runtime.json --repeat 3 --jobs 3 --output "$eval_root/ivr-text.json"
```

The first command validates fixtures and contracts offline. The second sends
fictional conversations to the same Realtime model in text mode. The 60 scenarios
cover 20 domains: rewards, retail, delivery, billing, subscriptions, telecom,
utilities, travel, hospitality, appointments, pharmacy, insurance, repairs,
warranties, home services, banking, libraries/public services, government,
education and events. Goals include status, eligibility, fees, dates, amounts,
locations, policy terms and outstanding documents, within the current read-only
inquiry scope. Menu navigation, question order, privacy and human fallback remain
covered by the original regression cases.

Twelve AI IVRs react to actual caller speech and keypad output. Their private
verification records and result text never enter the caller prompt. A deterministic
check rejects simulated verification or result release unsupported by caller output.
The simulator must request verification before taking an unavailable branch;
its private expected outcome cannot substitute for the caller's actual response.
The isolated signed-in Codex reasoner can call fictional lookup tools covering
success, timeout/retry, delay, unavailable access, permission denial, stale records
and conflicting matches. Real apps, code-execution MCP servers and shell tools are
disabled in these evaluation workers. Neither simulated IVRs nor lookup tools can
change a real account.
Live text runs incur Realtime text API charges; offline validation makes no
provider calls. Signed-in Codex reasoning uses your plan's Codex allowance, while
Realtime remains separately API-billed in both text and audio modes.
Three repetitions are the default. Use `--case ID`, `--suite regression`,
`--suite customer-service` and `--split regression|holdout` to select coverage.
Keep the nine remaining holdout scenarios out of prompt tuning, then run them against a frozen
candidate. Keep evaluation inputs and outputs local; the repository contains
fictional scenarios and reusable harnesses.
Reports preserve every trial, generated recipient utterance, source
tool result, final report, code/scenario hashes, text token usage and latency.
They report both trial passes and scenarios passing every planned repeat; failed
or incomplete trials are never silently retried or discarded. Realtime API costs
are estimated from observed text token counts; Codex allowance use is separate.
Billing errors stop new trials and leave a checkpoint. After restoring funding,
explicitly resume with the same model, backend and scenario/task contracts:

```sh
uv run python -m dots_phone.text_eval --live --codex-eval-worker --runtime-file .secrets/runtime.json --repeat 3 --jobs 3 --resume-from "$eval_root/ivr-text.json" --output "$eval_root/ivr-text-resumed.json"
```

Resume retains completed successes and behavior failures. Infrastructure failures
are retried and their original traces remain in `previous_attempts`; this is not
a way to replace a failing behavior trial with a passing one.
Keep these evaluation reports outside the checkout, as selected above; a failed
scenario exits nonzero. Text tests do not measure hearing,
speech timing, interruptions, DTMF delivery or carrier reliability.

For one bounded audio smoke test, prepare prerecorded IVR prompts locally on macOS.
Choose a new output directory outside the checkout for each run; it contains audio
and transcripts. This portable example can be replaced with another external path:

```sh
uv run python -m dots_phone.realtime_audio_smoke --output "$HOME/.local/share/dots-phone/test-assets/audio-smoke"
```

After approving the test budget, add `--live --runtime-file .secrets/runtime.json`
to run the exchange through the production Realtime voice factory and production
Codex call-agent lifecycle, with connected sources disabled. It prepares one
ephemeral agent before the audio session opens and reuses that thread for every
consultation and final review. Reports retain thread identities and reuse counts.
The harness permits one attempt per output directory,
with a 90-second limit, 512 output tokens per response, a response-count limit,
and a $1 usage guard reserving $0.25 for in-flight responses and transcription.
This local usage estimate is not a provider-enforced billing limit. It saves the
played audio, transcripts, source hashes, outcome and observed usage. The keypad
recipient is simulated; carrier DTMF, reconnects, interruptions and arbitrary IVR
timeouts require separate validation. No carrier calls or real account reads occur.

For the original regression cases, `--codex-worker-directory` can use the production
signed-in reasoning daemon, with real account reads disabled in the briefing.
`--independent-reasoner` compares a direct Responses backend instead. Reactive IVRs
and fictional lookup cases require `--codex-eval-worker`; Node and Codex signed in
with ChatGPT are prerequisites.

The design follows the official [Realtime Eval Guide](https://developers.openai.com/cookbook/examples/realtime_eval_guide):
balanced outcomes, deterministic tool mocks, a versioned simulator and full traces.
The [customer support evaluation cookbook](https://developers.openai.com/cookbook/examples/agent_optimization/optimizing_agents_for_cost_and_quality)
also keeps deterministic failures authoritative when adding model-based assessments.

Do not commit credentials, account data, transcripts, recordings, personal source
exclusions or installation IDs. `.secrets/`, `data/` and `artifacts/` are ignored.
The committed plugin folder is a generic template; setup writes your registered
app mapping into a separate private install directory. See [privacy](PRIVACY.md)
for repository checks and history cleanup. Third-party notices remain in `NOTICE`.

</details>
