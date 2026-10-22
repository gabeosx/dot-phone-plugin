# Product Hunt copy

Paste-ready drafts for owner review. Match availability, pricing and links to the
actual public release before scheduling. The product promise is the experimental
self-hosted preview described in the [README](../../README.md).

## Listing fields

**Name**

```text
Dots Phone
```

**Tagline** — 38 characters

```text
A phone-calling plugin for OpenAI Dots
```

**Description** — 231 characters; checked by `website/assets/launch/verify.py`

```text
Dots Phone is an independent plugin for OpenAI Dots. Give Dots a phone task: it prepares a brief, makes the call, asks Codex for missing details when needed, and reports the result. Self-hosted with your OpenAI and Twilio accounts.
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

**Pricing:** Free is the current selection in Product Hunt's pricing field. Keep
the connected-provider cost notice in the maker comment and linked usage guide.

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
the thumbnail. See [asset guide](ASSETS.md) for alt text. The static gallery images
are fictional illustrations, separate from the [actual recorded demo](https://phone.gabea.nyc/demo/).
The current form accepts YouTube or Loom in its optional video field. Leave that
field empty while the website hosts the recording. The thumbnail and all five
gallery images are uploaded to the saved draft; verify their order before scheduling.

## Maker first comment

Owner: review the personal perspective before pasting from the `@gabea` account.

```text
Hi Product Hunt — I’m Gabe, the maker of Dots Phone, an independent phone-calling plugin for OpenAI Dots.

Some errands still end with “you’ll need to call us.” Dots Phone is for those conversations: checking a delivery, following up with customer service, or getting an answer that isn’t available online.

Tell Dots what you need and what it may agree to. Dots gathers the details, saves a brief, and uses Dots Phone to make the call. If a question needs missing information, Codex can look it up through the apps you allow. Dots then reports the result and anything that still needs your attention.

Watch the 60-second demo: https://phone.gabea.nyc/demo/

It’s an actual recording using fictional order details and an owned automated test recipient. Dots reads the order confirmation before dialing, navigates the IVR and returns the reviewed answer.

This is an open-source, self-hosted experimental preview for technical early adopters, with a setup guide Codex can help you follow. I’d love to hear what you’d want it to call about and where setup could be easier.

Dots Phone is an independent, self-hosted project, not affiliated with or endorsed by OpenAI or Twilio. Results aren’t guaranteed; review important results. You’re responsible for connected-account charges, including when a call fails. Usage and responsibilities: https://phone.gabea.nyc/usage/
```

## Short answers for comments and support

**Who is this for?**

Technical early adopters who use OpenAI Dots and Codex and are comfortable running
Docker, configuring provider accounts, and reviewing a call's scope. This preview is
self-hosted. You need eligible ChatGPT custom plugin registration access; check
that before buying provider services. The [quickstart](../QUICKSTART.md) lists
the prerequisites.

**How does it work with OpenAI Dots?**

Give Dots a phone task in a normal conversation. It gathers the details, prepares a
brief and uses the Dots Phone plugin to make the call. Codex can look up missing
information through the apps you allow, and Dots reports the result. You run the
plugin's service on your own computer and connect it to Dots with your own accounts.

**Can I try it in my browser?**

You can watch the [recorded demo](https://phone.gabea.nyc/demo/) in your browser.
Making calls requires the local service, your own accounts and credentials, and
the setup steps in the repository.

**How much does it cost?**

You are responsible for OpenAI voice usage, Twilio's number and phone charges,
and any other connected service you use. Codex uses your account's allowance.
Charges can accrue even when a call fails. Call budgets are estimates; check your provider billing. See
[usage and responsibilities](../USAGE.md).

**Is this an OpenAI or Twilio product?**

Dots Phone is an independent, self-hosted project, not affiliated with or endorsed
by OpenAI or Twilio.

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

The opening focuses on the approved purpose and calling on your behalf. Dots
answers honestly about its identity if asked. Include any required calling or
recording disclosures in the call instructions.

**How do I report a bug?**

Start with [troubleshooting](../SUPPORT.md), then file a
[reproducible issue](https://github.com/gabeosx/dot-phone-plugin/issues) if needed.
Share software versions, the failed step and a minimal synthetic reproduction. Keep credentials,
phone numbers, inbox contents, call audio and real-call transcripts out of public
issues. If a report needs private material, first agree on a private route with
the maintainer.

**Is the demo an actual recording?**

Yes. The [60-second demo](https://phone.gabea.nyc/demo/) records an actual Dots
request and a telephone call to an owned automated test recipient, using fictional
order details. The connected Page is read before dialing; the recording shows IVR
navigation, keypad verification and the reviewed result. It contains no Gmail or
mid-call source lookup. The static gallery images are separate fictional
illustrations. Raw recordings and private call records stay outside the public
repository.

## Announcement drafts

Owner-only publishing. Replace `PRODUCT_HUNT_LAUNCH_URL` with the verified public
post URL after launch. These drafts invite feedback; they do not ask for votes.

**Short social post**

```text
I’m launching Dots Phone, an independent phone-calling plugin for OpenAI Dots. Give Dots a task: it prepares a brief, makes the call and reports back, with Codex handling missing details when needed. Self-hosted experimental preview. I’d love your feedback: PRODUCT_HUNT_LAUNCH_URL
```

**Longer post or community introduction**

```text
I’m sharing Dots Phone on Product Hunt today. It’s an independent phone-calling plugin for OpenAI Dots. Give Dots a task: it prepares a brief, makes the call, and brings in Codex for missing information from permitted read tools. Dots reports the result. This is an open-source, self-hosted experimental preview.

Watch a 60-second recording using fictional order details and an owned automated test recipient: https://phone.gabea.nyc/demo/

Dots Phone is an independent, self-hosted project, not affiliated with or endorsed by OpenAI or Twilio. Results aren’t guaranteed; review important results. You’re responsible for connected-account charges, including when a call fails. Usage and responsibilities: https://phone.gabea.nyc/usage/

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
