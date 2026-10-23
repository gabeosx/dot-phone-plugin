# EOW launch runbook

Launched **Friday, October 23, 2026** on [Product Hunt](https://www.producthunt.com/products/dots-phone?launch=dots-phone).
The owner approved the saved schedule of **12:01 AM PDT / 3:01 AM EDT**.
All coverage times below are **America/New_York (EDT)**.

The owner is Gabe, Product Hunt [@gabea](https://www.producthunt.com/@gabea),
unless another person explicitly accepts coverage. Any action
marked **owner-only** requires the account holder to perform or authorize it.
This document records completed launch preparation and as-available follow-up;
it does not assign coverage or authorize additional calls or promotion.

## Readiness record

The record below was reconciled on October 23. [Version 0.3.3](https://github.com/gabeosx/dot-phone-plugin/releases/tag/v0.3.3)
is published, with [release checks](https://github.com/gabeosx/dot-phone-plugin/pull/5/checks)
and [OpenAI Dots positioning checks](https://github.com/gabeosx/dot-phone-plugin/pull/6/checks)
complete. Keep operational evidence outside the public repository and actual call
data, secrets and account details out of this document.

| Gate | Required evidence | Status / evidence pointer |
| --- | --- | --- |
| Release identity | Public commit SHA, release tag and final CI evidence. | **Passed.** [0.3.3 is published](https://github.com/gabeosx/dot-phone-plugin/releases/tag/v0.3.3) from public commit [`260f066`](https://github.com/gabeosx/dot-phone-plugin/commit/260f066e19df11de8c076a5f9fabbf8e770a5cde), with audited archives and verified SHA256SUMS. [The subsequent copy update](https://github.com/gabeosx/dot-phone-plugin/pull/6) clarifies the OpenAI Dots plugin relationship without changing the calling engine or package version. |
| Release checks | Exact-commit results from [public release](../PUBLIC-RELEASE.md), source/history credential audits, dependency/license checks, package/container checks. | **Passed for 0.3.3.** Both exact-head [push](https://github.com/gabeosx/dot-phone-plugin/actions/runs/39803275662) and [PR](https://github.com/gabeosx/dot-phone-plugin/actions/runs/39803286828) CI runs passed, including offline, JavaScript, container, build, source/history and credential checks. Downloaded release archives match SHA256SUMS. Four live dependency audits passed with zero findings and zero exceptions; both supported frozen projects use fixed Pillow 13.0.0. See the [dependency review](../DEPENDENCY-SECURITY.md); [issue 3](https://github.com/gabeosx/dot-phone-plugin/issues/3) is closed. |
| Fresh setup | Independent checkout following the published quickstart reaches dry-run/no-dial readiness; blockers documented with reproducible steps. | **0.3.2 baseline passed within the documented installation boundary.** A new isolated Compose installation with invented credentials and empty state passed OAuth PKCE, authenticated tool discovery and workflow version 0.3.2, with zero calls. A fresh unconfigured checkout correctly reports missing registration and provider settings. Account eligibility remains a prerequisite; see [installation validation](../VALIDATION.md#installation). |
| Runtime boundary | Local service, gateway, reasoner heartbeat and installed phone tools verified without dialing; filter checked according to the setup guide. | **Passed for the installed 0.3.3 setup.** Service, gateways, worker heartbeat, authenticated tools and native filter passed no-dial checks. The installed workflow reports 0.3.3 and its resource digests match the source. Upgrade checks recorded zero active calls, pending reviews and unclosed terminal calls. Existing owner configuration and dialing preferences were preserved. |
| Public website | Final HTTPS URL resolves logged out; desktop/mobile, keyboard, reduced-motion and demo disclosure checked. | **Passed.** [phone.gabea.nyc](https://phone.gabea.nyc) identifies Dots Phone as an independent plugin for OpenAI Dots and links [usage and responsibilities](https://phone.gabea.nyc/usage/). HTTPS, desktop/mobile layout, keyboard and reduced-motion checks passed. The [60-second demo](https://phone.gabea.nyc/demo/) plays, seeks and displays captions; its disclosure identifies owned numbers, a fictional order and condensed preparation/waits. |
| Source path | Repo, quickstart, privacy, validation, license and reporting links work logged out. | **Passed.** [Repository](https://github.com/gabeosx/dot-phone-plugin), [quickstart](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/QUICKSTART.md), [privacy](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/PRIVACY.md), [validation](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/VALIDATION.md), [license](https://github.com/gabeosx/dot-phone-plugin/blob/main/LICENSE) and [support](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/SUPPORT.md) links were checked logged out. |
| Source and call evidence | Demonstrated source use, actual received audio, keypad receipts, reviewed outcome and closure agree. | **Passed for the scoped fictional checks.** Actual voice/Codex cases cover supplied and missing facts, wrong number, interruption, hold and telephone encoding. An authorized owned-number IVR trial independently verified actual received audio, all 17 keypad digits, the supported reviewed result, voice closure and exact restoration. The public demo illustrates existing Page context becoming the brief; it claims no mailbox or mid-call lookup. Private traces remain outside source and release archives. |
| Listing fidelity | Preview label, provider costs, setup requirements and fictional demo disclosed; no unmeasured reliability or instant-setup promise. | **Passed.** Saved copy and gallery describe the experimental, self-hosted preview and own-account prerequisites/costs. Demo media retains complete IVR questions, keypad sequences and original-speed speech, with visible wait cuts; it establishes no general success rate. |
| Call openings | Production instructions follow the approved task and identity, with truthful answers and explicit call-specific requirements. | **Passed for 0.3.3.** Production instructions open with the approved task purpose and on whose behalf the call is made, without a routine unsolicited AI self-introduction. They answer identity questions honestly, preserve explicit call-specific disclosure instructions and keep IVR openings listen-first. The 120 focused offline tests, full suite and CI passed. Prompt tests do not establish audible behavior for every call. |
| Product Hunt access — owner-only | Correct maker account and signed-in submission form available; draft saved with actual maker attribution. | **Verified.** The [existing listing](https://www.producthunt.com/products/dots-phone?launch=dots-phone) is live as Gabe/@gabea. No additional maker or volunteer is assumed. |
| Gallery | Thumbnail and PNGs 01–05 preview correctly after upload, with no crops hiding disclosures. | **Passed.** Thumbnail and all five gallery images uploaded, previewed and saved; final order 01–05 and the public product URL were verified after reload. |
| Support — owner-only | A public reporting route exists and actual launch-day coverage is identified. | **Reporting route passed; owner coverage as available.** [GitHub Issues](https://github.com/gabeosx/dot-phone-plugin/issues), [support guidance](../SUPPORT.md) and [private vulnerability reporting](https://github.com/gabeosx/dot-phone-plugin/security/advisories/new) are available. The owner will check comments and issues as available, with no fixed coverage window or response-time promise. No other person is assigned. |
| Schedule — owner-only | Exact launch date and timezone/timestamp confirmed in Product Hunt's current UI. | **Completed.** The owner explicitly approved Friday, October 23. Product Hunt confirmed and retained **October 23, 2026 at 12:01 AM PDT / 3:01 AM EDT** after reload; the listing is now live. |
| Go / no-go — owner-only | Owner authorizes the release and final Product Hunt draft; scheduling evidence is recorded. | **Launched.** The owner approved the release and final listing. Release/runtime checks, saved scheduling confirmation and as-available support coverage are recorded. Follow up on actual reports without promising response times. |

The published demo SHA-256 is
`aeb1c7678cea3a37c4ee5843c4a7e2661c88951b829a839a1e57715c6b2ae38f`.
Its 60-second export passed full decoding and audio correlation checks against
the actual received recording. Only silence was added; no dialogue or keypad
tones were substituted. The preparation animation is an illustration with
condensed timing, rather than a source-tool execution trace.

Use the validation boundary honestly. The scoped acoustic and owned-number
checks establish observations from those scenarios; they do not establish a
general task completion rate. If a further live test involves a real recipient, paid
models or phone usage, prepare its scope and get the owner's specific authorization
before running it. Keep its resulting evidence private under the project's
privacy policy. Do not quietly turn a launch check into a phone call.

## Completed preparation

The release, installed-runtime checks, website/demo review and saved Product Hunt
listing are complete. The listing, first gallery image, README and quickstart now
identify Dots Phone as an independent plugin for OpenAI Dots. The owner approved
Friday's schedule and confirmed as-available comment and issue coverage.

Keep the [recorded demo](https://phone.gabea.nyc/demo/) and repository links current.
Prioritize broken setup paths, security/privacy problems and material inaccuracies;
rerun affected checks when fixing them. Login credentials and private call evidence
do not belong in public launch material.

## Confirmed schedule and timezone

Product Hunt retained **October 23, 2026 at 12:01 AM PDT**, equivalent to
**3:01 AM EDT / 07:01 UTC**, after the owner approved scheduling. The launch is live
on the [existing listing](https://www.producthunt.com/products/dots-phone?launch=dots-phone).
Do not create a duplicate post. Friday was the owner's project deadline, not a
claim that a particular weekday improves ranking.

## Friday launch coverage

The owner will check comments and issues as available, without fixed coverage
windows. The times below are optional checkpoints, not a staffing or response-time
commitment. No overnight coverage or additional person is assumed.

| Time (EDT) | Owner action |
| --- | --- |
| First launch-day check | Verify the live listing while logged out. Confirm destination links, gallery order, maker comment and disclosure. |
| After the listing is verified — owner-only | Publish reviewed announcement drafts in chosen existing channels, using the actual post URL. Ask for use and feedback. No upvote requests or incentives. |
| 08:00 | Answer questions; check setup failures and issues. Prioritize a broken download/setup path, leaked sensitive data or misleading copy over promotion. |
| 12:00 | Clear questions and record common friction. Recheck site and quickstart availability. |
| 17:00 | Reply to new users, explain open limitations, and turn reproducible problems into scoped issues without personal data. |
| 22:00 | Final evening sweep. Record findings and leave a clear next step on unresolved reports. |
| Saturday, Oct 24, after the confirmed daily boundary | Capture the public outcome and technical feedback. Keep replying over the weekend when available. |

These drafts do not authorize posting. Publish only where the owner has selected
a channel and permitted the action. Do not bulk-message
people, coordinate voting, buy votes, or make participation contingent on voting.
Product Hunt's [voting policy](https://help.producthunt.com/en/articles/17350772-launch-guide)
allows spreading the word and inviting discussion, and prohibits pressure or
incentives for upvotes.

## Handling problems

| Problem | Immediate response | Follow-through |
| --- | --- | --- |
| Site or repository inaccessible | Pause announcements. Check the final direct links and hosting/repository visibility. | Restore the known working published version and recheck logged out. |
| Setup fails | Acknowledge the exact limitation. Ask for OS/version, failed step and synthetic/redacted output. | Reproduce from a fresh checkout, fix, and update quickstart and affected tests. |
| Suspected leaked credential or private call detail | Stop distributing the affected asset/source. Tell the owner through the established private route. | Follow the project's security/reporting guidance; rotate affected secrets and rerun audits before distribution resumes. |
| Incorrect capability claim | Correct listing/site text promptly. | State what was corrected and link the documented limit; avoid a speculative explanation. |
| Product Hunt is not live at expected time | Check the existing draft's status and exact date/time. Use PH support if needed. | Do not create a duplicate submission. |
| Product is live but not featured | Share the direct listing and continue useful conversations. | Featuring is curated; do not promise a leaderboard position or pressure support. |
| A visitor asks for an unsupported scenario | Say it is unverified and record the gap. | Use the [reply drafts](PRODUCT-HUNT-COPY.md#short-answers-for-comments-and-support); never invent a success rate. |

## After launch

- Record actual visits only if existing tooling supplies them; do not infer them
  from comments, views or votes. Note when a metric is unavailable.
- Group feedback into setup failures, use cases, voice/runtime issues and unclear
  communication. Keep the public issue minimal and synthetic.
- Reply to substantive comments for the next few days, within actual coverage.
- Prioritize the most common reproducible blocker. A launch ranking alone does
  not decide what to build next.
- If sharing results publicly, use the actual numbers and date and make no
  reliability inference from the Product Hunt response.
