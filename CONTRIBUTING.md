# Contributing

Start with a small issue describing the problem or proposed improvement. For
bugs, use fictional details and the [report template](https://github.com/gabeosx/dot-phone-plugin/issues/new/choose).
See [support](docs/SUPPORT.md) for troubleshooting and
[private security reporting](.github/SECURITY.md) for vulnerabilities.

## Local checks

```sh
uv sync --frozen --extra pipecat --python 3.13
uv run python -m dots_phone.checks offline
node --test experiments/pipecat/tests/*.test.mjs experiments/pipecat/test_automatic_call_worker.mjs
```

These checks use fictional fixtures and mocks. They do not dial, query an inbox
or need provider credentials. Some local socket checks need loopback access.
See [validation](docs/VALIDATION.md) for optional native checks and known limits.

Keep changes focused, add meaningful regression coverage for behavior changes,
and update affected setup or operations guidance. A pull request should explain
the user-visible problem, resulting behavior and validation performed.

Never commit secrets, personal information, actual-call contents, recordings,
transcripts, derived replay fixtures or private operator reports. Follow the
[public source boundary](docs/PUBLIC-RELEASE.md). Contributions are under the
project's [license](LICENSE); retain third-party attribution.
