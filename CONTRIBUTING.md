# Contributing

Start with a small issue describing the problem or proposed improvement. For
bugs, use fictional details and the [report template](https://github.com/gabeosx/dot-phone-plugin/issues/new/choose).
See [support](docs/SUPPORT.md) for troubleshooting and
[private security reporting](.github/SECURITY.md) for vulnerabilities.

## Local checks

```sh
bash scripts/install-hooks.sh
bash scripts/ci.sh
```

All CI must pass locally before every push to GitHub. The pre-push hook reruns
the same acceptance script as GitHub Actions against clean, committed HEAD.
It blocks pushes on failures or missing prerequisites. Never bypass the hook or
push a candidate to discover locally reproducible failures. Install the hook in
each clone/worktree; Git does not install hooks automatically after cloning.

The full suite requires uv, Node, Docker Compose, network access for dependency
downloads and all four live advisory audits, and Linux amd64 container support.
Commit changes before running the suite. It validates the exact commit and its
complete ancestry in an isolated checkout, leaving unrelated private backup refs
and ignored state out of the candidate. Reports stay in ignored
`artifacts/local-ci/<commit>-<run>/`. These checks use fictional fixtures and
mocks. They do not dial, query an inbox or need provider credentials.
Some local socket checks need loopback access.
See [validation](docs/VALIDATION.md) for optional native checks and known limits.
See [dependency maintenance](docs/DEPENDENCY-MAINTENANCE.md) for the weekly
upgrade and validated release process.

Keep changes focused, add meaningful regression coverage for behavior changes,
and update affected setup or operations guidance. A pull request should explain
the user-visible problem, resulting behavior and validation performed.

Never commit secrets, personal information, actual-call contents, recordings,
transcripts, derived replay fixtures or private operator reports. Follow the
[public source boundary](docs/PUBLIC-RELEASE.md). Contributions are under the
project's [license](LICENSE); retain third-party attribution.
