# Recommended DeepFilterNet3 audio preprocessing

DeepFilterNet3 runs locally on incoming audio before native Realtime VAD and
the audio model. It needs no recipient sample, enrollment, denoising API or
license key. **Enable it for normal setup**, unless you prefer unfiltered input.
The bare runtime defaults off until the native image and external weights are
provisioned; the setup skill performs those steps before activation. Vocal music
can still be mistaken for speech, so filtering supplements the agent's reasoning
rather than deciding whose words to obey.

Only our adapter, configuration and dependency identities live in Git. The
optional Docker image builds upstream's native C API from pinned v0.5.6 source,
its Cargo lockfile and a pinned compiler image. Unused embedded weights are
disabled. The final image includes the library and its notices; explicit model
weights are mounted read-only from an external cache. Calls never download or
write audio/model assets.

The build explicitly aligns the C API's SNR thresholds with the evaluated CLI
defaults (-15/35/35 dB), rather than using its different default thresholds.
Attenuation defaults to 100 dB and the optional post-filter is off.

## Install and verify without dialing

Create the private configuration using [local setup](LOCAL-SETUP.md) first.
Keep `DOTS_PHONE_DRY_RUN=true`. Use that same configuration file for every Compose
command below; replace `.secrets/config.env` consistently for a custom location.
No API or phone calls are needed for this procedure.

Explicitly install checksum-verified weights to an external cache:

```sh
model_file="$(uv run python -m dots_phone.deepfilter_setup)"
printf '%s\n' "$model_file"
```

The printed absolute path defaults to
`~/Library/Caches/dots-phone/deepfilter/v0.5.6/DeepFilterNet3_onnx.tar.gz` on macOS,
or `${XDG_CACHE_HOME:-~/.cache}/dots-phone/deepfilter/v0.5.6/DeepFilterNet3_onnx.tar.gz`
on Linux. `--directory` accepts another external location; Git checkouts are
refused. The installer retains provenance and upstream license notices. Keep the
`model_file` shell variable for the verification command below.

In the private `config.env`, set the following values, replacing the model path
with the actual printed absolute path. Preserve the file's existing entries and
owner-only permissions. Quote a path containing spaces.

```dotenv
DOTS_PHONE_PIPECAT_INPUT_FILTER=deepfilter
DOTS_PHONE_DEEPFILTER_MODEL_FILE='/absolute/external-cache/DeepFilterNet3_onnx.tar.gz'
```

Build the ordinary image first, then the native filter image. Neither command
starts or replaces a service:

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml build phone-app
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml build phone-app
```

Run the actual native streaming checks in isolation. This temporary container has
no network, provider credentials or call-state mount; it uses only the external
weights read-only. Its test reports are temporary, and no audio files are saved:

```sh
docker run --rm --pull never --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v "${model_file}:/models/deepfilter.tar.gz:ro" \
  -e DOTS_PHONE_TEST_DEEPFILTER_MODEL=/models/deepfilter.tar.gz \
  -e DOTS_PHONE_TEST_DEEPFILTER_LIBRARY=/opt/deepfilter/lib/libdf.so \
  --entrypoint python dots-phone-deepfilter:local \
  -m pytest -q -p no:cacheprovider \
  tests/test_deepfilter_filter.py tests/test_deepfilter_placement.py tests/test_deepfilter_pipeline.py
```

These checks cover the library/weights, causal streaming, failure bypass, keypad
stream replacement and closure with simulated peers. They do not measure an IVR's
recognition quality or prove the recipient will hear every word. If installation
or verification fails, retain dry-run, report the failure and use an explicitly
chosen unfiltered fallback; do not describe filtering as ready from its flag alone.

## Activate and verify configuration

For a new installation, return to [the quickstart](QUICKSTART.md#3-connect-the-local-service)
to start the host worker, gateways and filtered service. For an existing service,
wait until active calls close before recreating it:

```sh
docker compose --env-file .secrets/config.env -f compose.local.yaml -f compose.deepfilter.yaml up -d phone-app
```

Keep `-f compose.deepfilter.yaml` on subsequent startup, recreation, connection and
inspection commands while filtering is enabled. An explicit `off` in the selected
configuration takes precedence over the overlay's default. Confirm the effective
phone-app filter value, `/models/deepfilter/DeepFilterNet3_onnx.tar.gz` read-only bind
and healthy service without dumping its full environment or runtime secrets.
The overlay mounts only the model file; native VAD, voice prompts, output audio,
reasoning settings and approval/budget controls are unchanged. The library path
is `/opt/deepfilter/lib/libdf.so`. Other installations need a matching library
and `uv sync --extra pipecat --extra deepfilter`, with library/model paths set
explicitly. A health check alone does not exercise native processing; the isolated
checks above do. On an authorized call, `usage.input_audio_filter` establishes
whether that call actually used filtering or bypassed it.

## Stream behavior and failures

Each call has a separate native worker and resampler state. Mono PCM is converted
to 48 kHz and processed in causal 10 ms hops. The state persists across keypad
carrier-stream replacement and speaker changes; no speaker is enrolled or locked.
Workers receive no provider credentials. Processing runs in an isolated child
because upstream native errors can abort a process.

The adapter adds **50 ms** of signal delay: the model's 30 ms plus 20 ms of packet
alignment. It preserves the sample count of every input packet, including
irregular packet sizes. No audio is sped up or flushed after closure. Only a
short rolling input/output buffer is retained in memory.

Initialization is bounded to 10 seconds. Native processing transactions have a
100 ms deadline, configurable with `DOTS_PHONE_DEEPFILTER_FRAME_TIMEOUT_MS`.
A crash, stall, invalid output or disable control kills the worker and latches
raw bypass for the rest of that call. Bypass retains the same delay so switching
does not drop or replay part of a word. Re-enable on the next call, without
resetting an active Realtime session. Missing libraries or invalid/missing weights
are refused before dialing. Worker startup and processing failures preserve the
call through bypass.

`usage.input_audio_filter` records active/bypassed/stopped status, failure type,
model hash, delay, sample count and maximum processing time. It records no audio.

## Rollback

Set `DOTS_PHONE_PIPECAT_INPUT_FILTER=off` and restart after active calls finish.
This works with the optional image: no filter library/model is loaded, no worker
starts and no filter delay is added. The overlay still requires its bind-mounted
file to exist. To remove that requirement and the dependency, recreate the service
using only `compose.local.yaml` with the ordinary image and the same private
`--env-file`. No data migration is needed.

## Tests and private assets

Unit tests generate PCM in memory. Opt-in native integration tests use explicit
external weights and a simulated carrier/model, with no API calls:

```sh
DOTS_PHONE_TEST_DEEPFILTER_MODEL=/absolute/cache/DeepFilterNet3_onnx.tar.gz \
DOTS_PHONE_TEST_DEEPFILTER_LIBRARY=/opt/deepfilter/lib/libdf.so \
pytest -q tests/test_deepfilter_filter.py tests/test_deepfilter_placement.py tests/test_deepfilter_pipeline.py
```

Keep private replay assets/results outside the checkout, selected through
`DOTS_PHONE_TEST_ASSETS`. They are optional for installation and ordinary CI.
They must not be copied into container images or source distributions.
Frozen audio regression fixtures use the external root's `test-fixtures/`
directory; their descriptive manifests remain in `tests/fixtures`. Tests which
need that media skip explicitly when it is unavailable, without downloading it.
