/* Scripted product film. All content is fictional; no calls, email access, or network requests. */
(() => {
  "use strict";
  const mount = document.getElementById("call-film");
  if (!mount) return;
  const DURATION = 24;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const marks = '<span class="film-dots" aria-hidden="true"><i></i><i></i><i></i><i></i></span>';
  const phone = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m7 3 3 5-2 2a14 14 0 0 0 6 6l2-2 5 3-1 4C10 23 1 14 3 4Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const mail = '<svg viewBox="0 0 30 24" fill="none" aria-hidden="true"><path d="M3 21V5l12 9L27 5v16" stroke="#4285f4" stroke-width="5" stroke-linejoin="round"/><path d="M3 5l12 9L27 5" stroke="#ea4335" stroke-width="5" stroke-linejoin="round"/><path d="M3 13V5" stroke="#fbbc04" stroke-width="5"/><path d="M27 13V5" stroke="#34a853" stroke-width="5"/></svg>';
  const check = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12 4 4L19 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const documentIcon = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 3H5v18h14V8l-5-5Z M14 3v5h5 M8 12h8 M8 16h6" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const waves = Array.from({ length: 54 }, (_, i) => `<i data-wave="${i}"></i>`).join("");
  const chapters = [
    { at: 0, preview: 2.1, label: "Ask" },
    { at: 3.2, preview: 6.15, label: "Prepare" },
    { at: 6.8, preview: 8.9, label: "Call" },
    { at: 11.6, preview: 13.95, label: "Look up" },
    { at: 18.2, preview: 19.8, label: "Answer" },
    { at: 20.7, preview: 23.0, label: "Done" },
  ];
  const scenes = [
    { at: 0, end: 3.2, name: "ask", label: "The request" },
    { at: 3.2, end: 6.8, name: "brief", label: "Before the call" },
    { at: 6.8, end: 11.6, name: "call", label: "On the line" },
    { at: 11.6, end: 14.4, name: "route", label: "A missing detail" },
    { at: 14.4, end: 18.2, name: "mail", label: "The email" },
    { at: 18.2, end: 20.7, name: "answer", label: "Back to the call" },
    { at: 20.7, end: DURATION, name: "result", label: "The answer" },
  ];

  mount.innerHTML = `
    <div class="film-screen" tabindex="0" role="region" aria-label="Dots Phone animated demonstration. Space toggles playback. Arrow keys seek.">
      <div class="film-grid" aria-hidden="true"></div>
      <header class="film-topline"><span class="film-brand">${marks}<b>dots</b><span>phone</span></span><span class="film-take"><span class="film-take-dot"></span><span id="film-shot-label">The request</span></span></header>
      <div class="film-corner film-corner-tl" aria-hidden="true"></div><div class="film-corner film-corner-br" aria-hidden="true"></div>
      <section class="film-shot film-ask" data-shot="ask" aria-label="Jamie asks Dots to call Lumen Home">
        <div class="film-request-window">
          <div class="film-window-top"><div class="film-window-dots" aria-hidden="true"><i></i><i></i><i></i></div><span>Jamie’s Dots</span><span class="film-window-plus" aria-hidden="true">＋</span></div>
          <div class="film-request-inner">
            <div class="film-request-ident"><span class="film-avatar">J</span><span>Jamie Morgan<span class="film-composer-status">Writing to Dots</span></span></div>
            <p class="film-request-text">Call Lumen Home.<br><span class="film-request-remainder">Find out when my replacement washing machine will arrive.</span><span class="film-caret" aria-hidden="true"></span></p>
            <div class="film-request-meta"><span>Order <b>LH-4821</b></span><span>${mail} Gmail allowed</span></div>
            <div class="film-request-bottom"><span>Status only. No order changes.</span><span class="film-send" aria-hidden="true">↑</span><svg class="film-pointer" viewBox="0 0 24 30" fill="none" aria-hidden="true"><path d="M3 2v23l6-7 5 10 4-2-5-9h9L3 2Z" fill="#f8ffe9" stroke="#17240e" stroke-width="1.4"/></svg></div>
          </div>
        </div>
        <div class="film-request-reply">${marks}<span>I’ll get the order details together and call.</span><span class="film-reply-arrow" aria-hidden="true">↗</span></div>
      </section>
      <section class="film-shot film-brief" data-shot="brief" aria-label="Dots saves the briefing before dialing">
        <div class="film-source film-source-order"><span class="film-source-icon">${documentIcon}</span><span>Order confirmation<b>LH-4821</b></span></div>
        <div class="film-source film-source-person"><span class="film-avatar">J</span><span>Your request<b>Jamie Morgan</b></span></div>
        <div class="film-source film-source-scope"><span class="film-source-icon">${mail}</span><span>Allowed for this call<b>Gmail · read only</b></span></div>
        <article class="film-brief-page">
          <div class="film-page-label">${documentIcon}<span>Call briefing</span><span class="film-page-corner">01</span></div>
          <h3>Lumen Home</h3><p class="film-brief-goal">Find the replacement delivery date.</p>
          <div class="film-brief-row"><span>Customer</span><strong>Jamie Morgan</strong></div>
          <div class="film-brief-row"><span>Order</span><strong>LH-4821</strong></div>
          <div class="film-brief-row"><span>May look up</span><strong>Gmail · delivery details</strong></div>
          <div class="film-brief-limit">${check}<span>Ask for status. No order changes.</span></div>
          <div class="film-brief-saved">${check}<strong>Saved before dialing</strong><span>call-brief.md</span></div>
        </article>
        <div class="film-dial-ready">${phone}<span>Calling Lumen Home</span><i></i></div>
      </section>
      <section class="film-shot film-call" data-shot="call" aria-label="Realtime answers from the brief, then hears an unexpected question">
        <div class="film-call-top"><span class="film-phone-icon">${phone}</span><div><h3>Lumen Home</h3><span>Customer service</span></div><span class="film-connected"><i></i> Connected</span></div>
        <div class="film-audio-bars" aria-hidden="true">${waves}</div>
        <div class="film-dialog film-dialog-order"><span class="film-speaker">LUMEN HOME</span><p>“What’s the order number?”</p></div>
        <div class="film-dialog film-dialog-reply"><span class="film-speaker">DOTS <b>FROM THE BRIEF</b></span><p>“LH-4821.<br>For Jamie Morgan.”</p></div>
        <div class="film-dialog film-dialog-missing"><span class="film-speaker">LUMEN HOME</span><p>“And the reference<br>from our delivery email?”</p></div>
        <div class="film-call-footer"><span>${documentIcon} Brief saved</span><span>Realtime voice<span class="film-small-wave" aria-hidden="true">▂▅▇▃▆▂</span></span></div>
      </section>
      <section class="film-shot film-route" data-shot="route" aria-label="Realtime asks Codex, which accesses the permitted Gmail tool">
        <div class="film-route-caption"><span class="film-kicker">LIVE LOOKUP</span><h3>“I’ll check that email.”</h3><span class="film-call-stays"><i></i> Call stays connected</span></div>
        <div class="film-map">
          <svg class="film-routes" viewBox="0 0 1000 500" preserveAspectRatio="none" aria-hidden="true">
            <path class="film-wire" d="M150 125H500H850 M500 125V375H850"/>
            <path class="film-route-brief" d="M150 125H500"/>
            <path class="film-route-call" d="M500 125H850"/>
            <path class="film-route-lookup" d="M500 125V375H850" pathLength="1"/>
            <circle class="film-route-dot" cx="500" cy="125" r="7"/>
          </svg>
          <div class="film-map-node film-map-dots">${marks}<strong>Dots</strong><span>Call briefing</span></div>
          <div class="film-map-node film-map-voice"><div class="film-map-wave" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div><strong>Realtime</strong><span>Conversation</span></div>
          <div class="film-map-node film-map-phone">${phone}<strong>Lumen Home</strong><span>Phone call</span></div>
          <div class="film-map-node film-map-codex"><span class="film-codex-icon" aria-hidden="true">⌘</span><strong>Codex</strong><span>Reasoning & tools</span></div>
          <div class="film-map-node film-map-gmail">${mail}<strong>Gmail</strong><span>Read access allowed</span></div>
          <span class="film-wire-label film-label-brief">prepared facts</span><span class="film-wire-label film-label-call">live audio</span><span class="film-wire-label film-label-question">delivery reference?</span><span class="film-wire-label film-label-tool">selected read tool</span>
        </div>
      </section>
      <section class="film-shot film-mail" data-shot="mail" aria-label="The permitted Gmail lookup finds delivery reference LM-7308">
        <div class="film-email-window">
          <div class="film-email-top">${mail}<strong>Gmail</strong><span class="film-email-search">Lumen Home · delivery</span><span class="film-email-account">J</span></div>
          <div class="film-email-toolbar"><span aria-hidden="true">←</span><span>Inbox</span><span class="film-read-only">Read only</span></div>
          <div class="film-email-body"><h3>Your replacement is on its way</h3><div class="film-email-sender"><span class="film-lumen-logo">L</span><div><strong>Lumen Home</strong><span>Delivery team · to Jamie</span></div><span>Today</span></div>
            <div class="film-email-copy"><p>Hi Jamie,</p><p>Your replacement washing machine is with our delivery team.</p><div class="film-email-reference"><span>DELIVERY REFERENCE</span><mark>LM-7308</mark><span class="film-extract-bracket" aria-hidden="true"></span></div><p class="film-email-signoff">Thanks,<br>The Lumen Home team</p></div>
          </div>
        </div>
        <div class="film-mail-status"><span class="film-codex-small">⌘</span><span>Codex found the delivery reference</span>${check}</div>
      </section>
      <section class="film-shot film-answer" data-shot="answer" aria-label="The delivery reference returns to the live phone conversation">
        <div class="film-answer-top"><span class="film-phone-icon">${phone}</span><span>Lumen Home</span><span class="film-connected"><i></i> Still connected</span></div>
        <div class="film-answer-line"><span class="film-speaker">DOTS <b>FROM GMAIL</b></span><p>“The reference is<br><span>LM-7308.</span>”</p></div>
        <div class="film-answer-confirm"><span class="film-speaker">LUMEN HOME</span><p>“Friday, 9 am to 1 pm.<br>The driver will call ahead.”</p></div>
        <div class="film-answer-source">${mail}<span>Delivery email</span><i aria-hidden="true">→</i><span class="film-codex-small">⌘</span><span>Codex</span><i aria-hidden="true">→</i><span>Realtime voice</span></div>
      </section>
      <section class="film-shot film-result" data-shot="result" aria-label="Dots reports the confirmed delivery window to Jamie">
        <div class="film-result-label">${check}<span>DELIVERY CONFIRMED</span></div>
        <div class="film-result-date"><strong>Friday</strong><span>9<span class="film-result-dash">—</span>1<small>AM &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; PM</small></span></div>
        <div class="film-result-message">${marks}<p>Your replacement arrives Friday, 9 am–1 pm.<br><span>The driver will call ahead.</span></p><span class="film-result-sent">Sent to Jamie ${check}</span></div>
        <span class="film-result-review">Result checked against the call</span>
        <div class="film-result-ticks" aria-hidden="true">${Array.from({length: 12}, (_, i) => `<i data-tick="${i}"></i>`).join("")}</div>
      </section>
      <div class="film-travel-token" aria-hidden="true"><span>${mail}</span><strong>LM-7308</strong><i>${check}</i></div>
      <div class="film-cut-line" aria-hidden="true"></div>
      <div class="film-caption" aria-live="off">Fictional demo · silent · timing compressed</div>
    </div>
    <div class="film-player">
      <div class="film-transport"><button class="film-play" type="button" aria-label="Play demo"><span class="film-play-symbol" aria-hidden="true">▶</span><span class="film-play-label">Play</span></button><button class="film-replay" type="button" aria-label="Replay demo">↻</button><span class="film-time"><span class="film-time-current">00:00</span><span> / 00:24</span></span><label class="film-sr-only" for="film-scrub">Demo position in seconds</label><input id="film-scrub" class="film-scrub" type="range" min="0" max="24" step="0.05" value="0" aria-valuetext="0 seconds, The request"><button class="film-fullscreen" type="button" aria-label="Enter fullscreen"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5" stroke="currentColor" stroke-width="1.5"/></svg></button></div>
      <div class="film-chapters" role="group" aria-label="Demo chapters">${chapters.map((chapter, i) => `<button type="button" data-chapter="${i}" aria-pressed="${i === 0}"><span>0${i + 1}</span>${chapter.label}<i></i></button>`).join("")}</div>
    </div>
    <p class="film-sr-only film-screen-reader-status" role="status">Demo paused at the request.</p>`;

  const $ = (selector) => mount.querySelector(selector);
  const $$ = (selector) => [...mount.querySelectorAll(selector)];
  const screen = $(".film-screen");
  const shotElements = $$(".film-shot");
  const waveElements = $$("[data-wave]");
  const scrub = $(".film-scrub");
  const playButton = $(".film-play");
  const chapterButtons = $$("[data-chapter]");
  const token = $(".film-travel-token");
  let elapsed = 0;
  let playing = false;
  let frame = null;
  let lastTime = null;
  let autoPlayed = false;
  let inView = false;
  let disposed = false;
  let currentScene = -1;
  let lastReadout = -1;
  const clamp = (n, low = 0, high = 1) => Math.max(low, Math.min(high, n));
  const progress = (t, start, end) => clamp((t - start) / (end - start));
  const ease = (n) => 1 - (1 - clamp(n)) ** 3;
  const smooth = (n) => { const x = clamp(n); return x * x * (3 - 2 * x); };
  function visual(element, opacity, transform = "none") {
    element.style.opacity = String(clamp(opacity));
    element.style.transform = reducedMotion.matches ? "none" : transform;
    element.style.visibility = opacity > 0.001 ? "visible" : "hidden";
  }
  function reveal(selector, t, start = 0, span = 0.45, offset = 24) {
    const element = $(selector);
    const p = reducedMotion.matches ? (t >= start ? 1 : 0) : ease(progress(t, start, start + span));
    visual(element, p, `translate3d(0,${(1 - p) * offset}px,0)`);
    return p;
  }
  function render(time) {
    elapsed = clamp(time, 0, DURATION);
    const index = scenes.findIndex((scene) => elapsed < scene.end);
    const active = index === -1 ? scenes.length - 1 : index;
    const scene = scenes[active];
    const local = elapsed - scene.at;
    const entering = reducedMotion.matches ? 1 : ease(progress(local, 0, 0.36));
    mount.dataset.scene = scene.name;
    mount.dataset.playing = String(playing);
    mount.dataset.reducedMotion = String(reducedMotion.matches);
    const sceneChanged = active !== currentScene;
    if (sceneChanged) {
      currentScene = active;
      $("#film-shot-label").textContent = scene.label;
      shotElements.forEach((element, i) => {
        element.hidden = i !== active;
        element.setAttribute("aria-hidden", String(i !== active));
      });
    }
    const current = shotElements[active];
    current.style.opacity = "1";
    current.style.transform = reducedMotion.matches ? "none" : `translate3d(0,${(1 - entering) * 20}px,0) scale(${0.985 + entering * 0.015})`;
    const chapter = chapters.findLastIndex((item) => elapsed >= item.at);
    chapterButtons.forEach((button, i) => {
      button.setAttribute("aria-pressed", String(i === chapter));
      const end = chapters[i + 1]?.at ?? DURATION;
      button.style.setProperty("--chapter-progress", String(progress(elapsed, chapters[i].at, end)));
    });
    scrub.value = elapsed.toFixed(2);
    scrub.style.setProperty("--scrub-progress", `${elapsed / DURATION * 100}%`);
    const second = Math.floor(elapsed);
    if (second !== lastReadout || sceneChanged) {
      lastReadout = second;
      $(".film-time-current").textContent = `00:${String(second).padStart(2, "0")}`;
      scrub.setAttribute("aria-valuetext", `${second} seconds, ${scene.label}`);
    }
    // A crisp wipe bridges each cut; no perpetual background animation.
    const cut = !reducedMotion.matches && local < 0.4 ? Math.sin(progress(local, 0, 0.4) * Math.PI) : 0;
    $(".film-cut-line").style.opacity = String(cut * 0.5);
    $(".film-cut-line").style.transform = `translate3d(${progress(local, 0, 0.4) * 110 - 5}vw,0,0)`;
    visual(token, 0);

    if (scene.name === "ask") {
      const p = ease(progress(local, 0, 0.7));
      visual($(".film-request-window"), 1, `translate3d(0,${(1 - p) * 65}px,0) rotateX(${(1 - p) * 9}deg) scale(${0.91 + p * 0.09})`);
      reveal(".film-request-meta", local, 0.45, 0.4, 14);
      reveal(".film-request-bottom", local, 0.7, 0.3, 8);
      reveal(".film-request-reply", local, 1.45, 0.45, 18);
      const typed = ease(progress(local, 0.15, 0.85));
      $(".film-request-remainder").style.opacity = String(typed);
      $(".film-caret").style.opacity = local < 1.25 ? "1" : "0";
      $(".film-composer-status").textContent = local < 1.25 ? "Writing to Dots" : "Sent just now";
      const pointer = ease(progress(local, 0.5, 1.25));
      visual($(".film-pointer"), local < 1.6 ? progress(local, 0.5, 0.75) : 0, `translate3d(${(1 - pointer) * 75}px,${(1 - pointer) * 80}px,0)`);
      const press = Math.sin(progress(local, 1.2, 1.5) * Math.PI);
      $(".film-send").style.transform = reducedMotion.matches ? "none" : `scale(${1 - press * 0.2})`;
      $(".film-send").classList.toggle("film-sent", local > 1.4);
    }
    if (scene.name === "brief") {
      const p = ease(progress(local, 0, 0.7));
      visual($(".film-brief-page"), 1, `translate3d(${(1 - p) * 55}px,0,0) rotateY(${(1 - p) * -12}deg) scale(${0.9 + p * 0.1})`);
      $$(".film-source").forEach((element, i) => {
        const arrive = ease(progress(local, 0.15 + i * 0.2, 0.6 + i * 0.2));
        const gather = ease(progress(local, 1.2 + i * 0.2, 1.75 + i * 0.2));
        visual(element, arrive * (1 - gather), `translate3d(${(1 - arrive) * -90 + gather * 180}px,${gather * (i === 0 ? 45 : i === 2 ? -45 : 0)}px,0) scale(${1 - gather * 0.35})`);
      });
      $$(".film-brief-row").forEach((element, i) => {
        const pRow = reducedMotion.matches ? 1 : ease(progress(local, 0.65 + i * 0.32, 1.1 + i * 0.32));
        visual(element, pRow, `translate3d(${(1 - pRow) * 20}px,0,0)`);
      });
      reveal(".film-brief-limit", local, 1.5, 0.35, 10);
      reveal(".film-brief-saved", local, 2.05, 0.35, 12);
      reveal(".film-dial-ready", local, 2.65, 0.4, 20);
    }
    if (scene.name === "call") {
      const state = local < 1.2 ? "order" : local < 2.95 ? "reply" : "missing";
      ["order", "reply", "missing"].forEach((name) => {
        const start = name === "order" ? 0 : name === "reply" ? 1.2 : 2.95;
        const p = ease(progress(local, start, start + 0.28));
        visual($(`.film-dialog-${name}`), name === state ? p : 0, `translate3d(0,${(1 - p) * 28}px,0)`);
      });
      mount.dataset.speaker = state === "reply" ? "dots" : "recipient";
      waveElements.forEach((element, i) => {
        const envelope = Math.sin((i + 1) / 55 * Math.PI);
        const amplitude = reducedMotion.matches ? 0.45 : 0.15 + Math.abs(Math.sin(local * 6.4 + i * 0.55) * Math.cos(i * 0.33 + local * 2.2)) * 0.85;
        element.style.transform = `scaleY(${0.08 + envelope * amplitude})`;
      });
    }
    if (scene.name === "route") {
      const zoom = ease(progress(local, 0, 0.65));
      visual($(".film-map"), 1, `translate3d(0,${(1 - zoom) * 40}px,0) scale(${1.45 - zoom * 0.45})`);
      const lookup = progress(local, 0.6, 2.0);
      $(".film-route-lookup").style.strokeDashoffset = String(1 - lookup);
      const dot = $(".film-route-dot");
      const distance = lookup * 600;
      dot.setAttribute("cx", String(distance < 250 ? 500 : 500 + distance - 250));
      dot.setAttribute("cy", String(distance < 250 ? 125 + distance : 375));
      $(".film-map-codex").classList.toggle("film-node-active", local > 0.9);
      $(".film-map-gmail").classList.toggle("film-node-active", local > 1.65);
      reveal(".film-route-caption", local, 0.1, 0.4, -12);
    }
    if (scene.name === "mail") {
      const p = ease(progress(local, 0, 0.55));
      visual($(".film-email-window"), 1, `translate3d(${(1 - p) * 75}px,${(1 - p) * 25}px,0) scale(${0.76 + p * 0.24}) rotateY(${(1 - p) * -9}deg)`);
      const highlight = ease(progress(local, 1.0, 1.45));
      $(".film-email-reference").style.setProperty("--highlight", String(highlight));
      reveal(".film-mail-status", local, 1.55, 0.35, 12);
      if (local > 2.55) {
        const lift = ease(progress(local, 2.55, 3.8));
        const mobile = screen.clientWidth < 620;
        visual(token, progress(local, 2.55, 2.75), `translate3d(-50%,-50%,0) rotate(${(1 - lift) * -3}deg) scale(${0.7 + lift * 0.3})`);
        token.style.left = `${mobile ? 50 : 49 + lift}%`;
        token.style.top = `${(mobile ? 61 : 64) - lift * 10}%`;
        $(".film-email-window").style.opacity = String(1 - lift * 0.5);
      }
    }
    if (scene.name === "answer") {
      const settle = ease(progress(local, 0, 0.55));
      visual(token, 1 - settle, `translate3d(-50%,-50%,0) scale(${1 + settle * 0.18})`);
      token.style.left = "50%"; token.style.top = `${54 - settle * 6}%`;
      reveal(".film-answer-line", local, 0.15, 0.4, 20);
      reveal(".film-answer-confirm", local, 1.0, 0.35, 22);
      reveal(".film-answer-source", local, 0.35, 0.4, 12);
    }
    if (scene.name === "result") {
      const p = ease(progress(local, 0, 0.7));
      visual($(".film-result-date"), 1, `translate3d(0,${(1 - p) * 70}px,0) scale(${1.17 - p * 0.17})`);
      reveal(".film-result-label", local, 0.15, 0.4, -15);
      reveal(".film-result-message", local, 0.85, 0.45, 25);
      reveal(".film-result-review", local, 1.2, 0.35, 10);
      $$("[data-tick]").forEach((tick, i) => {
        const pTick = progress(local, 0.15, 1.7);
        const angle = i / 12 * Math.PI * 2;
        const distance = 30 + ease(pTick) * 200;
        visual(tick, reducedMotion.matches ? 0 : Math.sin(pTick * Math.PI) * 0.7, `translate3d(${Math.cos(angle) * distance}px,${Math.sin(angle) * distance * 0.68}px,0) rotate(${i * 30}deg)`);
      });
    }
  }

  function updatePlayback() {
    mount.dataset.playing = String(playing);
    playButton.setAttribute("aria-label", playing ? "Pause demo" : elapsed >= DURATION ? "Replay demo" : "Play demo");
    $(".film-play-symbol").textContent = playing ? "Ⅱ" : elapsed >= DURATION ? "↻" : "▶";
    $(".film-play-label").textContent = playing ? "Pause" : elapsed >= DURATION ? "Replay" : "Play";
  }
  function pause(announce = false) {
    playing = false;
    lastTime = null;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    updatePlayback();
    if (announce) $(".film-screen-reader-status").textContent = `Paused at ${Math.floor(elapsed)} seconds. ${scenes[currentScene]?.label ?? "The request"}.`;
  }
  function tick(now) {
    if (!playing || disposed) return;
    if (lastTime !== null) elapsed += Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    render(elapsed);
    if (elapsed >= DURATION) {
      pause();
      $(".film-screen-reader-status").textContent = "Demo complete. The replacement arrives Friday, 9 am to 1 pm.";
      return;
    }
    frame = requestAnimationFrame(tick);
  }
  function play() {
    if (elapsed >= DURATION) elapsed = 0;
    autoPlayed = true;
    playing = true;
    lastTime = null;
    updatePlayback();
    render(elapsed);
    if (frame === null) frame = requestAnimationFrame(tick);
  }
  function seek(time, announce = true) {
    pause();
    autoPlayed = true;
    render(time);
    updatePlayback();
    if (announce) $(".film-screen-reader-status").textContent = `${scenes[currentScene].label}. ${Math.floor(elapsed)} seconds.`;
  }
  playButton.addEventListener("click", () => playing ? pause(true) : play());
  $(".film-replay").addEventListener("click", () => { pause(); elapsed = 0; play(); });
  scrub.addEventListener("input", () => seek(Number(scrub.value), false));
  scrub.addEventListener("change", () => seek(Number(scrub.value)));
  chapterButtons.forEach((button, index) => {
    button.addEventListener("click", () => seek(chapters[index].preview));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? 0 : event.key === "End" ? chapters.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + chapters.length) % chapters.length;
      chapterButtons[target].focus();
      seek(chapters[target].preview);
    });
  });
  screen.addEventListener("keydown", (event) => {
    if (event.target !== screen) return;
    if (event.code === "Space" || event.key === "k") { event.preventDefault(); playing ? pause(true) : play(); }
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); seek(elapsed + (event.key === "ArrowRight" ? 1 : -1)); }
    if (event.key === "Home" || event.key === "End") { event.preventDefault(); seek(event.key === "Home" ? 0 : DURATION); }
  });
  const fullscreen = $(".film-fullscreen");
  if (!mount.requestFullscreen) fullscreen.hidden = true;
  fullscreen.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement === mount) await document.exitFullscreen();
      else await mount.requestFullscreen();
    } catch { $(".film-screen-reader-status").textContent = "Fullscreen is unavailable in this browser."; }
  });
  document.addEventListener("fullscreenchange", () => {
    fullscreen.setAttribute("aria-label", document.fullscreenElement === mount ? "Exit fullscreen" : "Enter fullscreen");
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
  reducedMotion.addEventListener("change", () => { if (reducedMotion.matches) pause(); render(elapsed); });
  const observer = new IntersectionObserver((entries) => {
    inView = entries[0].intersectionRatio >= 0.45;
    if (!inView) pause();
    else if (!autoPlayed && !reducedMotion.matches && !document.hidden) play();
  }, { threshold: [0, 0.45] });
  observer.observe(screen);
  window.addEventListener("pagehide", () => { pause(); observer.disconnect(); disposed = true; });
  window.addEventListener("pageshow", (event) => { if (event.persisted) { disposed = false; observer.observe(screen); render(elapsed); } });
  render(0);
  updatePlayback();
})();
