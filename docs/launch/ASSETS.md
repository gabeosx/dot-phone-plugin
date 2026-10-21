# Launch assets

The upload files live in [`website/assets/launch/`](../../website/assets/launch/).
They use the site's dark background, lime accent and violet reasoning accent.
All artwork is code-native SVG, with raster upload versions. No real-call data,
recordings, inbox screenshots or personal identifiers are used.

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

The website's 24-second walkthrough uses fictional people, a fictional retailer,
fictional email and compressed timing. These gallery illustrations explain that
flow. They must retain their preview and fictional labels if cropped or edited.
Do not call them a live-call recording or a screenshot of a completed real call.
If the owner creates a video later, add the same disclosure in the video itself
and its description, then use an accessible full YouTube URL in Product Hunt's
optional video field. No video is required by this kit.
