# EOW launch runbook

Target: **Friday, October 23, 2026**. All coverage times below are
**America/New_York (EDT)**. This is a preparation plan, not an automation or a
confirmation that a Product Hunt launch is scheduled.

The owner is Gabe, Product Hunt [@gabea](https://www.producthunt.com/@gabea),
unless another person explicitly accepts coverage. Any action
marked **owner-only** requires the account holder to perform or authorize it.
The kit does not publish, contact people, place a call, buy anything, or schedule
the launch.

## Readiness record

Keep operational evidence under ignored `artifacts/product-hunt-20261020/`.
Record the following before scheduling. A blank row is pending, not passed.
Keep actual call data, secrets and account details out of this public document.

| Gate | Required evidence | Status / evidence pointer |
| --- | --- | --- |
| Release identity | Public commit SHA, release tag if used, final CI run URL. | Pending |
| Release checks | Exact-commit results from [public release](../PUBLIC-RELEASE.md), source/history credential audits, dependency/license checks, package/container checks. | Pending |
| Fresh setup | Independent checkout following the published quickstart reaches dry-run/no-dial readiness; blockers documented with reproducible steps. | Pending |
| Runtime boundary | Local service, gateway, reasoner heartbeat and installed phone tools verified without dialing; filter checked according to the setup guide. | Pending |
| Public website | Final HTTPS URL resolves logged out; desktop/mobile, keyboard, reduced-motion and demo disclosure checked. | Pending |
| Source path | Repo, quickstart, privacy, validation, license and reporting links work logged out. | Pending |
| Listing fidelity | Preview label, provider costs, setup requirements and fictional demo disclosed; no unmeasured reliability or instant-setup promise. | Pending |
| Product Hunt access — owner-only | Correct maker account and signed-in submission form available; save a draft and confirm actual co-makers next. | Verified Oct 20: draft saved as Gabe/@gabea at `https://www.producthunt.com/products/dots-phone?launch=dots-phone`; explicitly not scheduled. |
| Gallery | Thumbnail and PNGs 01–05 preview correctly after upload, with no crops hiding disclosures. | All six uploaded and visible Oct 20. Check final gallery order before scheduling. |
| Support — owner-only | A public reporting route exists and the owner can monitor it on launch day. | Pending |
| Schedule — owner-only | Exact launch date and timezone/timestamp confirmed in Product Hunt's current UI. | Pending |
| Go / no-go — owner-only | Owner approves this exact release and final Product Hunt draft. | Pending |

Use the validation boundary honestly. Passing fixtures and no-dial readiness can
support an experimental preview; they cannot establish live-call audibility or
a task completion rate. If a needed live test involves a real recipient, paid
models or phone usage, prepare its scope and get the owner's specific authorization
before running it. Keep its resulting evidence private under the project's
privacy policy. Do not quietly turn a launch check into a phone call.

## Before launch

The owner supplied `@gabea`; the signed-in session was observed as Gabe and linked
to that profile. A previous launch dated January 17, 2026 was visible. That is
evidence of prior activity, not a verified account creation date or a guarantee
of permission to schedule this product. The actual submission form is accessible;
the draft is saved; the scheduler must still be checked in that authenticated session.
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

If an account cannot post, the install is materially broken, the public source
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
post when the first does not appear immediately.

The latest [scheduling help](https://help.producthunt.com/en/articles/2724119-how-to-schedule-a-post)
supports drafts and scheduling within 30 days. Friday is the owner-confirmed
project deadline, not a claim that a particular weekday improves ranking.

## Friday launch coverage

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
