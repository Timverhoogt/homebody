# Scenes: the Kids activity stage and the Everyday companion

Homebody draws live state as small illustrated scenes. There is one component,
`homebody/static/scene.js` (styles in `scene.css`). It has two Kids/companion mounts below, plus four adult mounts described under "Adult surfaces":

- **Kids activity stage** — the large picture in the Kids Mode display (`#kids-stage`). Each of the six activities has its own scene.
- **Everyday companion** — a compact Reachy disc in the Agent tab's Everyday card (`#everyday-companion`). It shows whether Reachy is waiting, listening, thinking or speaking.

Scenes are decoration plus a plain-text caption. They never fetch, store, wake, listen, record, move,
speak or start a game round. They also never take focus or pointer input.

## API

```js
const stage = HomebodyScene.mount(hostElement, { variant: "stage" | "compact", label: "Accessible name" });
stage.update(view);  // returns true if anything changed, false for an identical view
stage.reset();       // neutral, motionless "offline" still (used on owner loss)
stage.snapshot();    // { scene, phase, chip, title, detail, renders, generation, motion, reduced, destroyed }
stage.destroy();     // disconnects observers, clears timers, removes DOM; later updates are ignored

HomebodyScene.kidsView({ kids, state, powerMode, selected, offline, ending });  // pure adapter
HomebodyScene.companionView({ state, powerMode, offline, kidsActive });          // pure adapter
HomebodyScene.resetAll();   // every mounted scene → neutral offline still
HomebodyScene.liveCount();  // mounted instances (for tests)
```

A **view** is `{ scene, phase, chip, title, detail }`:

- `scene` is one of `buddy`, `story`, `quiz`, `riddles`, `calm`, `ispy` or `companion`.
- `phase` is one of `preview`, `waiting`, `listening`, `thinking`, `speaking`, `searching`, `paused`, `trouble`, `offline`, `ending` or `ended`.

Unknown scenes or phases fall back to the current scene and a neutral `waiting` phase.

The adapters are pure functions, kept separate from rendering. `main.js` passes them the
already-redacted status. The renderer only sees the resulting categorical view, and it builds all
text with `textContent`.

## What each phase is bound to

No new runtime telemetry was added. The Kids stage reads only these fields, all of which already
survive the Kids-locked status redaction:

- `kids_mode.active`
- `kids_mode.profile.activity`
- `kids_mode.camera_active`
- `kids_mode.last_end_reason` (presence only)
- `runtime.state`
- `runtime.power_mode`

| Status | Stage phase |
|---|---|
| Kids not active, no end reason | `preview` of the selected activity (static) |
| Kids not active, with end reason | `ended` (static) |
| End pressed, or a status poll started before End returned still says active | `ending` (static) |
| `/api/status` unreachable | `offline` (static) |
| Owner session lost | `offline` via `resetAll()` (static) |
| `power_mode` meeting/sleep, or state meeting/sleep | `paused` (static) |
| I Spy and `camera_active` | `searching` — the only phase that says the camera is on |
| `listening` | `listening` |
| `transcribing`, `thinking`, `synthesizing`, `announcement_synthesizing` | `thinking` |
| `speaking`, `announcing` | `speaking` |
| `waiting_for_wake_word` | `waiting` |
| `error`, `configuration_error`, `power_transition_error` | `trouble` (no error text shown) |
| anything else (`starting`, `stopping`, future states) | neutral `waiting` ("Waiting") |

Honest limits of the existing telemetry:

- **Typed I Spy guesses.** The runtime briefly reports `listening` before a typed guess is processed, even though the microphone is not used. The I Spy copy therefore says "ready for your guess or clue" rather than "hearing you".
- **I Spy target selection.** After the camera search the runtime reports `thinking` immediately before the target-selection request. The stage follows that categorical state, not a fabricated progress estimate.
- **Generic waveforms.** Speaking and listening arcs are generic decoration, not measured audio.
- **No outcomes.** Stages never show scores, progress, right/wrong, answers, targets, transcripts, replies, nicknames or guesses.

## Motion and resource rules

- **CSS only.** No `requestAnimationFrame`, `setInterval` or canvas. Loops are declared only under live phases (`waiting`, `listening`, `thinking`, `speaking`, `searching`). Preview, ended, ending, offline and paused are composed stills, so an idle Kids tab costs nothing.
- **When loops pause.** A loop runs only while all of these hold, tracked in `data-motion` and recalculated from listeners:
  - the stage is on screen (`IntersectionObserver`; a hidden tab panel counts as off screen),
  - the page is visible (`visibilitychange`, `pagehide`/`pageshow`),
  - reduced motion is off (`prefers-reduced-motion`).
- **Listener lifecycle.** Global listeners attach with the first mount and detach with the last `destroy()`.
- **Reduced motion.** Every phase is designed as a still first, and loops only add movement. With reduced motion the phase layers stay visible and correct, with no animation or transition.
- **Repeated polls are ignored.** Status polls every 1.5 s, but an identical view is a no-op: no DOM writes, no art swap, no phase cue. A real phase change plays one soft, 0.9 s light cue. A scene change cross-fades the art over about 0.5 s; the timer is tied to a generation counter and cleared on destroy.
- **Gentle by design.** Loops last at least 1.6 s, use low amplitude and never flash. Calm corner uses the slowest, dimmest motion (10–18 s).

## Stale-response guards

- **End fence.** `main.js` numbers each status request as it starts. Pressing End shows `ending` immediately. After End returns, any status request that started earlier cannot bring the stage back to an active phase. The first inactive status lifts the fence.
- **Offline and owner loss.** Both drop to a neutral still and never keep the last live phase.
- **Typed input untouched.** Typed I Spy input keeps its own guards in `ispy-input.js`. The stage does not read or change them.

## Art direction

All scenes share one Reachy figure with the dashboard mark's proportions: head, visor, two eyes and antennas. Poses carry the phase:

- **Listening:** head tilts toward the child and antennas perk up.
- **Thinking:** gaze drifts up and to the side.
- **Speaking:** the visor glows and the antennas sway.
- **Quiet phases:** the eyes close and the antennas droop.

Each activity then gets its own world:

- **Buddy** — golden-hour hills under a dusk sky. Two sparks orbit Reachy: a mint one for the child and a sun-coloured one for Reachy. The child's spark leads while Reachy listens, Reachy's while it speaks.
- **Story maker** — a paper theatre. Layered cut-paper hills, a moon and a cloud on strings, and a constellation the curtains open onto. Thinking draws the constellation; speaking slides the paper layers. Story contents are never illustrated.
- **Quiz quest** — an isometric island with a dotted trail, identical pennants, a compass and floating outline shapes. The needle turns toward the child while listening and sweeps while thinking. Pennants are all identical, so nothing reads as progress or a score.
- **Riddle box** — a lantern-lit alcove with a brass-bound box, three turning dials and a key shaped like a question mark. Speaking lifts the lid a crack to let light out; it never opens to an answer.
- **Calm corner** — a moonlit pond with slow water lines, reeds, a lily and a few dim fireflies. Deliberately low contrast and slow. There is no breathing instruction.
- **I Spy** — an explorer's view with abstract shapes and a magnifying lens. The lens iris stays closed unless status reports the bounded camera search. During a search, a soft beam sweeps the five-point viewing arc and the chip reads "Camera on". The lens never shows imagery, and the scene never hints at a target.

## Adult surfaces

The same engine draws four restrained adult scenes. Each declares its own phase vocabulary (`phases`), the phases that loop (`live`), a `neutral` fallback for unknown phases, and a `reset` view used on owner loss. Variants: `figure` (no caption), `strip`, `rail` and `glyph` (no caption). All hosts are `aria-hidden`, because the existing status text stays the authoritative live region.

| Surface | Mount | Adapter | Reads only |
|---|---|---|---|
| Home hero | `#hero-presence` (`figure`, main.js) | `presenceView({state, powerMode, offline, kidsActive})` | `runtime.state`, `runtime.power_mode`, Kids active/locked, owner |
| Workspace | `#workspace-native-scene` (`strip`, native-workspace.js) | `workbenchView({offline, showing, configured, bound, pending, status, verified, uncertain})` | booleans and the native `run.status` enum; `verified` is the existing exact receipt check |
| Camera | `#camera-lens` (`glyph`, camera.js) | `lensView({phase, offline})` | camera.js's own state: `live` needs a stream, a "Live" status and a decoded frame newer than 1.5 s |
| Updates | `#update-rail` (`rail`, updates.js) | `updaterView({offline, unreachable, job, acknowledged, checking, available, supported, checked})` | durable `job.state` and `acknowledged`; `checking` only while a real /check request is in flight |

Phase rules:

- **Presence.** Awake waiting, standby, listening, thinking, speaking, paused (meeting/Kids), asleep, trouble, offline and idle (unknown/starting).
- **Workbench.** idle, discussing, approval, working (submitting/running/verifying), stopping, uncertain (disconnected/submission_unknown/lost connection), finished (completed but unverified), ready (verified), stopped, trouble and offline. Unknown statuses read "Status unconfirmed".
- **Lens.** off, ready, connecting, live, held (frames stale), paused (playback blocked), privacy, trouble and offline. The camera viewer also gets a `data-lens` edge tone *outside* the video, which is disabled in native and fallback fullscreen.
- **Updater.** queued → downloading → installing → restarting (stopping/starting) → verifying, then succeeded, rolling_back, rolled_back or failed. Other phases: available, checking, idle (up to date / not checked / unsupported), unreachable and offline. There is no percentage and no timer.

Guards: every update re-reads `HomebodyOwner.isOwner()`. The bench inherits the native module's epoch/controller fence, so a reset fences an admitted response. The rail ignores reads that started before the last applied one. The lens follows the camera module's play/stream generation guards. `HomebodyNativeWorkspace.sync()` repaints the bench on each status poll without fetching.

## Motion primitives (`motion.js`)

```js
const handle = HomebodyMotion.pending(button);   // data-hb-motion="pending" while a real request is in flight
HomebodyMotion.settle(handle, "confirmed" | "failed" | "cleared"); // false if superseded/reset; marks clear after 1.4 s
HomebodyMotion.clear(button);
HomebodyMotion.enter(element);    // one-shot rise; skipped under reduced motion or a hidden page
HomebodyMotion.disclosure(details); // bound automatically for details[data-hb-disclosure]
HomebodyMotion.reset();           // owner loss: drop every mark and timer
```

"confirmed" means the server accepted that exact request. It is used for Select session, Review request, Approve, Show conversation (only if the display really turned on) and Check for updates. Stop requests are never marked confirmed. There are no MutationObservers, intervals, storage or network calls.

## Local showcase

`docs/scenes/ui-motion-showcase.html` renders every adult phase and the button states under a "SIMULATED STATES" banner. Like the Kids showcase, it is a local `file://` review fixture and is not packaged. `tests/test_ui_motion_browser.py` captures review screenshots into `.artifacts/ui-motion/`, which is untracked.

Honest limits: the presence pulses and arcs are decoration, not audio levels. The workbench cannot know progress inside a run. The lens reflects frame freshness in this browser, not camera-to-screen latency. A rail that shows "Reading saved progress" during a restart is expected.

### Kids showcase

`docs/scenes/showcase.html` renders every scene and phase from simulated views, under a
"SIMULATED STATES" banner. It is a review fixture, not production telemetry, and is not shipped in
the package. Open it directly from a checkout with `file://`; it needs no server or network.

`tests/test_scene_browser.py` also captures review screenshots into `.artifacts/kids-motion/`, which is untracked.
