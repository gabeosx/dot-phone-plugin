# Repository privacy

The distributable source contains generic code, fictional fixtures and setup
instructions. Each installer's provider keys, owner password hash, encryption key,
app/tunnel registration, phone numbers, mailbox identity and source exclusions
belong in local ignored files or the generated private install package.

Keep call recordings, transcripts, account facts, mail, approval grants and
operator diaries outside tracked files. This also excludes actual-call summaries,
recipient quotations, measurements, event IDs, hashes, and sanitized reenactments
of private conversations. Changing names or removing an account number is not
enough to make a private-call report publishable. Use independently authored
fictional fixtures for public tests. Runtime objects are encrypted locally;
that does not make a Git commit safe to publish. Build artifacts must not include
`.secrets`, `data`, `artifacts` or developer installation mappings.

Run `python scripts/secret_scan.py` before committing. The repository also uses
Gitleaks on full Git history in CI. These scanners catch known patterns, not every
form of personal information; review tracked docs, examples, fixtures and generated
packages before publishing. Private denylist files can supplement the scan without
committing personal values.

Removing a value from HEAD leaves old commits readable. Cleanup must cover all
branches/tags and local checkpoint refs/reflogs, then be verified from a fresh
clone of the rewritten remote. A force-push does not erase other people's clones,
GitHub pull-request refs, cached commit pages or previous CI artifacts. Those
copies require separate removal or host support; do not claim history is clean
until those accessible copies have been audited. Never publish an old backup ref
or merge old commits back into a sanitized repository.
