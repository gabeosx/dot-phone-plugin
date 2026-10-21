# Dots Phone launch kit

Prepared for the owner-confirmed **Friday, October 23, 2026** Product Hunt launch.
This is a working target, not a scheduled launch. The product is an **open-source,
self-hosted experimental preview** for technical users who can run the local
service and configure their own provider accounts.

| Ready to review | Location |
| --- | --- |
| Listing fields, maker comment, replies and announcement drafts | [Product Hunt copy](PRODUCT-HUNT-COPY.md) |
| Owner actions, launch gates and launch-day coverage | [Launch runbook](RUNBOOK.md) |
| Upload order, image descriptions and editable asset sources | [Asset guide](ASSETS.md) |
| Official platform requirements and source links | [Product Hunt requirements](PRODUCT-HUNT-REQUIREMENTS.md) |

The gallery includes five 1270×760 PNGs and a 240×240 thumbnail in
[`website/assets/launch/`](../../website/assets/launch/). The workflow illustration
is explicitly fictional. It is not evidence of a real phone call or measured
reliability. A [Product Hunt draft](https://www.producthunt.com/products/dots-phone?launch=dots-phone)
is saved under @gabea, with the thumbnail and five gallery images. It is not
scheduled. No announcement has been posted.

## Launch gates

Do not mark the launch ready from these documents alone. Record the evidence and
the exact public commit in the [runbook](RUNBOOK.md#readiness-record).

- The public repository and published website must resolve without signing in.
- The release checks and source/history audits must pass for the exact published
  commit; use [public release guidance](../PUBLIC-RELEASE.md).
- A clean setup must reach the documented no-dial readiness state. Failures must
  be fixed or prominently documented. See [quickstart](../QUICKSTART.md) and
  [validation limits](../VALIDATION.md).
- Keep real-call contents and derived evidence private. Publish the validation
  boundary, not a success rate inferred from fixtures.
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

Do not promise instant setup, guaranteed task completion, a call success rate,
universal IVR support, a fixed call price, or a fully local/private audio path.
The current proof boundary is documented in [VALIDATION.md](../VALIDATION.md).

## What a useful launch measures

Record qualified setup attempts, where users get stuck, actionable bug reports,
and recurring use cases. Record traffic only from tools already deployed and
available; the current site has no analytics. GitHub stars, Product Hunt rank and
votes are secondary observations. No new tracker is required to launch.
