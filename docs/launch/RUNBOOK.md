# EOW launch runbook

Target: **Friday, October 23, 2026**. All coverage times below are
**America/New_York (EDT)**. The owner authorized completing the remaining launch
work and scheduling Friday. The schedule remains unconfirmed until the actual
Product Hunt UI shows the saved date, time and timezone.

The owner is Gabe, Product Hunt [@gabea](https://www.producthunt.com/@gabea),
unless another person explicitly accepts coverage. Any action
marked **owner-only** requires the account holder to perform or authorize it.
This document records readiness and coverage; it does not itself perform those
actions or confirm a saved schedule.

## Readiness record

Keep operational evidence under ignored `artifacts/product-hunt-20261020/`.
The record below was reconciled on October 22 against the final release,
installation, published website and demo evidence. A pending row is not passed.
Keep actual call data, secrets and account details out of this public document.

| Gate | Required evidence | Status / evidence pointer |
| --- | --- | --- |
| Release identity | Public commit SHA, release tag and final CI evidence. | **Passed.** [0.3.2 is published](https://github.com/gabeosx/dot-phone-plugin/releases/tag/v0.3.2) with audited archives and SHA256SUMS. Domain-source commit [`6459b9d`](https://github.com/gabeosx/dot-phone-plugin/commit/6459b9d4cb5dc7db40299d55ad3342836baa5f75) and merged public main [`8f07df7`](https://github.com/gabeosx/dot-phone-plugin/commit/8f07df796c8bc9fd165d1b497c11bcf03e05ce0f) have the tested tree; [domain PR checks](https://github.com/gabeosx/dot-phone-plugin/pull/4/checks) passed. |
| Release checks | Exact-commit results from [public release](../PUBLIC-RELEASE.md), source/history credential audits, dependency/license checks, package/container checks. | **Passed.** [Final runtime CI](https://github.com/gabeosx/dot-phone-plugin/actions/runs/39527150844), host Python 1,788, Pipecat container 1,768, base container 900, JavaScript 94 and native filter 16 checks passed, with documented optional/platform skips. Source/history, credential and two-distribution audits passed; downloaded release archives match SHA256SUMS. Four dependency audits pass the [documented, expiring exception policy](../DEPENDENCY-SECURITY.md); nine Pillow advisories remain tracked in [issue 3](https://github.com/gabeosx/dot-phone-plugin/issues/3). |
| Fresh setup | Independent checkout following the published quickstart reaches dry-run/no-dial readiness; blockers documented with reproducible steps. | **Passed within the documented installation boundary.** A new isolated Compose installation with invented credentials and empty state passed OAuth PKCE, authenticated tool discovery and workflow version 0.3.2, with zero calls. A fresh unconfigured checkout correctly reports missing registration and provider settings. Account eligibility remains a prerequisite; see [installation validation](../VALIDATION.md#installation). |
| Runtime boundary | Local service, gateway, reasoner heartbeat and installed phone tools verified without dialing; filter checked according to the setup guide. | **Passed.** The upgraded 0.3.2 installation and connected tools agree. Final read-only setup checks report 26 OK, no failures, and optional local ASR explicitly not checked. Service/gateways, worker heartbeat, installed registration and native filter were verified; final active calls and pending reviews are zero. |
| Public website | Final HTTPS URL resolves logged out; desktop/mobile, keyboard, reduced-motion and demo disclosure checked. | **Passed.** [phone.gabea.nyc](https://phone.gabea.nyc) serves site version 10 from source `b20c547e6bfcf7c3743cbb3fa9aa71ef7a929afe`. HTTPS, desktop/mobile layout, keyboard and reduced-motion checks passed. The [60-second demo](https://phone.gabea.nyc/demo/) plays, seeks and displays captions; its disclosure identifies owned numbers, a fictional order and condensed preparation/waits. |
| Source path | Repo, quickstart, privacy, validation, license and reporting links work logged out. | **Passed.** [Repository](https://github.com/gabeosx/dot-phone-plugin), [quickstart](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/QUICKSTART.md), [privacy](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/PRIVACY.md), [validation](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/VALIDATION.md), [license](https://github.com/gabeosx/dot-phone-plugin/blob/main/LICENSE) and [support](https://github.com/gabeosx/dot-phone-plugin/blob/main/docs/SUPPORT.md) links were checked logged out. |
| Source and call evidence | Demonstrated source use, actual received audio, keypad receipts, reviewed outcome and closure agree. | **Passed for the scoped fictional checks.** Actual voice/Codex cases cover supplied and missing facts, wrong number, interruption, hold and telephone encoding. An authorized owned-number IVR trial independently verified actual received audio, all 17 keypad digits, the supported reviewed result, voice closure and exact restoration. The public demo illustrates existing Page context becoming the brief; it claims no mailbox or mid-call lookup. Private traces remain outside source and release archives. |
| Listing fidelity | Preview label, provider costs, setup requirements and fictional demo disclosed; no unmeasured reliability or instant-setup promise. | **Passed.** Saved copy and gallery describe the experimental, self-hosted preview and own-account prerequisites/costs. Demo media retains complete IVR questions, keypad sequences and original-speed speech, with visible wait cuts; it establishes no general success rate. |
| Human-answer disclosure | Production instructions and public claims agree on proactive AI identification when a person answers. | **Needs resolution.** The general voice prompt explicitly requires AI identification, while the autonomous task opening does not state it unconditionally. Reconcile the task instructions and disclosure claim before declaring the final launch review complete. Prompt wording alone cannot guarantee audible disclosure. |
| Product Hunt access — owner-only | Correct maker account and signed-in submission form available; draft saved with actual maker attribution. | **Verified.** Draft saved as Gabe/@gabea at [the existing product draft](https://www.producthunt.com/products/dots-phone?launch=dots-phone). No additional maker or volunteer is assumed. |
| Gallery | Thumbnail and PNGs 01–05 preview correctly after upload, with no crops hiding disclosures. | **Passed.** Thumbnail and all five gallery images uploaded, previewed and saved; final order 01–05 and the public product URL were verified after reload. |
| Support — owner-only | A public reporting route exists and actual launch-day coverage is identified. | **Reporting route passed; coverage unconfirmed.** [GitHub Issues](https://github.com/gabeosx/dot-phone-plugin/issues), [support guidance](../SUPPORT.md) and [private vulnerability reporting](https://github.com/gabeosx/dot-phone-plugin/security/advisories/new) are available. The owner has not committed personal availability or named another person for the proposed coverage times. No response-time promise is made. |
| Schedule — owner-only | Exact launch date and timezone/timestamp confirmed in Product Hunt's current UI. | **Pending actual UI confirmation.** Friday, October 23 is authorized. Record the saved timestamp and timezone after scheduling this existing draft. |
| Go / no-go — owner-only | Owner authorizes the release and final Product Hunt draft; scheduling evidence is recorded. | **Authorized to finish and schedule Friday.** Resolve the disclosure consistency item, then confirm the final saved listing/date/time and coverage plan before treating launch operations as complete. |

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

## Before launch

The owner supplied `@gabea`; the signed-in session was observed as Gabe and linked
to that profile. The actual submission form is accessible, the draft is saved,
and the owner has authorized Friday scheduling. Confirm the saved scheduler
state in that authenticated session.
Creating a draft requires Product Hunt login. No login credentials belong in this
kit or in chat.

| When (EDT) | Work | Done when |
| --- | --- | --- |
| Tuesday, Oct 20 | Finish claims, source/privacy review, launch kit and assets. Owner checks Product Hunt account access now. | Account eligibility risks and technical blockers have named owners. |
| Wednesday, Oct 21 | Run fresh setup and release checks; resolve user-facing failures. Review site, quickstart and public issue-reporting route. | Another checkout can follow the public instructions through no-dial readiness. |
| Thursday, Oct 22, by 12:00 | Readiness review. Owner selects final product URL, release SHA and support route. | Evidence rows above are filled; launch has an honest scope. |
| Thursday, Oct 22, by 18:00 — owner-only | Create Product Hunt draft using [copy](PRODUCT-HUNT-COPY.md), upload assets and review preview on desktop/mobile. Add first comment. | Draft has correct links, maker identity, costs and disclosures. |
| Thursday, Oct 22, by 21:00 — owner-only | If gates pass, select Oct 23 in scheduler. Record the exact timestamp and reserve comment coverage. | Owner has reviewed the final draft and knows when it goes live. |
| Thursday, Oct 22, after review | Freeze launch copy and assets. Limit changes to broken links, security/privacy problems or material inaccuracies; rerun affected checks. | Public version and draft agree. |

The listed release, setup, runtime, source, website, demo and gallery checks are
complete; the newly identified human-answer disclosure consistency item remains
open. The remaining operational items are the actual saved schedule/timezone and an
honest coverage plan; the table below proposes work and does not assign personal
availability. If an account cannot post, the install is materially broken, the public source
audit fails, or the published site is stale, resolve it before scheduling. Moving
the date is better than sending visitors into a known failure. A late optional
YouTube video does not block launch: the static gallery and fictional website
walkthrough cover the demo.

## Scheduling and timezone check

Product Hunt's current [launch guide](https://help.producthunt.com/en/articles/17350772-launch-guide)
uses **12:01 AM Pacific**. Its older
[posting page](https://help.producthunt.com/en/articles/479557-how-to-post-a-product)
uses **PST**. On the proposed date, 00:01 America/Los_Angeles is **03:01 EDT**;
fixed UTC−08:00 would instead mean **04:01 EDT**. The form's confirmed timestamp
is the operational source of truth. Have the owner available from **02:45 through
04:15 EDT** if the UI does not make the offset unambiguous. Do not create another
post when the first does not appear immediately. These are planning conversions;
the saved UI date/time and offset are still pending in this record.

The latest [scheduling help](https://help.producthunt.com/en/articles/2724119-how-to-schedule-a-post)
supports drafts and scheduling within 30 days. Friday is the owner-confirmed
project deadline, not a claim that a particular weekday improves ranking.

## Friday launch coverage

These are proposed checkpoints for the owner or someone who explicitly accepts
them. They are not confirmed staffing or a response-time commitment. If overnight
coverage is unavailable, record that limit and check the listing when available.

| Time (EDT) | Owner action |
| --- | --- |
| 02:45 | Open the final draft and public site. Check availability and the actual scheduler timestamp. |
| Confirmed go-live, expected 03:01; allow through 04:15 | Verify the public listing while logged out. Confirm destination links, gallery order, maker comment and disclosure. Save the real public post URL privately. |
| After the listing is verified — owner-only | Publish reviewed announcement drafts in chosen existing channels, using the actual post URL. Ask for use and feedback. No upvote requests or incentives. |
| 08:00 | Answer questions; check setup failures and issues. Prioritize a broken download/setup path, leaked sensitive data or misleading copy over promotion. |
| 12:00 | Clear questions and record common friction. Recheck site and quickstart availability. |
| 17:00 | Reply to new users, explain open limitations, and turn reproducible problems into scoped issues without personal data. |
| 22:00 | Final evening sweep. Record findings and leave a clear next step on unresolved reports. |
| Saturday, Oct 24, after the confirmed daily boundary | Capture the public outcome and technical feedback. Keep replying over the weekend when available. |

No social or direct message in this kit has been sent. Publish only where the
owner has selected a channel and permitted the action. Do not bulk-message
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
