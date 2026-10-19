# Licenses and third-party code

Dots Phone's own code and documentation use the [MIT License](../LICENSE).
That license does not replace the terms for vendored code, installed dependencies,
container base images, or the services you connect. The Python distribution's
`MIT AND Apache-2.0` metadata covers its bundled code and schemas.

| Included material | License and retained attribution |
| --- | --- |
| Owner-password parameter choices adapted from Agent Call | MIT; the original copyright and permission notice are in [NOTICE](../NOTICE). |
| OpenAI voice evaluation harness in `vendor/voice_evals` | MIT; [upstream license](../vendor/voice_evals/LICENSE) and the original notice in [NOTICE](../NOTICE). |
| Agent Plugins 1.0.0 manifest and MCP schemas | Apache-2.0; [license copy](../vendor/agent-plugins-LICENSE) and source attribution in [NOTICE](../NOTICE). Both files are unchanged from upstream commit `ff8ab5e392cc87bd88d87c060815a87490e51003`. |
| OpenAI tunnel-client, downloaded when building the connection container | Apache-2.0 with bundled dependency notices. The image retains the release's `LICENSE`, `NOTICE`, dependency license report, and SPDX inventory in `/usr/local/share/licenses/tunnel-client`. |

Schema licensing comes from the [Agent Plugins licensing policy](https://github.com/agentplugins/agent-plugins-spec/blob/ff8ab5e392cc87bd88d87c060815a87490e51003/LICENSE.md).
The connection image uses the checksum-verified [tunnel-client v0.0.16 release](https://github.com/openai/tunnel-client/releases/tag/v0.0.16).

## Installed dependencies

The lockfiles record dependency versions; they do not grant permission to
redistribute those packages. Python packages, native libraries, and container
base images retain their upstream licenses and notices. Their installed license
files are kept in the environments and images. Dependencies with LGPL or MPL
terms remain subject to those terms, including any applicable source or
relinking requirements when redistributing binaries or modified versions.

Examples in the locked environments include `num2words` 0.5.14 and `soxr` 1.0.0
(LGPL), the `libsndfile` library used by `soundfile` 0.13.1 (LGPL), and `certifi`
2026.7.22 (MPL-2.0). The separate experimental environment also uses `orjson`
3.13.0 (MPL-2.0 and Apache-2.0 or MIT) and PyAV 16.1.0, whose bundled FFmpeg
libraries need their own redistribution review.

Bitwarden's Secrets Manager SDK 2.1.0 is an optional `bws` extra with separate
[vendor terms](https://github.com/bitwarden/sdk-sm/blob/main/LICENSE).
The default install and application image do not include it. The project's MIT
license does not grant permission to redistribute that SDK.

Before shipping a binary, container, or downstream package, review the licenses
of its actual contents and retain the required notices and source availability.
An inventory of the source repository alone does not cover everything added by
a dependency installer or container build.

The optional DeepFilterNet image builds the native C API from
[upstream v0.5.6](https://github.com/Rikorose/DeepFilterNet/tree/978576aa8400552a4ce9730838c635aa30db5e61),
under MIT/Apache-2.0. Its notices remain in `/opt/deepfilter/licenses`. The build
pins the upstream commit, source archive checksum, compiler image and upstream
Cargo lockfile. Build metadata selects shared-library output, disables the
unused embedded model, and aligns C API inference thresholds with the evaluated
CLI defaults (-15/35/35 dB). No upstream implementation is vendored in this repository.
Explicitly installed, checksum-verified model weights live in an external cache
with provenance and notices, and are mounted read-only. The ordinary image does
not include the native library or model weights. The Python `deepfilter` extra
contains only NumPy and soxr for the adapter's streaming resampling.
