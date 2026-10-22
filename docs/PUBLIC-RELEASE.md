# Public release preparation

Dots Phone is an experimental, self-hosted preview. Publishing its source does
not provide a hosted calling service, provider credentials, or access to anyone's
mailbox. The landing page hosts an approved recording of an owned-number test using
fictional order details. Watching it does not place a call or access a mailbox.
Demo media and private preparation/call evidence remain outside the public source
and distributions.

## Public source boundary

Publish implementation code, setup documentation and independently authored
fictional tests. Keep actual-call contents, summaries, recipient quotations,
recorded timing windows, event identifiers, audio hashes, private replay fixtures,
operator diaries and internal experiment reports outside the public repository.
Anonymizing a report does not make it part of the public source.

Review all branches, tags, releases, distribution members and Actions artifacts.
A clean working tree does not establish clean history. For a public snapshot,
start from the reviewed export in a fresh repository; keep earlier development
history private. Do not push backup refs or merge private history into it.

Automated scans complement content review. Pattern matching cannot recognize
every private story or determine whether a test was copied from a real call.

## Rights and attribution

Dots Phone project code is licensed under the [MIT License](../LICENSE), included
in the first public commit and in both Python distributions. Existing third-party
rights and notices remain in [NOTICE](../NOTICE) and the corresponding vendored
license files. Separately installed dependencies retain their own licenses; the
project license does not relicense those packages or provider services. See
[dependency licenses](DEPENDENCY-LICENSES.md) for the bundled notices and optional SDK terms.

## What the testing establishes

The release checks exercise local orchestration, preparation documents, scoped
tool access, fast ordinary replies, optional Codex reasoning, result validation,
transcription-conflict handling, and packaging. Automated fixtures use synthetic
records. The public demo uses fictional order-confirmation context before the
call, then an actual owned-recipient IVR path and reviewed result. It does not
demonstrate a Gmail or mid-call lookup.

No-call model tests can exercise voice and reasoning behavior with prerecorded or
synthetic audio without dialing a recipient. They do not establish performance
over a carrier connection, successful completion with a real business, or
end-to-end access to a particular user's connected mailbox. Latency measurements
are observations from the stated test setup, not guarantees.

See [validation and limitations](VALIDATION.md) for the current evidence and
known limits. Further testing must retain explicit authorization for recipients,
mail sources, spending, and disclosure. Launch preparation does not authorize a
real phone call.

## Publication checklist

- Keep the original repository private and create the replacement repository as
  private while preparing its first commit.
- Export only reviewed source, synthetic tests/evals, public documentation,
  dependency locks, generic configuration, the project LICENSE, third-party notices, and the
  landing page's static files. Use a generic project contributor identity for
  the new initial commit.
- Exclude runtime secrets, environment files other than `.env.example`, databases,
  recordings/transcripts, artifacts, private research, local deployment identity,
  dependency caches, and the original `.git` directory. Refuse source symlinks.
- Run the source/history audit and a separate credential scanner. Inspect the
  exported file manifest, then scan the clean export again. The export includes
  Git-visible untracked source, so review additions as carefully as tracked files.
- Build the wheel and source distribution from the final source. Audit both
  archives, including their member paths and retained third-party notices.
- Run the documented offline tests and bounded no-call checks. Preserve failures
  and state the remaining limitations in the release notes.
- Check the website on desktop and mobile, including keyboard controls, reduced
  motion, and links. Keep the scenario clearly fictional and distinguish modeled
  steps from measured behavior.
- Push only the fresh snapshot to the new private repository. Verify its actual
  file tree, single intended history with LICENSE in its root commit, default branch, workflow permissions, and CI
  results before changing visibility.
- Review the exact repository and website that will become public. Publish only
  after the final source/export/package scans and release checks pass. Record the
  resulting URLs and visibility; do not imply that a prepared preview is already
  public.

## Repeating the snapshot audit

From the repository root, with the intended final files present:

```sh
python scripts/public_release_audit.py \
  --history \
  --report artifacts/release/audit.json \
  --export-dir artifacts/release/snapshot

gitleaks git --log-opts="--all --full-history" --redact=100
gitleaks dir artifacts/release/snapshot --redact=100
```

The export destination must be new. Findings prevent export. The report contains
file locations and hashes, not matched secret values. Add one `--distribution`
argument for each final wheel or source archive to audit package contents. A
private JSON denylist can be supplied with `--denylist`; it is never copied into
the snapshot.

This local history check does not discover GitHub-retained pull-request refs by
itself. For an existing repository, review those remote refs and other remote
surfaces separately before changing visibility. Keep raw audit inputs, reports,
and exports under ignored local artifacts; do not publish the audit directory.
