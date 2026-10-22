# Dots Phone launch kit

Prepared for the owner-confirmed **Friday, October 23, 2026** Product Hunt launch.
The exact schedule and readiness record are in the [runbook](RUNBOOK.md). The
product is an **open-source, self-hosted experimental preview** for technical users
who can run the local service and configure their own provider accounts.

| Ready to review | Location |
| --- | --- |
| Listing fields, maker comment, replies and announcement drafts | [Product Hunt copy](PRODUCT-HUNT-COPY.md) |
| Owner actions, launch gates and launch-day coverage | [Launch runbook](RUNBOOK.md) |
| Upload order, image descriptions and editable asset sources | [Asset guide](ASSETS.md) |
| Official platform requirements and source links | [Product Hunt requirements](PRODUCT-HUNT-REQUIREMENTS.md) |

The gallery includes five 1270×760 PNGs and a 240×240 thumbnail in
[`website/assets/launch/`](../../website/assets/launch/). These are fictional
illustrations. The separate [60-second demo](https://phone.gabea.nyc/demo/) records
an actual Dots request, preparation from a fictional connected Page, a call to an
owned automated test recipient, IVR navigation with keypad verification, and the
reviewed answer. The Page is read before dialing; this recording includes no
Gmail or mid-call source lookup.

A [Product Hunt draft](https://www.producthunt.com/products/dots-phone?launch=dots-phone)
is saved under @gabea, with the thumbnail and five gallery images. The website
hosts the demo; the optional Product Hunt video field remains empty.

## Launch gates

Do not mark the launch ready from these documents alone. Record the evidence and
the exact public commit in the [runbook](RUNBOOK.md#readiness-record).

- The public repository and published website must resolve without signing in.
- The release checks and source/history audits must pass for the exact published
  commit; use [public release guidance](../PUBLIC-RELEASE.md).
- A clean setup must reach the documented no-dial readiness state. Failures must
  be fixed or prominently documented. See [quickstart](../QUICKSTART.md) and
  [validation limits](../VALIDATION.md).
- Keep raw recordings, private call records and operational evidence outside the
  public repository. Share only the explicitly approved demo edit, with its
  fictional-details and automated-test-recipient disclosure.
- The owner must verify Product Hunt account eligibility, approve listing facts,
  review the image previews, and confirm the scheduler's actual timezone.
- The owner must select a support channel they can monitor and ensure its public
  URL is available. Do not promise a response time without coverage.

## Positioning to preserve

Dots prepares a briefing, uses Realtime voice through Twilio, asks Codex for help
when needed, and reports back after review. Connected read tools are optional
and scoped. Local call records are encrypted; model requests go to OpenAI and
phone audio goes through Twilio. Setup needs the user's own accounts and keys.
Open source. Self-hosted. Uses your OpenAI and Twilio accounts.

Dots Phone is an independent, self-hosted project, not affiliated with or endorsed
by OpenAI or Twilio. Results are not guaranteed; users must review important
results. Users are responsible for connected-account charges, including when a
call fails, and the permissions needed to use those accounts. Link to
[usage and responsibilities](../USAGE.md) in launch copy.

Do not promise instant setup, guaranteed task completion, a call success rate,
universal IVR support, a fixed call price, or a fully local/private audio path.
The current proof boundary is documented in [VALIDATION.md](../VALIDATION.md).

## What a useful launch measures

Record qualified setup attempts, where users get stuck, actionable bug reports,
and recurring use cases. Record traffic only from tools already deployed and
available; the current site has no analytics. GitHub stars, Product Hunt rank and
votes are secondary observations. No new tracker is required to launch.
