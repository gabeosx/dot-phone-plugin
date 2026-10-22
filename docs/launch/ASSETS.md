# Launch assets

The upload files live in [`website/assets/launch/`](../../website/assets/launch/).
They use the site's dark background, lime accent and violet reasoning accent.
The static gallery artwork is code-native SVG, with raster upload versions. It
uses fictional illustrations, with no call records, inbox screenshots or personal
identifiers. The separate recorded demo is described below.

## Upload order

| File | Size | Purpose / alt text |
| --- | --- | --- |
| `thumbnail.png` | 240×240 | Four lime dots on a dark background: Dots Phone product mark. |
| `01-let-dots-call.png` | 1270×760 | Dots Phone: let Dots make the call. A fictional request asks when a replacement washing machine will arrive. Self-hosted experimental preview. |
| `02-brief-first.png` | 1270×760 | An illustrated fictional call brief: ask for the delivery window, share order details, and make no purchases or changes. |
| `03-help-during-the-call.png` | 1270×760 | Workflow diagram: Realtime carries the conversation through Twilio; Codex can use permitted read tools when information is missing. |
| `04-the-answer-back.png` | 1270×760 | A fictional result says the retailer confirmed a Friday 9 am–1 pm delivery window. Explicitly labeled as a fictional result, not a real call. |
| `05-your-setup.png` | 1270×760 | Self-hosted setup: local service, OpenAI API, Twilio and Codex access. Optional authenticated read tools. Provider charges apply. |
| `social-card.png` | 1200×630 | Dots Phone: let Dots make the call. Open-source, self-hosted experimental preview. |

Use the five numbered PNGs as the gallery, not the contact sheet. Product Hunt
recommends 1270×760 gallery images and at least two, and a square 240×240 thumbnail
under 3 MB. See [official preparation guidance](https://www.producthunt.com/launch/preparing-for-launch).

The SVGs are editable master artwork. PNGs are ready for upload. `social-card.png`
is for link previews and owner-approved social posts. The parent website workflow
may copy it into the deployed site directory for Open Graph metadata.

## Rebuild and check

`render.mjs` writes SVG and PNG versions using the `sharp` image library. It does
not make network calls. With `sharp` available in your normal Node environment:

```sh
node website/assets/launch/render.mjs
python3 website/assets/launch/verify.py
```

If `sharp` is in a bundled dependency directory, set `DOTS_LAUNCH_SHARP_PATH` to
its entrypoint. The renderer also writes a review contact sheet into the ignored
launch artifact folder. Inspect the individual full-size PNGs after changing
text; a size check cannot prove visual quality.

The `verify.py` check covers dimensions, PNG encoding, thumbnail size, required
fictional/preview labels in SVG sources and listing character limits. It is a
small release-artifact sanity check, not product validation.

## Demo usage

The [60-second website demo](https://phone.gabea.nyc/demo/) is an edited recording
of an actual Dots request, a brief prepared from a fictional order confirmation
in a connected Page, a call to an owned automated test recipient, IVR navigation
with keypad verification, and the reviewed result. The order details are
available before dialing. No Gmail or mid-call source lookup appears in this
recording. Preserve the disclosures about fictional details, the automated test
recipient and editing when sharing it.

The five gallery images remain authored fictional illustrations. Retain their
preview and fictional labels when cropping or editing, and describe them as
illustrations rather than frames from the recorded call. Their illustrated
delivery window is separate from the recorded demo's result.

Product Hunt's submission form accepts an optional YouTube or Loom URL, as
observed on October 22. The demo is currently hosted on the website, so leave the
video field empty. A YouTube or Loom upload is optional; if one is added later,
verify public playback and carry over the demo disclosures.
