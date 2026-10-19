# UI shell: the robot is the interface

Design reference: `design-preview/` (static prototype with fixture content; not served by the app).
Production: `homebody/static/index.html` plus `fable.css`, which is loaded last and owns the look.

## Composition

- Every page opens with a quiet page name, then Reachy's live scene (scene.js) and a serif sentence
  from the runtime. The scene phases are the truth: nothing in the hero is invented copy.
- Surfaces are hairline-separated sections on warm paper. There are no card boxes, no coloured left
  borders, and kicker labels above headings are hidden.
- Three type voices: serif for Reachy's words and headings, sans for controls, mono for work evidence
  (file names, checks, commit hashes, receipts).
- One icon family (the `#i-*` sprite in index.html), 24 grid, one stroke weight.

## Navigation

- Phone: a fixed row with Everyday, Workspace, Kids, Robot. Settings is the gear beside the connection
  dot. The More menu no longer exists; `#tab-more` stays in the DOM (hidden) for the kids-lock code path.
- Wide screens (≥ 900 px): the header becomes a left rail; Settings sits at its foot.
- Announce is an Everyday action (`#everyday-announce-link`) with a back link; its tab button is kept,
  hidden, so `#announce` deep links and `activateTab("announce")` still work.
- The tabs carry the voice route: a real tap on Workspace switches to the agent profile and connects
  (`HomebodyWorkspace.enter()`); a real tap on Everyday switches back through the same profile API with
  approved project work preserved. The two route buttons remain in the DOM (hidden) for the Workspace
  gate and scripts; the confirmed route is named in the status strip ("Voice goes to …"). Programmatic
  navigation (`activateTab`, hash restore, Android restore) never switches the route.
- The home-screen shortcut card and the remembered-device controls live in Settings; the shortcut card
  is hidden inside the Android app's WebView (`runningInWrapper()`).

## Pages

- Everyday: hero (live scene and state), one primary action derived from the live state (Stop /
  Stop speaking during a voice turn, Standby while Awake, Wake up from Standby / Meeting / Sleep,
  disabled "Reconnecting…" when status is unreachable, hidden in Kids) that forwards one click to the
  real control, three small chips (Look / Announce / Move), then the voice strip (route line, work badge,
  Stop all work when work runs) and the last exchange. Power controls (status row, Wake up / Standby /
  Stop movement, Meeting · Sleep) live in the Robot page's power section; the motor bar keeps Stop action.
  On phones the header scrolls with the page so it never covers the hero.
- Workspace: project chip, Chat / Work / Project (segmented on phones, side by side wider). Reachy's
  turns are serif prose, yours are bubbles, evidence stays in Work.
- Kids: stage first, "What shall we play?", Start playing (opens the unchanged grown-up check where
  Play together starts the session), Just one game, the idea cloud (the six activities as starters;
  choosing one never starts anything), End and fold, and one folded grown-up note.
- Robot: Wake / Fold / Stop, look pad, expressions, dances, camera; precision, gamepad and physical
  buttons folded.
- Settings: an index list, then the unchanged sections (`#voice-card`, `#agent-setup-card`,
  `#speaker-card`, `#updates-card`, `#lifecycle-card`).

## Tests

`tests/test_fable_shell_browser.py` covers the row and rail, every destination at 390 and 1440 in
light and dark, Everyday actions, deep links, Kids start/stop, Robot and Settings reachability and the
Workspace thread. Screenshots land in `HOMEBODY_TEST_SCREENSHOTS` (default `.artifacts/screenshots`)
under `fable/`.
