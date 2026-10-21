# Product Hunt copy

Paste-ready drafts for owner review. Match availability, pricing and links to the
actual public release before scheduling. The product promise is the experimental
self-hosted preview described in the [README](../../README.md).

## Listing fields

**Name**

```text
Dots Phone
```

**Tagline** — 55 characters

```text
A self-hosted assistant for the phone calls you put off
```

**Description** — 206 characters; checked by `website/assets/launch/verify.py`

```text
Dots makes customer-service calls for errands like delivery updates. It prepares a brief and asks Codex for missing details in apps you allow. Open source. Self-hosted. Uses your OpenAI and Twilio accounts.
```

**Primary URL**

```text
https://phone.gabea.nyc
```

Check that the deployed site serves the final version before using this URL. Use
the plain URL in Product Hunt's product field, without UTM parameters.

**Additional link**

```text
https://github.com/gabeosx/dot-phone-plugin
```

**Topics:** Open Source, Artificial Intelligence, Productivity. These are the
three options selected in the saved draft.

**Pricing:** Free is selected in Product Hunt's pricing field. The description
and maker comment state that setup uses your OpenAI and Twilio accounts.

**Status:** experimental preview, source available for self-hosting. If the form
has a beta indicator, choose the option that reflects the release actually
available. Do not present a waitlist, managed service or hosted free trial.

**Maker:** Gabe, [@gabea](https://www.producthunt.com/@gabea). The owner supplied
this profile, and the signed-in Product Hunt session was observed linking to it.
Select `gabea` as the maker in the submission form. Add only actual co-makers;
their handles still need confirmation. Account creation date has not been checked.

**Shoutouts to consider:** OpenAI / Codex, Twilio, DeepFilterNet. Select the real
entries available in Product Hunt and only credit tools used in the release.

**Gallery:** upload PNGs 01–05 in numerical order from
[`website/assets/launch/`](../../website/assets/launch/). Use `thumbnail.png` for
the thumbnail. See [asset guide](ASSETS.md) for alt text. Leave the video field
empty unless the owner publishes and verifies a YouTube version of the fictional
walkthrough. The thumbnail and all five gallery images are uploaded to the saved
draft. Verify their order before scheduling; no video has been uploaded.

## Maker first comment

Owner: review the personal perspective before pasting from the `@gabea` account.

```text
Hi Product Hunt — I’m Gabe, the maker of Dots Phone.

Some errands still end with “you’ll need to call us.” Dots Phone is for those conversations: checking a delivery, following up with customer service, or getting an answer that isn’t available online.

Tell Dots what you need and what it may agree to. It gathers the details, saves a brief, and makes the call. If a question needs missing information, Codex can look it up through the apps you allow. Dots then reports the result and anything that still needs your attention.

This first release is open source and self-hosted, with a setup guide Codex can help you follow. It uses your OpenAI and Twilio accounts. Email access is optional.

The site walks through a fictional delivery inquiry so you can see the flow. I’d love to hear which phone errands you’d want Dots to take off your list, and where the setup could be easier.
```

## Short answers for comments and support

**Who is this for?**

Technical early adopters who use Codex and are comfortable running Docker,
configuring provider accounts, and reviewing a call's scope. This preview is
self-hosted. You need eligible ChatGPT custom plugin registration access; check
that before buying provider services. The [quickstart](../QUICKSTART.md) lists
the prerequisites.

**Can I try it in my browser?**

The site has a scripted fictional walkthrough. Making calls requires the local
service, your own accounts and credentials, and the setup steps in the repository.

**How much does it cost?**

Dots uses your OpenAI and Twilio accounts. OpenAI charges for API voice usage;
Twilio charges for the phone number and calls. Your tunnel service may also
charge. Codex reasoning uses your ChatGPT plan's allowance, which is separate
from API billing. Call budgets use estimates rather than live billing.

**Does this need my inbox?**

No. Start with the details you provide. Connected sources such as Gmail or Yahoo
are optional, authenticated separately, and limited to the read tools allowed for
the call. Installing the phone plugin does not grant inbox access.

**Is everything local?**

The service and encrypted call records are local. Model requests go to OpenAI;
Twilio carries phone audio. Connected source providers process their own tool
requests, and tunnels have their own data handling. Worker exchange files rely
on local file permissions. See the [privacy guide](../PRIVACY.md) for the data flow.

**How reliable is it?**

Launch checks cover task completion, missing information, wrong numbers, holds
and interruptions through the actual voice and Codex path, alongside the offline
suites. The [validation record](../VALIDATION.md) lists the results and remaining
work. Start with a narrow call and review the result in your own setup.

**What can it agree to?**

The brief defines the goal, permitted disclosures, decisions and limits. Start
with a narrow information request. A new purchase, account change or other action
needs authorization within the actual call scope. Outcomes can be incomplete.

**Does it support every IVR, accent, language or country?**

There is no universal support claim. IVR paths, carrier rules, source availability
and acoustic conditions can affect results. Check the validation limits and test
your intended setup before relying on it.

**Does Dots disclose that it is an assistant?**

The opening prompt tells Dots to introduce itself as an AI assistant calling on
your behalf. You review the purpose and permitted disclosures in the call brief.

**How do I report a bug?**

Start with [troubleshooting](../SUPPORT.md), then file a
[reproducible issue](https://github.com/gabeosx/dot-phone-plugin/issues) if needed.
Share software versions, the failed step and a minimal synthetic reproduction. Keep credentials,
phone numbers, inbox contents, call audio and real-call transcripts out of public
issues. If a report needs private material, first agree on a private route with
the maintainer.

**Can you share a real call recording?**

The public gallery and walkthrough are authored fictional examples. The project
keeps actual call contents, summaries and derived replay fixtures private. Public
test results describe the harness and its limits.

## Announcement drafts

Owner-only publishing. Replace `PRODUCT_HUNT_LAUNCH_URL` with the verified public
post URL after launch. These drafts invite feedback; they do not ask for votes.

**Short social post**

```text
I’m launching Dots Phone: a self-hosted phone assistant for calls you put off. It prepares a brief and can ask Codex for missing details during the call. Experimental preview; bring your own provider accounts. I’d love your feedback: PRODUCT_HUNT_LAUNCH_URL
```

**Longer post or community introduction**

```text
I’m sharing Dots Phone on Product Hunt today. It’s an open-source, self-hosted experimental phone assistant: prepare a brief, make the call, and bring in Codex for missing information from permitted read tools.

The site walks through a fictional delivery inquiry. Dots runs on your computer and uses your OpenAI and Twilio accounts.

If you work on voice agents or have a phone errand you would want help with, I’d value feedback on the setup and the design: PRODUCT_HUNT_LAUNCH_URL
```

**Reply to a setup failure**

```text
Thanks for trying it. Which setup step failed, and which OS and software versions are you using? Please leave out keys, phone numbers, inbox contents and real-call records. A minimal synthetic reproduction or redacted error message is enough to start investigating.
```

**Reply when the answer is not known**

```text
I haven’t verified that scenario yet. I’ll record it as a gap rather than promise it works. The current validation page explains what has been tested; a synthetic reproduction would help narrow the next check.
```
