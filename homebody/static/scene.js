/* Homebody scenes: one small illustrated-stage component for live state (see docs/scenes.md).

   Rendering only. A scene never fetches, stores, wakes, listens, records, moves or speaks; it
   draws authored SVG for a categorical view that callers derive from already-redacted status.
   Looping motion is CSS-only and runs only in live phases while the stage is on screen, the
   page is visible and reduced motion is off. Identical updates are ignored. */
(() => {
  const ACTIVITIES = ["buddy", "story", "quiz", "riddles", "calm", "ispy"];
  const PHASES = ["preview", "waiting", "listening", "thinking", "speaking", "searching", "paused", "trouble", "offline", "ending", "ended"];
  // Only these phases loop. Everything else is a composed still, so an idle or ended stage costs nothing.
  const LIVE_PHASES = new Set(["waiting", "listening", "thinking", "speaking", "searching"]);
  const SWAP_MS = 520;
  const CUE_MS = 900;

  /* ---------- Shared character ---------- */

  // Reachy, drawn from the same proportions as the dashboard mark: head, visor, two eyes, antennas.
  function bot(p, x, y, s, extra = "") {
    return `<g class="hb-bot" transform="translate(${x} ${y}) scale(${s})">
      <ellipse class="hb-bot-shadow" cx="0" cy="93" rx="60" ry="8"/>
      <path class="hb-bot-body" d="M-46 92c0-33 18-49 46-49s46 16 46 49z" fill="url(#${p}-body)"/>
      <path class="hb-bot-collar" d="M-26 50q26 8 52 0"/>
      <g class="hb-bot-head">
        <g class="hb-bot-antenna hb-bot-antenna-l"><path d="M-24-38-36-70"/><circle cx="-37" cy="-73" r="6.5"/></g>
        <g class="hb-bot-antenna hb-bot-antenna-r"><path d="M24-38 36-70"/><circle cx="37" cy="-73" r="6.5"/></g>
        <rect class="hb-bot-shell" x="-54" y="-44" width="108" height="90" rx="40" fill="url(#${p}-shell)"/>
        <rect class="hb-bot-visor" x="-40" y="-22" width="80" height="44" rx="22"/>
        <rect class="hb-bot-visor-glow" x="-40" y="-22" width="80" height="44" rx="22" fill="url(#${p}-visor)"/>
        <g class="hb-bot-gaze">
          <circle class="hb-bot-eye-glow hb-bot-eye-l" cx="-16" cy="0" r="13"/><circle class="hb-bot-eye-glow hb-bot-eye-r" cx="16" cy="0" r="13"/>
          <circle class="hb-bot-eye hb-bot-eye-l" cx="-16" cy="0" r="8"/><circle class="hb-bot-eye hb-bot-eye-r" cx="16" cy="0" r="8"/>
        </g>
        <path class="hb-bot-shine" d="M-38-12c3-7 9-10 16-10"/>
        ${extra}
      </g>
    </g>`;
  }

  function botDefs(p) {
    return `<linearGradient id="${p}-shell" x1="0" y1="0" x2="0" y2="1"><stop class="hb-shell-a" offset="0"/><stop class="hb-shell-b" offset="1"/></linearGradient>
      <linearGradient id="${p}-body" x1="0" y1="0" x2="0" y2="1"><stop class="hb-body-a" offset="0"/><stop class="hb-body-b" offset="1"/></linearGradient>
      <radialGradient id="${p}-visor" cx=".5" cy=".5" r=".6"><stop class="hb-visor-a" offset="0"/><stop class="hb-visor-b" offset="1"/></radialGradient>`;
  }

  // Deterministic speckle so every render of a scene is identical.
  function dots(points, cls, r = 1.4) {
    return points.map(([x, y, scale = 1], index) => `<circle class="${cls}" style="--i:${index}" cx="${x}" cy="${y}" r="${(r * scale).toFixed(2)}"/>`).join("");
  }

  /* ---------- Scenes ----------
     Each scene is an authored composition: back layers, Reachy, then phase layers (`hb-fx-*`) that
     are visible as stills and only loop in live phases. Copy describes what Reachy is doing, never
     what was said, guessed or chosen. */

  const SCENES = {
    buddy: {
      label: "Buddy chat",
      tagline: "A warm chat under a golden-hour sky",
      copy: {
        listening: "Reachy is all ears.",
        thinking: "Finding the right words.",
        speaking: "Reachy is talking with you.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#21142a"/><stop offset=".5" stop-color="#55283d"/><stop offset=".86" stop-color="#b65a45"/><stop offset="1" stop-color="#d77a4c"/></linearGradient>
          <radialGradient id="${p}-sun" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#ffe0a0" stop-opacity=".9"/><stop offset=".3" stop-color="#ffb46b" stop-opacity=".45"/><stop offset="1" stop-color="#ff8a5c" stop-opacity="0"/></radialGradient>
          <radialGradient id="${p}-spark" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#fff" stop-opacity=".9"/><stop offset=".35" stop-color="currentColor" stop-opacity=".55"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></radialGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-sky)"/>
        <circle class="hb-buddy-sun" cx="200" cy="186" r="128" fill="url(#${p}-sun)"/>
        <g class="hb-twinkles">${dots([[38, 30], [92, 54, .8], [146, 22, 1.1], [258, 36, .8], [322, 22, 1.2], [364, 60, .9], [60, 92, .7], [344, 104, .7]], "hb-star")}</g>
        <path d="M0 192c58-26 108-20 160-6 58 15 108-16 168-14 30 1 54 9 72 15v73H0z" fill="#46213a"/>
        <path d="M0 212c70-18 132-10 200 0 70 10 130-14 200-8v56H0z" fill="#311729"/>
        <g transform="translate(200 128) rotate(-8)">
          <path class="hb-orbit-back" d="M-150 0a150 34 0 0 1 300 0"/>
        </g>
        <path d="M84 260c22-34 64-46 116-46s94 12 116 46z" fill="#2b1426"/>
        <path class="hb-rimlight" d="M104 240c24-16 58-24 96-24s72 8 96 24"/>
        ${bot(p, 200, 157, 0.62)}
        <g transform="translate(200 128) rotate(-8)">
          <path class="hb-orbit-front" d="M-150 0a150 34 0 0 0 300 0"/>
          <g class="hb-orb hb-orb-child" style="--ox:-128px;--oy:14px;color:#7be0c0"><g class="hb-orb-x"><g class="hb-orb-y"><circle r="16" fill="url(#${p}-spark)"/><circle class="hb-orb-core" r="5.5"/></g></g></g>
          <g class="hb-orb hb-orb-bot" style="--ox:132px;--oy:-10px;color:#ffd15c"><g class="hb-orb-x"><g class="hb-orb-y"><circle r="16" fill="url(#${p}-spark)"/><circle class="hb-orb-core" r="5.5"/></g></g></g>
        </g>
        <g class="hb-fx hb-fx-listening"><path class="hb-arc hb-arc-in" style="--i:0" d="M150 112q-9 18 0 36"/><path class="hb-arc hb-arc-in" style="--i:1" d="M136 104q-13 26 0 52"/><path class="hb-arc hb-arc-in" style="--i:2" d="M122 96q-17 34 0 68"/></g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="240" cy="98" r="4"/><circle class="hb-thought" style="--i:1" cx="253" cy="84" r="5.5"/><circle class="hb-thought" style="--i:2" cx="270" cy="70" r="7"/></g>
        <g class="hb-fx hb-fx-speaking"><path class="hb-arc hb-arc-out" style="--i:0" d="M250 112q9 18 0 36"/><path class="hb-arc hb-arc-out" style="--i:1" d="M264 104q13 26 0 52"/><path class="hb-arc hb-arc-out" style="--i:2" d="M278 96q17 34 0 68"/></g>`,
    },

    story: {
      label: "Story maker",
      tagline: "A paper theatre under the stars",
      copy: {
        listening: "Tell Reachy what happens next.",
        thinking: "Turning the idea over.",
        speaking: "Story time.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#120f29"/><stop offset=".6" stop-color="#262050"/><stop offset="1" stop-color="#3a2d66"/></linearGradient>
          <linearGradient id="${p}-curtain" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#5e2450"/><stop offset=".55" stop-color="#93427a"/><stop offset="1" stop-color="#6d2a5c"/></linearGradient>
          <radialGradient id="${p}-spot" cx=".5" cy=".2" r=".8"><stop offset="0" stop-color="#fff2c4" stop-opacity=".28"/><stop offset="1" stop-color="#fff2c4" stop-opacity="0"/></radialGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-sky)"/>
        <g class="hb-twinkles">${dots([[84, 34], [128, 58, .7], [170, 30, .9], [214, 52, .7], [236, 24, 1.1], [376, 92, .8], [72, 108, .7], [372, 140, .6]], "hb-star")}</g>
        <polyline class="hb-constellation" points="258,46 290,30 318,50 350,36 338,74 318,50"/>
        <g class="hb-constellation-stars">${dots([[258, 46, 1.5], [290, 30, 1.8], [318, 50, 1.6], [350, 36, 1.4], [338, 74, 1.5]], "hb-cstar", 2)}</g>
        <g class="hb-moon"><path d="M112 0v52" class="hb-string"/><path d="M120 54a15 15 0 1 0 0 26a12 12 0 1 1 0-26z" fill="#f4e3a4"/></g>
        <g class="hb-paper hb-paper-far"><path d="M0 178q60-44 120-14t120-10 160-12v118H0z" fill="#000" opacity=".22" transform="translate(0 4)"/><path d="M0 178q60-44 120-14t120-10 160-12v118H0z" fill="#383070"/>
          <path d="M52 158l10-26 10 26z M300 150l9-22 9 22z" fill="#2b2560"/></g>
        <g class="hb-cloud"><path d="M182 104h-36a12 12 0 0 1 3-24 16 16 0 0 1 30-4 12 12 0 0 1 3 28z" fill="#e7e1ff" opacity=".88"/><path d="M164 72v-72" class="hb-string"/></g>
        <g class="hb-paper hb-paper-mid"><path d="M0 196q80-34 150-8t150-12 100 8v76H0z" fill="#000" opacity=".22" transform="translate(0 4)"/><path d="M0 196q80-34 150-8t150-12 100 8v76H0z" fill="#4a3d8a"/></g>
        <rect x="70" y="40" width="260" height="200" fill="url(#${p}-spot)"/>
        <path d="M0 226h400v34H0z" fill="#2a1d31"/><path d="M0 226h400" stroke="#e7b75f" stroke-opacity=".5" stroke-width="2"/>
        <path d="M60 240h60M150 248h80M262 238h70" stroke="#000" stroke-opacity=".25" stroke-width="2"/>
        <g class="hb-paper hb-paper-near"><path d="M0 230q50-20 96-8 30 8 14 8z M400 230q-50-20-96-8-30 8-14 8z" fill="#5b4ba1"/></g>
        ${bot(p, 200, 170, 0.58)}
        <g class="hb-fx hb-fx-listening">${[[262, 92, 0], [292, 116, 1], [240, 128, 2]].map(([x, y, i]) => `<g transform="translate(${x} ${y})"><path class="hb-paper-star" style="--i:${i}" d="M0-7 2-2 7-2 3 1 5 7 0 3-5 7-3 1-7-2-2-2z"/></g>`).join("")}</g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="236" cy="128" r="3.5"/><circle class="hb-thought" style="--i:1" cx="247" cy="116" r="4.5"/><circle class="hb-thought" style="--i:2" cx="261" cy="104" r="6"/></g>
        <g class="hb-fx hb-fx-speaking">${[[168, 118, 0], [232, 110, 1], [206, 96, 2], [184, 92, 3]].map(([x, y, i]) => `<g transform="translate(${x} ${y})"><path class="hb-paper-star hb-rise" style="--i:${i}" d="M0-6 1.7-1.7 6-1.7 2.6 1 4.2 6 0 2.6-4.2 6-2.6 1-6-1.7-1.7-1.7z"/></g>`).join("")}</g>
        <g class="hb-curtains"><g class="hb-curtain hb-curtain-l"><path d="M-6 0h70c-10 64-4 122-20 178-8 30-12 56-20 82H-6z" fill="url(#${p}-curtain)"/><path d="M18 8c4 80 0 170-10 250M40 8c2 70-4 150-14 250" class="hb-fold"/></g>
          <g class="hb-curtain hb-curtain-r"><path d="M406 0h-70c10 64 4 122 20 178 8 30 12 56 20 82h30z" fill="url(#${p}-curtain)"/><path d="M382 8c-4 80 0 170 10 250M360 8c-2 70 4 150 14 250" class="hb-fold"/></g>
          <path d="M0 0h400v18q-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0-20 14-40 0z" fill="#7e3466"/>
          <path d="M0 18q20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0 20 14 40 0" class="hb-trim"/></g>`,
    },

    quiz: {
      label: "Quiz quest",
      tagline: "A little island of map, compass and shapes",
      copy: {
        listening: "Reachy is listening.",
        thinking: "Working it out.",
        speaking: "Reachy has something to say.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0a2226"/><stop offset="1" stop-color="#15403f"/></linearGradient>
          <linearGradient id="${p}-top" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2a6d64"/><stop offset="1" stop-color="#1d5550"/></linearGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-sky)"/>
        <g class="hb-grid">${Array.from({ length: 9 }, (_, i) => `<path d="M${-40 + i * 60} 260 L${80 + i * 60} 0"/>`).join("")}</g>
        <g class="hb-compass" transform="translate(62 64)"><circle r="30" class="hb-compass-ring"/><circle r="22" class="hb-compass-inner"/>
          <path d="M0-28 4-4 28 0 4 4 0 28-4 4-28 0-4-4z" class="hb-compass-rose"/>
          <g class="hb-needle"><path d="M0-20 5 0H-5z" fill="#ff7b6b"/><path d="M0 20 5 0H-5z" fill="#f4ede1"/><circle r="2.5" fill="#0a2226"/></g></g>
        <g class="hb-shapes">
          <g class="hb-shape" style="--i:0" transform="translate(320 60)"><path d="M0-14 13 9H-13z" stroke="#68d8ff"/></g>
          <g class="hb-shape" style="--i:1" transform="translate(356 108)"><rect x="-10" y="-10" width="20" height="20" rx="2" stroke="#f2c25a"/></g>
          <g class="hb-shape" style="--i:2" transform="translate(286 106)"><circle r="9" stroke="#ff9f92"/></g>
          <g class="hb-shape" style="--i:3" transform="translate(124 92)"><path d="M0-11 10-5v11L0 12-10 6V-5z" stroke="#a68cff"/></g>
        </g>
        <path d="M70 198 200 244 330 198v14L200 258 70 212z" fill="#0f3533"/>
        <path d="M200 244 330 198v14L200 258z" fill="#0b2b29"/>
        <path d="M200 152 330 198 200 244 70 198z" fill="url(#${p}-top)"/>
        <g class="hb-tiles">${[1, 2, 3].map((i) => `<path d="M${200 - 32.5 * i} ${152 + 11.5 * i} L${330 - 32.5 * i} ${198 + 11.5 * i}"/><path d="M${200 + 32.5 * i} ${152 + 11.5 * i} L${70 + 32.5 * i} ${198 + 11.5 * i}"/>`).join("")}</g>
        <path class="hb-trail" d="M104 202q46-20 72-4t60 8 64-14"/>
        <g class="hb-peaks"><path d="M268 186l16-28 16 28z" fill="#e0a54a"/><path d="M284 158l16 28h-16z" fill="#b37a2e"/><path d="M290 190l11-18 11 18z" fill="#c98f3c"/></g>
        ${[[104, 202, 0], [236, 206, 1], [300, 192, 2]].map(([x, y, i]) => `<g class="hb-pennant" style="--i:${i}" transform="translate(${x} ${y})"><path d="M0 0v-22" class="hb-pole"/><path class="hb-flag" d="M0-22 14-17 0-12z"/></g>`).join("")}
        <ellipse cx="200" cy="204" rx="40" ry="11" fill="#103a37"/><ellipse cx="200" cy="200" rx="40" ry="11" fill="#2f8073"/>
        ${bot(p, 200, 150, 0.54)}
        <g class="hb-fx hb-fx-listening">${[0, 1, 2].map((i) => `<path class="hb-chevron hb-chevron-in" style="--i:${i}" d="M${118 + i * 14} 112l8 9-8 9"/>`).join("")}</g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="236" cy="104" r="3.5"/><rect class="hb-thought" style="--i:1" x="244" y="86" width="9" height="9" rx="1.5"/><path class="hb-thought" style="--i:2" d="M266 64l7 12h-14z"/></g>
        <g class="hb-fx hb-fx-speaking">${[0, 1, 2].map((i) => `<path class="hb-chevron hb-chevron-out" style="--i:${i}" d="M${248 + i * 14} 112l8 9-8 9"/>`).join("")}</g>`,
    },

    riddles: {
      label: "Riddle box",
      tagline: "A curious locked box in a lantern-lit alcove",
      copy: {
        listening: "Reachy is listening for your guess.",
        thinking: "Pondering the puzzle.",
        speaking: "Listen closely.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-wall" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0b1916"/><stop offset="1" stop-color="#14302a"/></linearGradient>
          <radialGradient id="${p}-lamp" cx=".42" cy=".62" r=".55"><stop offset="0" stop-color="#ffcf7a" stop-opacity=".34"/><stop offset="1" stop-color="#ffcf7a" stop-opacity="0"/></radialGradient>
          <radialGradient id="${p}-glow" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#ffe6a6"/><stop offset=".4" stop-color="#ffb84d" stop-opacity=".6"/><stop offset="1" stop-color="#ffb84d" stop-opacity="0"/></radialGradient>
          <linearGradient id="${p}-wood" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7c4a30"/><stop offset="1" stop-color="#55301f"/></linearGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-wall)"/>
        <path d="M62 260V112Q62 26 200 26t138 86v148" fill="#183a32" stroke="#25503f" stroke-width="7"/>
        <path d="M84 260V118Q84 50 200 50t116 68v142" fill="#0f2622"/>
        <rect width="400" height="260" fill="url(#${p}-lamp)"/>
        <g class="hb-motes">${dots([[120, 96], [150, 70, .8], [236, 88, .9], [210, 120, .7], [262, 64, .8]], "hb-mote", 1.6)}</g>
        <path d="M0 214h400v46H0z" fill="#0c1d1a"/><path d="M84 214h232" stroke="#ffcf7a" stroke-opacity=".18" stroke-width="2"/>
        <ellipse cx="172" cy="216" rx="72" ry="7" fill="#000" opacity=".4"/>
        <g class="hb-chest">
          <rect x="112" y="166" width="120" height="48" rx="5" fill="url(#${p}-wood)"/>
          <path d="M112 182h120" stroke="#000" stroke-opacity=".2" stroke-width="2"/>
          <rect x="126" y="166" width="8" height="48" fill="#d9a650"/><rect x="210" y="166" width="8" height="48" fill="#d9a650"/>
          ${[[148, 0], [172, 1], [196, 2]].map(([x, i]) => `<g transform="translate(${x} 197)"><circle r="8.5" fill="#2a1a12"/><circle class="hb-dial" style="--i:${i}" r="6.5"/></g>`).join("")}
          <g class="hb-leak"><path d="M118 166 60 112h224l-56 54z" fill="url(#${p}-glow)" opacity=".5"/></g>
          <g class="hb-lid"><path d="M112 168v-12q0-16 16-16h88q16 0 16 16v12z" fill="#8a5336"/><path d="M126 140h8v28h-8zM210 140h8v28h-8z" fill="#e0b45e"/>
            <circle cx="172" cy="157" r="8" fill="#d9a650"/><circle class="hb-keyhole-glow" cx="172" cy="157" r="16" fill="url(#${p}-glow)"/><path d="M172 152a3 3 0 0 1 2 5l1 5h-6l1-5a3 3 0 0 1 2-5z" fill="#2a1a12"/></g>
        </g>
        <g class="hb-key"><path d="M162 92q0-13 13-13t13 12q0 8-9 11-4 2-4 7v6" class="hb-key-bow"/><path d="M175 115v18M175 127h6M175 133h5" class="hb-key-shaft"/></g>
        ${bot(p, 286, 166, 0.55)}
        <g class="hb-fx hb-fx-listening"><circle class="hb-ripple" style="--i:0" cx="172" cy="157" r="20"/><circle class="hb-ripple" style="--i:1" cx="172" cy="157" r="30"/></g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="320" cy="114" r="3.5"/><circle class="hb-thought" style="--i:1" cx="331" cy="101" r="4.5"/><circle class="hb-thought" style="--i:2" cx="345" cy="88" r="6"/></g>
        <g class="hb-fx hb-fx-speaking"><path class="hb-arc hb-arc-out" style="--i:0" d="M332 128q8 16 0 32"/><path class="hb-arc hb-arc-out" style="--i:1" d="M345 120q12 24 0 48"/></g>`,
    },

    calm: {
      label: "Calm corner",
      tagline: "A quiet moonlit pond",
      copy: {
        listening: "Reachy is listening quietly.",
        thinking: "A quiet moment.",
        speaking: "Reachy is speaking softly.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#121d26"/><stop offset="1" stop-color="#1f3640"/></linearGradient>
          <linearGradient id="${p}-water" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1c3741"/><stop offset="1" stop-color="#132830"/></linearGradient>
          <radialGradient id="${p}-halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#e9e4cf" stop-opacity=".3"/><stop offset="1" stop-color="#e9e4cf" stop-opacity="0"/></radialGradient>
          <radialGradient id="${p}-fly" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#f6e7a8" stop-opacity=".85"/><stop offset="1" stop-color="#f6e7a8" stop-opacity="0"/></radialGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-sky)"/>
        <circle cx="92" cy="64" r="70" fill="url(#${p}-halo)"/><circle cx="92" cy="64" r="21" fill="#e6e0c8" opacity=".86"/>
        <path d="M0 168q40-22 84-10t90-6 110 4 116-8v32H0z" fill="#182b33"/>
        <path d="M0 178h400v82H0z" fill="url(#${p}-water)"/>
        <g class="hb-reflection">${[0, 1, 2, 3].map((i) => `<rect x="${78 - i * 3}" y="${186 + i * 11}" width="${28 + i * 6}" height="2.4" rx="1.2"/>`).join("")}</g>
        <g class="hb-waves"><path d="M-20 200q25-5 50 0t50 0 50 0 50 0 50 0 50 0 50 0 50 0 50 0"/><path d="M-20 222q25-5 50 0t50 0 50 0 50 0 50 0 50 0 50 0 50 0 50 0" style="--i:1"/><path d="M-20 244q25-5 50 0t50 0 50 0 50 0 50 0 50 0 50 0 50 0 50 0" style="--i:2"/></g>
        <g class="hb-reeds"><path d="M28 260q-4-50 6-92M40 260q2-40-8-74M52 260q-2-36 10-62"/><path d="M372 260q4-46-6-84M384 260q-2-38 8-66"/></g>
        <g class="hb-lily"><ellipse cx="312" cy="214" rx="24" ry="6" fill="#36604f"/><path d="M312 214l18-4" stroke="#1c3741" stroke-width="3"/>
          <path d="M304 210q8-16 8-16t8 16q-8 3-16 0z" fill="#d9b4bb"/><path d="M298 211q4-9 14-9 10 0 14 9-14 4-28 0z" fill="#c79aa3" opacity=".8"/></g>
        <ellipse cx="96" cy="230" rx="16" ry="4" fill="#2f5446" opacity=".8"/>
        <ellipse cx="210" cy="200" rx="48" ry="12" fill="#26363d"/><ellipse cx="210" cy="196" rx="46" ry="11" fill="#3a4c54"/>
        ${bot(p, 210, 148, 0.5)}
        <g class="hb-flies">${[[150, 120, 0], [286, 96, 1], [334, 150, 2]].map(([x, y, i]) => `<g class="hb-fly" style="--i:${i}" transform="translate(${x} ${y})"><g><circle r="7" fill="url(#${p}-fly)"/><circle r="1.8" fill="#fbf0c2"/></g></g>`).join("")}</g>
        <g class="hb-fx hb-fx-listening"><ellipse class="hb-pond-ring" style="--i:0" cx="120" cy="212" rx="16" ry="3.5"/><ellipse class="hb-pond-ring" style="--i:1" cx="120" cy="212" rx="28" ry="6"/></g>
        <g class="hb-fx hb-fx-thinking"><g transform="translate(236 112)"><g class="hb-visitor"><circle r="7" fill="url(#${p}-fly)"/><circle r="1.8" fill="#fbf0c2"/></g></g></g>
        <g class="hb-fx hb-fx-speaking"><ellipse class="hb-pond-ring" style="--i:0" cx="210" cy="204" rx="58" ry="13"/><ellipse class="hb-pond-ring" style="--i:1" cx="210" cy="206" rx="76" ry="17"/></g>`,
    },

    ispy: {
      label: "I Spy",
      tagline: "An explorer's desk with a magnifying lens",
      copy: {
        listening: "Reachy is ready for your guess or clue.",
        thinking: "Thinking about the clue.",
        speaking: "Reachy is speaking.",
        searching: "Camera on for this desk search only.",
      },
      art: (p) => `<defs>${botDefs(p)}
          <linearGradient id="${p}-wall" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1a140f"/><stop offset="1" stop-color="#2b2118"/></linearGradient>
          <radialGradient id="${p}-lamp" cx=".84" cy=".1" r=".7"><stop offset="0" stop-color="#ffd38a" stop-opacity=".22"/><stop offset="1" stop-color="#ffd38a" stop-opacity="0"/></radialGradient>
          <linearGradient id="${p}-desk" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5a3e29"/><stop offset="1" stop-color="#3a2819"/></linearGradient>
          <linearGradient id="${p}-beam" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe2a0" stop-opacity=".62"/><stop offset="1" stop-color="#ffe2a0" stop-opacity="0"/></linearGradient>
          <radialGradient id="${p}-glass" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#d7f3ff" stop-opacity=".45"/><stop offset="1" stop-color="#7cc6dc" stop-opacity=".08"/></radialGradient>
        </defs>
        <rect width="400" height="260" fill="url(#${p}-wall)"/><rect width="400" height="260" fill="url(#${p}-lamp)"/>
        <path class="hb-search-arc" d="M68 156a134 112 0 0 1 264 0"/>
        <g class="hb-ticks">${[-120, -60, 0, 60, 120].map((deg) => { const a = (deg / 2 - 90) * Math.PI / 180; const x = 200 + 134 * Math.cos(a); const y = 156 + 112 * Math.sin(a); return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3"/>`; }).join("")}</g>
        <path d="M18 260 58 182h284l40 78z" fill="url(#${p}-desk)"/><path d="M58 182h284" stroke="#8a6344" stroke-width="2"/>
        <path d="M80 200h70M222 214h96M48 236h110M250 244h90" stroke="#000" stroke-opacity=".18" stroke-width="2"/>
        <g class="hb-things">
          <ellipse cx="96" cy="212" rx="18" ry="4" fill="#000" opacity=".3"/><rect x="80" y="186" width="32" height="26" rx="6" fill="#56707c"/><rect x="80" y="186" width="32" height="6" rx="3" fill="#6c8995"/>
          <ellipse cx="122" cy="226" rx="14" ry="3.5" fill="#000" opacity=".3"/><path d="M110 226 122 200l12 26z" fill="#a9825a"/>
          <ellipse cx="300" cy="210" rx="16" ry="4" fill="#000" opacity=".3"/><path d="M286 178v30a14 4 0 0 0 28 0v-30z" fill="#7d6490"/><ellipse cx="300" cy="178" rx="14" ry="4" fill="#9a80ad"/>
          <ellipse cx="334" cy="232" rx="12" ry="3" fill="#000" opacity=".3"/><circle cx="334" cy="222" r="10" fill="#5f8a70"/>
        </g>
        <g class="hb-beam"><path d="M200 136 150 232q50 12 100 0z" fill="url(#${p}-beam)"/></g>
        ${bot(p, 200, 166, 0.55, '<circle class="hb-cam-dot" cx="0" cy="-34" r="4.5"/>')}
        <g class="hb-lens" transform="translate(266 148)"><path d="M14 16 32 40" class="hb-lens-handle"/><circle r="21" fill="url(#${p}-glass)"/>
          <g class="hb-iris">${[0, 60, 120, 180, 240, 300].map((deg) => `<path transform="rotate(${deg})" d="M0 0 18.2-10.5A21 21 0 0 1 18.2 10.5z"/>`).join("")}</g>
          <circle r="21" class="hb-lens-rim"/><path d="M-12-8a14 14 0 0 1 8-6" class="hb-lens-shine"/></g>
        <g class="hb-fx hb-fx-listening">${[[120, 104, 0], [140, 86, 1]].map(([x, y, i]) => `<g transform="translate(${x} ${y})"><g class="hb-bubble" style="--i:${i}"><rect x="-13" y="-10" width="26" height="20" rx="10"/><circle cx="-6" r="1.8"/><circle r="1.8"/><circle cx="6" r="1.8"/></g></g>`).join("")}</g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="160" cy="114" r="3.5"/><circle class="hb-thought" style="--i:1" cx="148" cy="101" r="4.5"/><circle class="hb-thought" style="--i:2" cx="133" cy="88" r="6"/></g>
        <g class="hb-fx hb-fx-speaking"><path class="hb-arc hb-arc-out hb-arc-l" style="--i:0" d="M150 126q-8 16 0 32"/><path class="hb-arc hb-arc-out hb-arc-l" style="--i:1" d="M137 118q-12 24 0 48"/></g>`,
    },

    companion: {
      label: "Reachy",
      tagline: "",
      copy: {},
      viewBox: "0 0 120 120",
      art: (p) => `<defs>${botDefs(p)}
          <radialGradient id="${p}-disc" cx=".5" cy=".35" r=".7"><stop offset="0" stop-color="#2a2420"/><stop offset="1" stop-color="#15120f"/></radialGradient>
        </defs>
        <circle cx="60" cy="60" r="60" fill="url(#${p}-disc)"/>
        <ellipse class="hb-halo" cx="60" cy="58" rx="46" ry="12" transform="rotate(-10 60 58)"/>
        <g class="hb-orb hb-orb-bot" transform="translate(60 58) rotate(-10)" style="--ox:40px;--oy:-6px;color:#ff6b2c"><g class="hb-orb-x hb-orb-small"><g class="hb-orb-y hb-orb-small"><circle class="hb-orb-core" r="3.4"/></g></g></g>
        ${bot(p, 60, 66, 0.44)}
        <g class="hb-fx hb-fx-listening"><path class="hb-arc hb-arc-in" style="--i:0" d="M27 54q-5 11 0 22"/><path class="hb-arc hb-arc-in" style="--i:1" d="M19 49q-8 16 0 32"/></g>
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="84" cy="40" r="2.6"/><circle class="hb-thought" style="--i:1" cx="92" cy="32" r="3.4"/><circle class="hb-thought" style="--i:2" cx="101" cy="23" r="4.2"/></g>
        <g class="hb-fx hb-fx-speaking"><path class="hb-arc hb-arc-out" style="--i:0" d="M93 54q5 11 0 22"/><path class="hb-arc hb-arc-out" style="--i:1" d="M101 49q8 16 0 32"/></g>`,
    },

    /* ---------- Adult instrument scenes ----------
       Small, restrained and transparent so they sit on the existing dark displays. Each declares its
       own phase vocabulary; only `live` phases loop, everything else is a composed still. */

    presence: {
      label: "Reachy",
      viewBox: "0 0 200 200",
      fit: "meet",
      phases: ["waiting", "standby", "listening", "thinking", "speaking", "paused", "asleep", "trouble", "offline", "idle"],
      live: ["waiting", "listening", "thinking", "speaking"],
      neutral: "idle",
      reset: { phase: "offline", title: "Out of reach", detail: "Status unavailable." },
      art: (p) => `<defs>${botDefs(p)}</defs>
        <circle class="hb-pr-ring" cx="100" cy="104" r="92"/>
        <circle class="hb-pr-ring-live" cx="100" cy="104" r="92"/>
        <ellipse class="hb-pr-orbit" cx="100" cy="112" rx="78" ry="16" transform="rotate(-8 100 112)"/>
        ${bot(p, 100, 106, 0.78)}
        <g class="hb-fx hb-fx-thinking"><circle class="hb-thought" style="--i:0" cx="138" cy="52" r="3.6"/><circle class="hb-thought" style="--i:1" cx="149" cy="40" r="4.6"/><circle class="hb-thought" style="--i:2" cx="162" cy="27" r="5.8"/></g>
        <g class="hb-fx hb-fx-listening"><path class="hb-arc hb-arc-in" style="--i:0" d="M44 92q-8 16 0 32"/><path class="hb-arc hb-arc-in" style="--i:1" d="M32 84q-12 24 0 48"/></g>
        <g class="hb-fx hb-fx-speaking"><path class="hb-arc hb-arc-out" style="--i:0" d="M156 92q8 16 0 32"/><path class="hb-arc hb-arc-out" style="--i:1" d="M168 84q12 24 0 48"/></g>
        <g class="hb-pr-moon"><path d="M150 40a12 12 0 1 0 10 18a9 9 0 1 1-10-18z"/><path class="hb-pr-z" d="M164 30h7l-7 8h7"/></g>
        <g class="hb-pr-quiet"><rect x="146" y="36" width="24" height="24" rx="12"/><path d="M152 48h12"/></g>
        <g class="hb-pr-alert"><circle cx="158" cy="48" r="11"/><path d="M158 42v7M158 53v.5"/></g>`,
    },

    workbench: {
      label: "Project session",
      viewBox: "20 14 200 100",
      fit: "meet",
      phases: ["idle", "discussing", "approval", "working", "stopping", "uncertain", "finished", "ready", "stopped", "trouble", "offline"],
      live: ["approval", "working"],
      neutral: "idle",
      reset: { phase: "offline", chip: "Hidden", title: "Project work hidden", detail: "Reconnect as owner to see it." },
      // Three stations: you, Reachy and the native worker. Links show who is talking to whom; nothing
      // here measures progress, and the worker never draws a result it has not reported.
      art: () => `<rect class="hb-wb-bg" width="240" height="120" rx="14"/>
        <g class="hb-wb-grid">${[30, 60, 90].map((y) => `<path d="M12 ${y}h216"/>`).join("")}</g>
        <path class="hb-wb-link hb-wb-link-a" d="M44 64Q82 40 120 60"/>
        <path class="hb-wb-link hb-wb-link-b" d="M120 60Q158 40 196 64"/>
        <path class="hb-wb-flow" d="M120 60Q158 40 196 64"/>
        <g class="hb-wb-gate" transform="translate(158 47)"><path d="M0-9 9 0 0 9-9 0z"/><circle r="2.4"/></g>
        <g class="hb-wb-node hb-wb-you" transform="translate(44 64)"><circle r="13"/><circle class="hb-wb-core" r="4.5"/></g>
        <g class="hb-wb-node hb-wb-reachy" transform="translate(120 60)"><rect x="-15" y="-12" width="30" height="24" rx="10"/><circle class="hb-wb-eye" cx="-5" cy="0" r="2.6"/><circle class="hb-wb-eye hb-wb-eye-r" cx="5" cy="0" r="2.6"/></g>
        <g class="hb-wb-node hb-wb-worker" transform="translate(196 64)"><rect x="-14" y="-14" width="28" height="28" rx="7"/>
          <g class="hb-wb-orbit"><circle r="20"/></g>
          <path class="hb-wb-check" d="M-6 0l4 4 8-9"/>
          <path class="hb-wb-bang" d="M0-6v6M0 5v.5"/>
          <path class="hb-wb-ask" d="M-3.5-4a3.6 3.6 0 1 1 4.6 3.4c-1 .4-1.1 1-1.1 2M0 5.5v.5"/>
          <rect class="hb-wb-stop" x="-4.5" y="-4.5" width="9" height="9" rx="1.5"/></g>`,
    },

    lens: {
      label: "Camera",
      viewBox: "0 0 48 48",
      fit: "meet",
      phases: ["off", "ready", "connecting", "live", "held", "paused", "privacy", "trouble", "offline"],
      live: ["connecting", "live"],
      neutral: "off",
      reset: { phase: "offline", title: "Camera off", detail: "Owner session unavailable." },
      // The aperture only opens for decoded video the camera module has already confirmed. It never
      // shows imagery.
      art: (p) => `<defs><radialGradient id="${p}-glass" cx=".38" cy=".32" r=".8"><stop offset="0" stop-color="#6b7f8a"/><stop offset="1" stop-color="#101418"/></radialGradient></defs>
        <circle class="hb-ln-body" cx="24" cy="24" r="22"/>
        <circle class="hb-ln-seek" cx="24" cy="24" r="20"/>
        <circle cx="24" cy="24" r="15" fill="url(#${p}-glass)"/>
        <g class="hb-ln-iris" transform="translate(24 24)">${[0, 60, 120, 180, 240, 300].map((deg) => `<path transform="rotate(${deg})" d="M0 0 13-7.5A15 15 0 0 1 13 7.5z"/>`).join("")}</g>
        <circle class="hb-ln-rim" cx="24" cy="24" r="15"/>
        <path class="hb-ln-shine" d="M15 19a10 10 0 0 1 6-5"/>
        <path class="hb-ln-slash" d="M10 38 38 10"/>
        <circle class="hb-ln-dot" cx="40" cy="8" r="4"/>`,
    },

    updater: {
      label: "Software update",
      viewBox: "0 0 300 44",
      fit: "meet",
      phases: ["idle", "available", "checking", "queued", "downloading", "installing", "restarting", "verifying", "rolling_back", "succeeded", "rolled_back", "failed", "unreachable", "offline"],
      live: ["checking", "queued", "downloading", "installing", "restarting", "verifying", "rolling_back"],
      neutral: "idle",
      reset: { phase: "offline", chip: "Hidden", title: "Updates hidden", detail: "Pair an owner device to read update progress." },
      // Five durable updater steps, left to right. A node lights only for a step the updater journal
      // has reached; there is no percentage and no timer.
      art: () => `<path class="hb-up-track" d="M22 22H278"/>
        <path class="hb-up-fill" d="M22 22H278"/>
        ${["queued", "downloading", "installing", "restarting", "verifying"].map((step, i) => `<g class="hb-up-step hb-up-${step}" style="--i:${i}" transform="translate(${(22 + i * 51.2).toFixed(1)} 22)"><circle class="hb-up-ring" r="9"/><circle class="hb-up-core" r="4"/></g>`).join("")}
        <g class="hb-up-end" transform="translate(278 22)"><circle class="hb-up-ring" r="11"/>
          <path class="hb-up-check" d="M-5 0l3.5 3.5L5.5-4"/><path class="hb-up-back" d="M4-3A5.5 5.5 0 1 0 5 2M5-6v4H1"/><path class="hb-up-bang" d="M0-5v5M0 4v.5"/></g>`,
    },
  };

  /* ---------- State adapters (pure; no DOM) ---------- */

  const STATE_PHASE = {
    listening: "listening",
    transcribing: "thinking",
    thinking: "thinking",
    synthesizing: "thinking",
    announcement_synthesizing: "thinking",
    speaking: "speaking",
    announcing: "speaking",
    waiting_for_wake_word: "waiting",
    meeting: "paused",
    sleep: "paused",
    sleeping: "paused",
    error: "trouble",
    configuration_error: "trouble",
    power_transition_error: "trouble",
  };

  const GENERIC = {
    waiting: ["Ready", "Say “Hey Reachy” to talk."],
    unknown: ["Waiting", "Reachy will be with you in a moment."],
    paused: ["Paused", "Microphone and camera are off for now."],
    trouble: ["Something went wrong", "A grown-up can check the app."],
    offline: ["Can't reach Reachy", "Waiting for the connection to return."],
    ending: ["Ending", "Reachy is folding safely."],
    ended: ["Session ended", "Thanks for playing together."],
  };

  function sceneKey(value, fallback) {
    return ACTIVITIES.includes(value) ? value : fallback;
  }

  /** Kids stage view from allowlisted status only: activity, active, camera_active, state, power mode. */
  function kidsView({ kids = {}, state = "", powerMode = "", selected = "buddy", offline = false, ending = false } = {}) {
    const chosen = sceneKey(selected, "buddy");
    const active = kids.active === true && !ending;
    const scene = active ? sceneKey(kids.profile?.activity, chosen) : chosen;
    const info = SCENES[scene];
    const view = (phase, chip, title, detail) => ({ scene, phase, chip, title, detail });
    if (offline) return view("offline", "Offline", ...GENERIC.offline);
    if (ending) return view("ending", "Ending", ...GENERIC.ending);
    if (!active) {
      return kids.last_end_reason
        ? view("ended", "Ended", ...GENERIC.ended)
        : view("preview", "Preview", info.label, info.tagline);
    }
    if (["meeting", "sleep"].includes(powerMode)) return view("paused", "Paused", ...GENERIC.paused);
    if (scene === "ispy" && kids.camera_active === true) return view("searching", "Camera on", "Searching the desk", info.copy.searching);
    const phase = STATE_PHASE[state];
    if (phase === "paused") return view("paused", "Paused", ...GENERIC.paused);
    if (phase === "trouble") return view("trouble", "Live", ...GENERIC.trouble);
    if (phase === "waiting") return view("waiting", "Live", ...GENERIC.waiting);
    if (phase) return view(phase, "Live", phase.charAt(0).toUpperCase() + phase.slice(1), info.copy[phase]);
    // Starting, stopping or an unrecognised state: say nothing we cannot back up.
    return view("waiting", "Live", ...GENERIC.unknown);
  }

  /** Compact adult companion: runtime state only. */
  function companionView({ state = "", powerMode = "", offline = false, kidsActive = false } = {}) {
    const view = (phase, title, detail) => ({ scene: "companion", phase, chip: "", title, detail });
    if (offline) return view("offline", "Out of reach", "Status unavailable.");
    if (kidsActive) return view("paused", "Kids Mode", "Adult voice is locked.");
    if (["meeting", "sleep"].includes(powerMode)) return view("paused", powerMode === "meeting" ? "In a meeting" : "Asleep", "Voice capture is off.");
    const phase = state === "looking" ? "thinking" : STATE_PHASE[state];
    const copy = {
      listening: ["Listening", "Go ahead, Reachy is listening."],
      thinking: [state === "looking" ? "Taking a look" : "Thinking", "Working on a reply."],
      speaking: ["Speaking", "Reachy is replying."],
      waiting: ["Waiting", powerMode === "standby" ? "Standby · wake word only." : "Say a wake word to ask."],
      paused: ["Paused", "Voice capture is off."],
      trouble: ["Needs attention", "See the Home status."],
    }[phase];
    return copy ? view(phase, ...copy) : view("waiting", "Waiting", "Reachy will be ready shortly.");
  }

  /** Home hero: runtime state and power mode only. Unknown or starting states stay neutral. */
  function presenceView({ state = "", powerMode = "", offline = false, kidsActive = false } = {}) {
    const view = (phase, title, detail) => ({ scene: "presence", phase, chip: "", title, detail });
    if (offline) return view("offline", "Out of reach", "Status unavailable.");
    if (kidsActive) return view("paused", "Kids Mode", "Adult voice is locked.");
    if (powerMode === "sleep" || ["sleep", "sleeping"].includes(state)) return view("asleep", "Asleep", "Voice capture is off.");
    if (powerMode === "meeting" || state === "meeting") return view("paused", "In a meeting", "Voice capture is off.");
    const phase = state === "looking" ? "thinking" : STATE_PHASE[state];
    if (phase === "listening") return view("listening", "Listening", "Microphone open for this turn.");
    if (phase === "thinking") return view("thinking", state === "looking" ? "Taking a look" : "Thinking", "Working on a reply.");
    if (phase === "speaking") return view("speaking", "Speaking", "Reachy is replying.");
    if (phase === "trouble") return view("trouble", "Needs attention", "See the status details.");
    if (phase === "waiting") {
      return powerMode === "standby"
        ? view("standby", "Standby", "Wake word only.")
        : view("waiting", "Awake", "Ready for a wake word.");
    }
    return view("idle", "Waiting", "Reachy will be ready shortly.");
  }

  const RUN_WORKING = { submitting: "Submitting", running: "Running", verifying: "Verifying" };
  const RUN_TROUBLE = new Set(["failed", "error", "rejected", "timed_out", "timeout"]);
  const RUN_STOPPED = new Set(["cancelled", "canceled", "stopped", "aborted"]);

  /**
   * Workspace bench from booleans and the native run status enum only (never text, ids or receipts).
   * `verified` must be the caller's exact receipt check; "completed" alone is never "Ready to test".
   */
  function workbenchView({ offline = false, showing = false, configured = false, bound = false, pending = false, status = "", verified = false, uncertain = false } = {}) {
    const view = (phase, chip, title, detail) => ({ scene: "workbench", phase, chip, title, detail });
    const run = String(status || "");
    if (offline) return view("offline", "Hidden", "Project work hidden", "Reconnect as owner to see it.");
    if (uncertain || ["disconnected", "submission_unknown"].includes(run)) {
      return view("uncertain", "Unconfirmed", "Outcome unconfirmed", "Work may still be running. Reconnect to reconcile.");
    }
    if (!showing) return view("idle", "Off", "Display off", "Show the conversation to open a project.");
    if (pending) return view("approval", "Your approval", "Waiting for your approval", "Nothing runs until you approve the exact request.");
    if (RUN_WORKING[run]) return view("working", "Busy", RUN_WORKING[run], "The worker is busy. No progress estimate is shown.");
    if (run === "stopping") return view("stopping", "Stopping", "Stopping", "Stop requested; waiting for the worker.");
    if (run === "completed") {
      return verified
        ? view("ready", "Verified", "Ready to test", "Fixed checks passed for this run.")
        : view("finished", "Finished", "Finished", "Not verified ready to test.");
    }
    if (RUN_TROUBLE.has(run)) return view("trouble", "Not done", "Did not complete", "Check the session before trying again.");
    if (RUN_STOPPED.has(run)) return view("stopped", "Stopped", "Stopped", "No work is running.");
    if (run) return view("idle", "Unknown", "Status unconfirmed", "Check the project session.");
    if (bound) return view("discussing", "Selected", "Discussing", "Session selected. No work is running.");
    return configured
      ? view("idle", "Ready", "No session selected", "Select an owner-registered session.")
      : view("idle", "Off", "Not configured", "No native sessions are set up on the host.");
  }

  const LENS = {
    off: ["Camera off", "No video is being received."],
    ready: ["Camera ready", "Nothing starts until you press Start."],
    connecting: ["Connecting", "Waiting for decoded video."],
    live: ["Live", "Decoded video is arriving."],
    held: ["Video delayed", "Movement held until fresh video arrives."],
    paused: ["Playback paused", "Connected, but this browser paused playback."],
    privacy: ["Camera blocked", "Privacy or power mode keeps the camera off."],
    trouble: ["Camera problem", "See the camera message."],
    offline: ["Camera off", "Owner session unavailable."],
  };

  /** Camera lens from the camera module's own categorical phase. Unknown phases read as off. */
  function lensView({ phase = "", offline = false } = {}) {
    const key = offline ? "offline" : (LENS[phase] ? phase : "off");
    return { scene: "lens", phase: key, chip: "", title: LENS[key][0], detail: LENS[key][1] };
  }

  const UPDATE_STEPS = { queued: 1, downloading: 2, installing: 3, stopping: 4, starting: 4, verifying: 5 };
  const UPDATE_TITLES = { queued: "Queued", downloading: "Downloading", installing: "Installing", stopping: "Restarting", starting: "Restarting", verifying: "Verifying" };

  /**
   * Update rail from the durable updater job state. Success is shown only for a journal state of
   * `succeeded` that has not been acknowledged yet; an unreachable updater never implies progress.
   */
  function updaterView({ offline = false, unreachable = false, job = "", acknowledged = false, checking = false, available = false, supported = true, checked = false } = {}) {
    const view = (phase, chip, title, detail) => ({ scene: "updater", phase, chip, title, detail });
    const state = String(job || "");
    if (offline) return view("offline", "Hidden", "Updates hidden", "Pair an owner device to read update progress.");
    if (unreachable) return view("unreachable", "Reconnecting", "Reading saved progress", "Success is not confirmed until the updater answers.");
    if (UPDATE_STEPS[state]) {
      const phase = ["stopping", "starting"].includes(state) ? "restarting" : state;
      return view(phase, `Step ${UPDATE_STEPS[state]} of 5`, UPDATE_TITLES[state], "Saved by the Reachy updater. Reconnecting is safe.");
    }
    if (state === "rolling_back") return view("rolling_back", "Recovering", "Restoring the previous version", "The update did not complete. Waiting for recovery.");
    if (state === "succeeded" && !acknowledged) return view("succeeded", "Verified", "Installed and verified", "Restart and health check passed.");
    if (state === "rolled_back") return view("rolled_back", "Restored", "Previous version restored", "The update did not complete.");
    if (["failed", "rollback_failed"].includes(state)) return view("failed", "Failed", "Update failed", state === "rollback_failed" ? "Operator help may be needed." : "The unchanged version was recovered.");
    if (checking) return view("checking", "Checking", "Checking for updates", "Asking the Reachy updater.");
    if (!supported) return view("idle", "Unavailable", "Updates unavailable", "The updater is not ready.");
    if (available) return view("available", "Available", "Update available", "Review it before installing.");
    return checked
      ? view("idle", "Current", "Up to date", "No installation is needed.")
      : view("idle", "Not checked", "Not checked yet", "Check Reachy for updates.");
  }

  /* ---------- Rendering ---------- */

  const live = new Set();
  const reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  let instanceCount = 0;
  let pageShown = true;

  function syncAll() { live.forEach((instance) => instance.sync()); }
  function onPageHide() { pageShown = false; syncAll(); }
  function onPageShow() { pageShown = true; syncAll(); }
  function attachGlobal() {
    document.addEventListener("visibilitychange", syncAll);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    reduceQuery?.addEventListener?.("change", syncAll);
  }
  function detachGlobal() {
    document.removeEventListener("visibilitychange", syncAll);
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("pageshow", onPageShow);
    reduceQuery?.removeEventListener?.("change", syncAll);
  }

  function element(tag, className, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function buildArt(sceneName, prefix) {
    const scene = SCENES[sceneName];
    const template = document.createElement("template");
    template.innerHTML = `<svg class="hb-art hb-art-${sceneName}" viewBox="${scene.viewBox || "0 0 400 260"}" preserveAspectRatio="xMidYMid ${scene.fit || "slice"}" focusable="false" aria-hidden="true">${scene.art(prefix)}</svg>`;
    return template.content.firstElementChild;
  }

  // Kids scenes share the global vocabulary; adult scenes declare their own phases and loops.
  function phasesOf(sceneName) { return SCENES[sceneName]?.phases || PHASES; }
  function loops(sceneName, phase) {
    const own = SCENES[sceneName]?.live;
    return own ? own.includes(phase) : LIVE_PHASES.has(phase);
  }

  /**
   * Mount a scene into `host`. Options: { variant: "stage" | "compact" | "figure" | "strip" | "rail" | "glyph", label }.
   * Returns { update(view) -> boolean, reset(), destroy(), snapshot() }.
   */
  function mount(host, { variant = "stage", label = "Reachy scene" } = {}) {
    if (!host) return null;
    const id = ++instanceCount;
    let generation = 0;
    let destroyed = false;
    let onScreen = typeof IntersectionObserver !== "function";
    let current = { scene: "", phase: "", chip: "", title: "", detail: "" };
    let key = "";
    let swapTimer = 0;
    let cueTimer = 0;
    let renders = 0;
    let art = null;

    const root = element("figure", "hb-scene");
    root.dataset.variant = variant;
    root.dataset.motion = "off";
    const canvas = element("div", "hb-scene-canvas", root);
    canvas.setAttribute("role", "img");
    const cue = element("span", "hb-scene-cue", canvas);
    cue.setAttribute("aria-hidden", "true");
    const caption = element("figcaption", "hb-scene-caption", root);
    const chip = element("span", "hb-scene-chip", caption);
    const title = element("strong", "hb-scene-title", caption);
    const detail = element("span", "hb-scene-detail", caption);
    host.replaceChildren(root);

    const observer = typeof IntersectionObserver === "function"
      ? new IntersectionObserver((entries) => {
        onScreen = entries.some((entry) => entry.isIntersecting);
        instance.sync();
      })
      : null;
    observer?.observe(root);

    function reduced() { return Boolean(reduceQuery?.matches); }

    function swapArt(sceneName) {
      generation += 1;
      const next = buildArt(sceneName, `hbs${id}g${generation}`);
      const previous = art;
      window.clearTimeout(swapTimer);
      canvas.querySelectorAll(".hb-art-leaving").forEach((node) => node.remove());
      art = next;
      if (previous && !reduced()) {
        previous.classList.add("hb-art-leaving");
        next.classList.add("hb-art-entering");
        const swapGeneration = generation;
        swapTimer = window.setTimeout(() => {
          if (destroyed || swapGeneration !== generation) return;
          previous.remove();
          next.classList.remove("hb-art-entering");
        }, SWAP_MS);
      } else {
        previous?.remove();
      }
      canvas.insertBefore(next, cue);
    }

    const instance = {
      /** Apply a view; identical views are ignored so polling never retriggers motion. */
      update(view) {
        if (destroyed || !view) return false;
        const scene = SCENES[view.scene] ? view.scene : (current.scene || "buddy");
        const phase = phasesOf(scene).includes(view.phase) ? view.phase : (SCENES[scene].neutral || "waiting");
        const next = {
          scene, phase,
          chip: String(view.chip || ""),
          title: String(view.title || ""),
          detail: String(view.detail || ""),
        };
        const nextKey = [next.scene, next.phase, next.chip, next.title, next.detail].join("|");
        if (nextKey === key) return false;
        const sceneChanged = next.scene !== current.scene;
        const phaseChanged = next.phase !== current.phase;
        key = nextKey;
        current = next;
        renders += 1;
        if (sceneChanged) swapArt(scene);
        root.dataset.scene = scene;
        root.dataset.phase = phase;
        chip.textContent = next.chip;
        chip.hidden = !next.chip;
        title.textContent = next.title;
        detail.textContent = next.detail;
        canvas.setAttribute("aria-label", `${label}: ${SCENES[scene].label}. ${next.title}.`);
        if (phaseChanged && !sceneChanged && !reduced()) {
          // One soft light cue per real phase change; never on repeated polls.
          window.clearTimeout(cueTimer);
          root.classList.remove("hb-cue");
          void cue.offsetWidth;
          root.classList.add("hb-cue");
          cueTimer = window.setTimeout(() => root.classList.remove("hb-cue"), CUE_MS);
        }
        instance.sync();
        return true;
      },
      /** Forget the current phase (owner loss, privacy): show a neutral, motionless offline still. */
      reset(view) {
        const scene = current.scene || (variant === "compact" ? "companion" : "buddy");
        if (!view && SCENES[scene].reset) return instance.update({ scene, chip: "", ...SCENES[scene].reset });
        return instance.update(view || { scene, phase: "offline", chip: variant === "compact" ? "" : "Offline", ...(variant === "compact"
          ? { title: "Out of reach", detail: "Status unavailable." }
          : { title: GENERIC.offline[0], detail: GENERIC.offline[1] }) });
      },
      sync() {
        if (destroyed) return;
        const isReduced = reduced();
        root.dataset.reduced = String(isReduced);
        // The observer can miss a reveal that happens during startup (the page shell unhiding), so a
        // real change also checks geometry once. Later scrolls and tab switches stay with the observer.
        if (!onScreen && observer && loops(current.scene, current.phase)) {
          const box = root.getBoundingClientRect();
          onScreen = box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0
            && box.top < window.innerHeight && box.left < window.innerWidth;
        }
        const running = onScreen && pageShown && !document.hidden && !isReduced && loops(current.scene, current.phase);
        root.dataset.motion = running ? "on" : "off";
      },
      snapshot() {
        return { ...current, renders, generation, motion: root.dataset.motion, reduced: root.dataset.reduced === "true", destroyed };
      },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        generation += 1;
        window.clearTimeout(swapTimer);
        window.clearTimeout(cueTimer);
        observer?.disconnect();
        root.remove();
        live.delete(instance);
        if (!live.size) detachGlobal();
      },
    };
    if (!live.size) attachGlobal();
    live.add(instance);
    instance.sync();
    return instance;
  }

  window.HomebodyScene = Object.freeze({
    ACTIVITIES,
    PHASES,
    mount,
    kidsView,
    companionView,
    presenceView,
    workbenchView,
    lensView,
    updaterView,
    /** Owner loss: every mounted scene drops its phase and stops moving. */
    resetAll() { live.forEach((instance) => instance.reset()); },
    liveCount() { return live.size; },
    sceneLabel(name) { return SCENES[name]?.label || ""; },
  });
})();
