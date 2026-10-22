#!/usr/bin/env node
// Editable launch artwork. This script only writes local SVG/PNG artifacts.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const folder = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(folder, '../../..');
const sharpModule = process.env.DOTS_LAUNCH_SHARP_PATH
  ? await import(pathToFileURL(process.env.DOTS_LAUNCH_SHARP_PATH).href)
  : await import('sharp');
const sharp = sharpModule.default;
const c = { bg: '#090b0d', paper: '#f3f4ed', lime: '#d5ff70', muted: '#a6afaa',
  quiet: '#79867d', line: '#2b342f', violet: '#c6a7ff' };
const esc = (s) => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const rect = (x, y, w, h, fill, stroke = 'none', radius = 0) =>
  `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${radius}" fill="${fill}" stroke="${stroke}"/>`;
const line = (x1, y1, x2, y2, color = c.line, width = 1) =>
  `<path d="M${x1} ${y1} L${x2} ${y2}" fill="none" stroke="${color}" stroke-width="${width}"/>`;
const txt = (x, y, s, size = 24, fill = c.paper, weight = 400, spacing = 0) =>
  `<text x="${x}" y="${y}" font-size="${size}" font-weight="${weight}" letter-spacing="${spacing}" fill="${fill}">${esc(s)}</text>`;
const multi = (x, y, rows, size, fill = c.paper, leading = 1.2, weight = 400, spacing = 0) =>
  rows.map((s, i) => txt(x, y + i * size * leading, s, size, fill, weight, spacing)).join('');
const logo = (x, y, diameter = 9, gap = 5, color = c.lime) =>
  [0, 1].map((r) => [0, 1].map((col) => `<circle cx="${x + col * (diameter + gap) + diameter / 2}" cy="${y + r * (diameter + gap) + diameter / 2}" r="${diameter / 2}" fill="${color}"/>`).join('')).join('');
const pill = (x, y, w, label, fill = '#141c12', color = c.lime, stroke = '#3d5026') =>
  rect(x, y, w, 34, fill, stroke, 17) + txt(x + 15, y + 22, label, 11, color, 500, 1.1);
const arrow = (x1, y, x2, color = '#6d7c65') =>
  line(x1, y, x2, y, color, 1.5) + `<path d="M${x2 - 7} ${y - 5} L${x2} ${y} L${x2 - 7} ${y + 5}" fill="none" stroke="${color}" stroke-width="1.5"/>`;
const phone = (x, y, size = 24, color = c.lime) =>
  `<svg x="${x}" y="${y}" width="${size}" height="${size}" viewBox="0 0 24 24"><path d="M5.7 3.5 9 7.8 7.3 10c1.2 2.7 3.1 4.6 5.9 5.8l2.2-1.7 4.4 3.3c-1 4.7-4.8 3.8-8.6 1.6C7 16.7 3.5 12.9 3.2 7.8 3.1 5.8 4 4 5.7 3.5Z" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
const wave = (x, y, color = c.lime) =>
  [13, 25, 38, 20, 43, 29, 15].map((h, i) => `<rect x="${x + i * 9}" y="${y + (43 - h) / 2}" width="3" height="${h}" rx="1.5" fill="${color}"/>`).join('');

function svg(width, height, content) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">
  <defs><radialGradient id="haze"><stop stop-color="#19321b" stop-opacity=".65"/><stop offset="1" stop-color="#090b0d" stop-opacity="0"/></radialGradient></defs>
  <style>text{font-family:Arial,Helvetica,sans-serif} .mono{font-family:Menlo,monospace}</style>
  ${rect(0, 0, width, height, c.bg)}${content}</svg>`;
}

function frame(number, section, content, footer = 'SELF-HOSTED EXPERIMENTAL PREVIEW') {
  return svg(1270, 760,
    `<ellipse cx="1050" cy="390" rx="560" ry="510" fill="url(#haze)"/>` +
    logo(62, 49) + txt(99, 70, 'dots', 31, c.paper, 600, -1.2) + txt(174, 69, '/ phone', 17, c.muted) +
    txt(923, 66, section, 11, c.muted, 500, 1.4) + line(62, 98, 1208, 98) +
    content + line(62, 686, 1208, 686) +
    txt(62, 719, footer, 11, c.muted, 400, 1.1) + txt(1117, 719, `${number} / 05`, 11, c.quiet, 400, 1.5));
}

const gallery = [
  {
    name: '01-let-dots-call',
    svg: frame('01', 'BUILT FOR OPENAI DOTS',
      txt(62, 205, 'Let Dots', 73, c.paper, 500, -4.2) + txt(62, 279, 'make the call.', 73, c.lime, 500, -4.2) +
      multi(66, 385, ['An independent plugin for OpenAI Dots.', 'For errands that still need a call.'], 25, c.muted, 1.48) +
      pill(66, 520, 150, 'OPEN SOURCE') + pill(229, 520, 176, 'YOUR ACCOUNTS') +
      txt(698, 151, 'FICTIONAL WALKTHROUGH', 10, c.quiet, 500, 1.8) +
      rect(674, 175, 534, 409, '#121911', '#3e5033', 17) +
      logo(706, 205, 6, 3) + txt(736, 220, 'YOU → OPENAI DOTS', 10, c.muted, 500, 1.5) +
      multi(706, 290, ['Find out when my', 'replacement washing', 'machine will arrive.'], 34, c.paper, 1.2, 400, -1) +
      line(706, 435, 1176, 435, '#35432d') +
      txt(706, 473, 'BEFORE DIALING', 10, c.lime, 500, 1.6) +
      multi(706, 512, ['Dots gathers the details', 'and saves a call brief.'], 22, '#aaba9f', 1.4)),
  },
  {
    name: '02-brief-first',
    svg: frame('02', 'PREPARE BEFORE DIALING',
      multi(62, 204, ['A clear brief.', 'Then the call.'], 67, c.paper, 1.05, 500, -3.6) +
      multi(66, 390, ['Set the goal, share the details,', 'and define what Dots may', 'say and agree to.'], 25, c.muted, 1.5) +
      pill(66, 548, 251, 'SAVED BEFORE DIALING') +
      rect(640, 148, 568, 476, '#edf2e4', '#edf2e4', 13) +
      txt(674, 185, 'FICTIONAL EXAMPLE · CALL BRIEF', 10, '#64725a', 500, 1.3) +
      txt(674, 242, 'Delivery update', 42, '#25321f', 500, -1.5) +
      line(674, 269, 1174, 269, '#ccd6bf') +
      txt(674, 304, 'GOAL', 10, '#7c886f', 500, 1.4) +
      multi(674, 340, ['Ask for the replacement', 'washing-machine delivery window.'], 23, '#33422b', 1.35) +
      line(674, 397, 1174, 397, '#ccd6bf') +
      txt(674, 432, 'DETAILS TO SHARE', 10, '#7c886f', 500, 1.4) +
      txt(674, 469, 'Order details supplied for this task.', 23, '#33422b') +
      rect(666, 514, 516, 82, '#dce9ca', '#c9dab2', 7) +
      txt(686, 544, 'LIMITS', 10, '#637a4a', 500, 1.4) +
      txt(686, 575, 'No purchases or changes.', 25, '#33422b', 500)),
  },
  {
    name: '03-help-during-the-call',
    svg: frame('03', 'HELP WHEN IT IS NEEDED',
      multi(62, 193, ['Missing a detail?', 'Bring in Codex.'], 66, c.paper, 1.06, 500, -3.5) +
      multi(756, 181, ['Realtime handles the conversation.', 'Codex can look up missing facts', 'using permitted read tools.'], 23, c.muted, 1.52) +
      txt(66, 348, 'WORKFLOW ILLUSTRATION', 10, c.quiet, 500, 1.8) +
      rect(62, 380, 270, 205, '#111a0e', '#536d35', 13) + wave(85, 405) +
      txt(86, 491, 'Realtime', 31, c.paper, 500, -1) +
      txt(86, 529, 'Voice through Twilio', 17, '#a8b99b') +
      txt(86, 556, 'Answers from the brief', 15, c.quiet) +
      arrow(345, 480, 451, c.violet) + txt(346, 455, 'when needed', 12, '#ab97c8') +
      rect(466, 380, 286, 205, '#171121', '#514061', 13) +
      txt(491, 443, '↗', 36, c.violet) + txt(491, 491, 'Codex', 31, c.paper, 500, -1) +
      txt(491, 529, 'Reasoning and lookups', 17, '#b6a7c8') +
      txt(491, 556, 'Reviews the result', 15, '#8e7da3') +
      arrow(765, 480, 871, c.violet) + txt(771, 455, 'allowed reads', 12, '#ab97c8') +
      rect(886, 380, 322, 205, '#151819', '#3e4645', 13) +
      txt(911, 441, '↔', 35, '#b0b9b5') + txt(911, 491, 'Connected tools', 29, c.paper, 500, -1) +
      txt(911, 529, 'Gmail, Yahoo and more', 17, '#a8b1ad') +
      txt(911, 556, 'Optional, authenticated access', 15, c.quiet) +
      txt(66, 630, 'No inbox access is required to start. Installing Dots does not grant access to your email.', 16, c.muted)),
  },
  {
    name: '04-the-answer-back',
    svg: frame('04', 'BACK TO YOU, WITH AN ANSWER',
      multi(62, 205, ['Get the answer.', 'Then get on with it.'], 60, c.paper, 1.08, 500, -3.3) +
      multi(66, 398, ['Dots reports what the other side', 'confirmed, after the result', 'has been reviewed.'], 24, c.muted, 1.48) +
      pill(66, 548, 258, 'FICTIONAL WALKTHROUGH') +
      rect(674, 168, 534, 425, '#121b0f', '#455b31', 17) +
      txt(706, 207, 'FICTIONAL RESULT · NOT A REAL CALL', 10, c.muted, 500, 1.3) +
      txt(706, 288, 'Friday', 57, c.paper, 500, -2.4) +
      txt(706, 350, '9 am – 1 pm', 53, c.lime, 400, -2.5) +
      line(706, 387, 1176, 387, '#354b27') +
      logo(706, 419, 6, 3) + txt(735, 434, 'DOTS → YOU', 10, c.muted, 500, 1.5) +
      multi(706, 482, ['In this scripted example,', 'the retailer confirms the', 'replacement delivery window.'], 23, '#b2c3a5', 1.4)),
  },
  {
    name: '05-your-setup',
    svg: frame('05', 'A PREVIEW YOU RUN YOURSELF',
      multi(62, 195, ['Your machine.', 'Your scope.'], 67, c.paper, 1.04, 500, -3.5) +
      multi(66, 386, ['Open source. Self-hosted.', 'Built for technical early adopters.'], 24, c.muted, 1.48) +
      pill(66, 530, 244, 'EXPERIMENTAL PREVIEW') +
      txt(671, 171, 'WHAT YOU BRING', 11, c.lime, 500, 1.7) +
      [
        ['01', 'A local service', 'Docker Compose, uv and Node'],
        ['02', 'OpenAI API and Twilio', 'Your accounts, keys and phone number'],
        ['03', 'Codex access', 'Signed-in worker and plugin registration'],
        ['04', 'HTTPS / WSS tunnels', 'Connectivity for ChatGPT and Twilio'],
      ].map(([n, title, detail], i) => {
        const y = 221 + i * 96;
        return txt(671, y, n, 11, '#596c4a', 500, 1.2) + txt(711, y + 1, title, 26, c.paper, 500, -.6) +
          txt(711, y + 31, detail, 16, c.muted) + line(671, y + 55, 1208, y + 55);
      }).join('') +
      txt(66, 630, 'MIT-licensed code. Provider charges apply. Model requests go to OpenAI; phone audio goes through Twilio.', 16, c.muted)),
  },
];

// Use a simple solid four-dot mark: it stays legible at Product Hunt's small size.
const thumbnail = svg(240, 240,
  rect(12, 12, 216, 216, '#10170e', '#283621', 48) + logo(59, 59, 48, 26));
const social = svg(1200, 630,
  `<ellipse cx="950" cy="300" rx="530" ry="470" fill="url(#haze)"/>` +
  logo(62, 48, 8, 4) + txt(96, 69, 'dots', 31, c.paper, 600, -1.2) + txt(170, 68, '/ phone', 17, c.muted) +
  txt(879, 66, 'BUILT FOR OPENAI DOTS', 11, c.lime, 500, 1.4) +
  line(62, 97, 1138, 97) +
  txt(62, 224, 'Let Dots', 88, c.paper, 500, -5.5) + txt(62, 314, 'make the call.', 88, c.lime, 500, -5.5) +
  txt(66, 448, 'An independent phone plugin for OpenAI Dots.', 26, c.muted) +
  pill(66, 520, 150, 'OPEN SOURCE') + pill(230, 520, 208, 'SELF-HOSTED PREVIEW') +
  `<g transform="translate(925 215) rotate(-12 80 80)">${logo(0, 0, 66, 25)}</g>` +
  txt(66, 597, 'YOUR ACCOUNTS · PROVIDER CHARGES APPLY', 10, c.quiet, 500, 1.7));

await fs.mkdir(folder, { recursive: true });
for (const { name, svg: source } of [...gallery, { name: 'thumbnail', svg: thumbnail }, { name: 'social-card', svg: social }]) {
  await fs.writeFile(path.join(folder, `${name}.svg`), source);
  await sharp(Buffer.from(source)).png().toFile(path.join(folder, `${name}.png`));
}

const privateFolder = path.join(root, 'artifacts/product-hunt-20261020/launch-kit');
await fs.mkdir(privateFolder, { recursive: true });
const cells = await Promise.all(gallery.map(async ({ name }, i) => ({
  input: await sharp(path.join(folder, `${name}.png`)).resize(635, 380).toBuffer(),
  left: (i % 2) * 635, top: Math.floor(i / 2) * 380,
})));
await sharp({ create: { width: 1270, height: Math.ceil(gallery.length / 2) * 380, channels: 3, background: c.bg } })
  .composite(cells).png().toFile(path.join(privateFolder, 'gallery-contact-sheet.png'));
console.log('Wrote five gallery PNG/SVG pairs, thumbnail, social card and private contact sheet.');
