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

- **Robot.** The power section is the page's hero: "Reachy is *Awake*", the mic / camera / motors facts
  as one quiet row, then Wake up / Standby / Stop movement centred, then Meeting · Sleep folded. Under
  Move Reachy the old dark "Motor state" block became one readiness line (dot, live motor text,
  readiness) with Stop action beside it. The live motor text stays visible so a real error still shows.
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
