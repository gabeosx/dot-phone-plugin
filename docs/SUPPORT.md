# Troubleshooting and support

Dots Phone is an experimental, self-hosted preview. Start with the
[quickstart](QUICKSTART.md); use [local setup](LOCAL-SETUP.md) for registration and
[operations](RUNBOOK.md) for call state and recovery. Keep dry-run enabled while
diagnosing installation problems. A health check is not a real-call test.

Start with the [read-only setup diagnostic](LOCAL-SETUP.md#verify-setup-without-dialing).
It reads the same Compose configuration as startup and returns an actionable
checklist without printing credential or path values. Include `--running` to
check local containers and the host worker; it makes no model, mailbox or phone
requests. A passing local checklist does not establish a successful phone task.

## Common setup problems

| Symptom | Check and next step |
| --- | --- |
| Custom connection or tunnel registration is unavailable | Confirm your own account supports private custom MCP/plugin registration and Secure MCP Tunnels. These are required for the default path. Offline checks still work without registration. |
| Setup says the destination exists | Reuse that installation or select a new private directory. Do not delete credentials or regenerate the encryption key to retry setup. |
| Docker cannot read `runtime.json`, the tunnel key or weights | Use the same private `--env-file` as initialization. Check that its paths exist and are accessible to Docker. Quote paths containing spaces; keep the original file permissions. |
| A local gateway health check fails | Check Docker is running and inspect service status with the command below. Verify the local ports before investigating public tunnels. |
| Local health works, but OAuth or the connection fails | Check exact owner origin, resource URL, callback URLs, tunnel ID and registration. Owner and media gateways deliberately exclude `/mcp`; the default MCP connection uses the separate Secure MCP Tunnel. |
| The host worker is unavailable or stale | Check `codex login status`, restart the host worker using the matching private reasoner directory, then run the [setup diagnostic](LOCAL-SETUP.md#verify-setup-without-dialing). The worker needs ChatGPT sign-in. |
| The phone skill is missing or stale | Regenerate and reinstall the private package with your registration. Verify its version; checkout changes do not update installed snapshots. `get_phone_workflow` is the authenticated static fallback. |
| A source tool is unavailable | Inspect the signed-in worker's actual connections. The chat and worker can have different tools. Connect the intended read tool or deliberately choose supplied facts only (`source_tools: []`); never claim a lookup ran. |
| DeepFilter build or native checks fail | Follow [the filter guide](DEEPFILTER.md) and retain the error. Use an explicitly chosen unfiltered configuration if needed; a flag alone does not establish filtering. |
| Optional local audio review fails setup checks | Verify the [external model files and read-only mounts](LOCAL-SETUP.md#optional-local-audio-review). Recording retention is required; preserve an opt-out by disabling the optional overlay instead of silently enabling retention. |
| A spoken value and transcript disagree | Keep the result unresolved unless its evidence supports the full value. Local recognizers and model agreement can still be wrong; do not replace an uncertain amount with a guess. Check the configured transcription model and preserve the private evidence for local diagnosis. |
| A model or carrier request fails | Check the configured model is available to your account and the relevant provider account is funded. Health checks do not validate paid access. A live test needs its own recipient, purpose and spending authorization. |
| A call start is ambiguous | Retrieve the current call state and reconcile it before any retry. Do not redial automatically or reset budget reservations. |

For the recommended filtered setup, inspect local status without rendering the
environment or secret files:

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml --profile connection ps
```

Use your actual private environment path. Omit the filter overlay only if your
installation intentionally runs unfiltered. Review service logs locally when
needed; do not upload raw logs or configuration with a report.

## Report a reproducible issue

Use [GitHub Issues](https://github.com/gabeosx/dot-phone-plugin/issues) for setup
bugs, reproducible failures and feature requests. Public reports should contain:

- Source revision and plugin version, operating system, CPU architecture, and
  Docker/Node/uv/Codex versions.
- The setup step that failed, expected behavior and the exact error after local
  review and removal of private values.
- Whether the issue occurs with dry-run and independently authored fictional
  fixtures, plus the smallest reproduction that demonstrates it.

Keep provider keys, passwords, account identities, phone numbers, email contents,
call identifiers, private URLs, recordings, transcripts and actual-call summaries
out of public reports. Do not attach `runtime.json`, `config.env`, the database,
the reasoner directory, personal plugin package or raw diagnostic exports. Describe
a failure using a new fictional example rather than rewriting a private call.

## Security reports

Report a suspected vulnerability through the repository's
[private vulnerability reporting form](https://github.com/gabeosx/dot-phone-plugin/security/advisories/new)
(**Security → Report a vulnerability**). Keep credentials and private call records
out of the initial report. Do not publish an issue containing a working credential.
Revoke any exposed credential in its provider account and review the installation
before re-enabling calls.

Community preview support has no promised response time. The project currently
supports the documented single-owner local deployment; unfamiliar phone systems,
unavailable source data and carrier behavior can leave tasks unresolved. See
[validation and limits](VALIDATION.md) before relying on a result.
