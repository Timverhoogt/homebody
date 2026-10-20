# Fable alpha: one vocabulary for the adult pages

Scope: Everyday, Workspace, Robot, Announce and Settings in `homebody/static/`. Kids keeps its own
playful rules and is untouched. No backend, routing, endpoint, DOM id or event binding changed.

## What is preserved

- The warm paper/ink palette, the orange accent, the serif/sans/mono type voices.
- The Everyday composition Tim approved: illustration beside the serif state sentence, one full-width
  orange primary, three small secondary chips, then the voice route line.
- The bottom row on phones and the left rail on wide screens. Settings is the gear in the header.
- Owner, privacy and Stop behaviour. Every Stop stays reachable; `#robot-stop-button` is now the one
  accent beside the motor readiness line, filled red only while an action is in flight.

## The six elements

All of them live at the end of `fable.css` under "Fable alpha" and apply to every adult page.

1. **Page eyebrow.** Small capitals, left. The page name and nothing else.
2. **Section.** A hairline above, a serif `h2`, one optional short note under it. Left aligned. No
   boxes, no shadows, no dark panels. Nested sections in Settings are the same thing, smaller.
3. **Button.** Four looks, assigned by role, never by "whatever is left over":
   - primary (orange fill): each page's one thing to do, matched by id in `fable.css` (Wake up /
     Standby on Everyday, Connect on Workspace, Announce now, Start live camera, Save, Approve);
   - filled (ink): an ordinary action (Perform expression, Review request, Scan);
   - secondary (hairline pill): everything else, including the Everyday chips;
   - danger (hairline red pill): every Stop and every destructive action.
   The current power mode (`aria-pressed`) is an orange-outlined pill with orange text, so it reads as
   "where Reachy is" rather than as a second filled action. Disabled is one flat muted fill with no
   grey filter. Rows of buttons are centred, including wrapped rows, and equal-width where they are
   siblings (the three Everyday chips, Wake up / Standby).
4. **Field.** One rounded input, select or textarea on the inset paper tone, orange focus ring.
5. **Status.** One pill badge (sans, not mono capitals) with ok / running / failed colours. One message
   style with a coloured left edge for ok / error; empty messages take no space.
6. **Disclosure.** `details` with a round + / − at the right, used for everything secondary.

## Page by page

- **Robot.** Recomposed around three tasks with one state (composition pass after 81d0767):
  1. *State header*, one line: "Reachy is *Awake*" with its detail and the mic · camera · motors facts on
     the left; Wake up / Standby as one compact pair on the right (stacked under the state on phones),
     Meeting · Sleep folded beneath them. It is a header, not a hero: nothing is pushed below the fold.
  2. *Look*, the focal block and the first thing in the viewport: the d-pad centred (76 px round keys,
     ink centre), the one Stop action under it, and the live motor text + readiness as a centred caption.
     The motor text still carries real errors; busy fills Stop red. `#global-robot-stop` is hidden on
     Robot because it calls the same `stopRobotMovement` as `#robot-stop-button` (verified in main.js);
     its id and handler remain in the DOM. The camera overlay's Stop is a different scope (camera.js
     emergency stop) and is untouched.
  3. *Camera*, its own task, beside Look on wide screens and directly under it on phones (CSS order; the
     DOM order is unchanged). Opt-in as before; Start live camera is the only orange fill on the page.
  Gestures (expression select + Perform, three dance chips) are one quieter row under Look; precision,
  Bluetooth and GPIO stay folded. Markup moved: the motor bar now sits under the d-pad inside
  `.look-stage`, dance joined expression in `.robot-gestures`, and the old "Move Reachy" heading is gone.
  All Robot layout rules live in one `fable.css` block ("Robot: three tasks"); the three earlier Robot
  override layers were removed rather than stacked on.
  Follow-up after the first render: (a) the power group is pinned to column 2, row 1 of the header and
  right-aligned (`style.css` had spanned `.global-adult-controls` across the grid, which is why it floated
  centred); (b) the visible Stop is named **Stop movement** everywhere it is referenced, same handler;
  (c) the motor line and readiness live in a native "Movement details" fold under Stop. `main.js` opens the
  fold, and never closes it, when motors are unknown, `last_error` or `robot_action_last_error` is set, the
  mode is not Awake/Standby, or live status is unreachable; healthy Awake / Standby start closed. The
  healthy-Awake subtitle "Motor torque enabled" is hidden by a `data-routine` flag because the Motors fact
  beside it says the same thing; transition, unconfirmed, offline and Standby subtitles still show.
  Asset revision 117 → 118 for this candidate; package stays 0.5.45.
- **Announce.** Text first, four quick templates, then one visible "Reachy will …" choice that states the
  movement behaviour, then a full-width Announce now with Stop & clear queue under it. Provider, model,
  voice, repeat and pause fold into "Voice and repeat options". Status is a small section below.
- **Workspace.** While disconnected the Chat pane no longer repeats "Connect to show this conversation";
  `#workspace-state` stays visible because it carries "Cleared…" and connection errors. Segments and
  the project header use the same pill and hairline rules as everything else.
- **Settings.** The index is a row of chips. Each of the four groups is a hairline section whose summary
  is the serif heading; the cards inside are plain subsections separated by hairlines. Inputs, toggles
  and actions follow the shared rules; the danger zone is a red heading, not a red box.
- **Everyday.** Same composition, tuned for symmetry: the illustration column is narrower (118 px,
  112 px on phones) so the greeting has an inset and room; the state label stays on one line; the
  three chips are equal width in a centred 440 px row under the full-width primary; the voice strip,
  conversation line and companion section share one 14–16 px rhythm. The companion row never wraps
  (Look / Ask about it / Stop share one height). The hero also stops inheriting the dark-screen
  tokens of the old `.display` class, which had made hairline chips invisible in light mode.
  Under 360 px the hero stacks and centres: illustration, state line (wrapping, never clipped),
  greeting, full-width primary, chips that may wrap to a second centred line. Touch targets stay 48 px
  and the page has no horizontal scroll.
- **Asset revision.** Static assets and the service-worker shell cache moved from 116 to 117 so a
  cached shell picks up the new CSS. The package version stays 0.5.45; this is a local preview, not a
  release.

## Not done here

- No manual light/dark switch (system preference only, as before).
- Kids is out of scope.
- Deeper Settings regrouping (moving cards between groups) would need settings-layout.js changes and
  test updates; this pass only restyles the groups it already builds.
