# Dots Phone landing page

A dependency-free, buildless landing page. `dist/` is the complete website and
editable source: HTML, CSS, JavaScript, and a local SVG favicon. No build,
package install, external fonts, analytics, or runtime network requests are used.

## Preview

From this directory:

```sh
python3 -m http.server 4173 --directory dist --bind 127.0.0.1
```

Open `http://127.0.0.1:4173`. Stop the server with Ctrl+C when finished.

## Product film

`film.js` renders a silent, 24-second film into `#call-film`. `film.css` contains
its responsive layout and visual styling; the page frame lives in `styles.css`.
All people, businesses, messages, identifiers, and delivery details are fictional.
Nothing in the film makes a call, opens an inbox, or sends data anywhere.

The film has seven shots:

| Seconds | Shot |
| --- | --- |
| 0–3.2 | Jamie writes and sends a request in Dots. |
| 3.2–6.8 | The order, request, and permitted Gmail access become a briefing. It is saved before dialing. |
| 6.8–11.6 | Realtime answers from the briefing. The retailer then asks for a missing delivery reference. |
| 11.6–14.4 | A pullback shows the live call and the Realtime → Codex → Gmail lookup path. |
| 14.4–18.2 | The email opens; the reference is highlighted and lifted out of the message. |
| 18.2–20.7 | The reference returns to the conversation, and the retailer confirms the delivery window. |
| 20.7–24 | The confirmed Friday delivery window returns to Jamie. |

The animation is a deterministic render of elapsed time, so the native range
control can seek to any frame. Play/pause, replay, and six chapter buttons are
keyboard accessible. Chapters open a representative, readable moment of their
scene. Space toggles playback when the film itself has focus; left/right arrows
seek one second, and Home/End select the beginning or end. Arrow keys and
Home/End navigate the chapter controls. The native scrubber retains its own
keyboard behavior. Fullscreen is available on supporting desktop browsers.

Playback starts once when at least 45% of the stage is visible. It pauses when
the stage leaves the viewport or the page becomes hidden, and stops at the end.
Reduced-motion preferences disable autoplay, camera movement, the pointer,
and celebration effects; all chapters and the scrubber remain available.
The mobile stage has a fixed portrait height and recomposes each shot.

The renderer sets `data-scene` and `data-playing` on `#call-film` for inspection.
It uses one animation frame loop only while playing. There are no canvas,
video, audio, timer, or third-party animation dependencies.

## Launch checks

- Confirm GitHub links match the final repository and default branch.
- Check desktop and narrow mobile layouts, keyboard focus, reduced motion,
  all chapters, arbitrary seeking, pause/resume, replay, and the stopped end.
- Keep the fictional-demo disclosure and the compressed-timing label.
- Keep capability and architecture copy aligned with the project README.

The parent workflow owns hosting metadata and deployment.
