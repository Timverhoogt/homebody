# Kids Chat Show and Tell

**Status: integrated on the `feat/kids-show-and-tell` branch with synthetic and loopback evidence
only.** It has not been tested on a robot, with a real camera, speaker or provider, or with
children. It is unreleased and undeployed, and it has not been reconciled with the separate
motor work.

## Permission model: opt-out within the Owner's camera approval

Show and Tell reuses the Owner's existing explicit approval, **Settings → Camera access for AI**
(`camera_enabled`). That setting is off on a fresh install and can only be changed through the
paired Owner page. Pairing or logging in alone grants nothing.

- If Camera access for AI is **on**, every eligible Kids Chat has Show and Tell available
  automatically. There is no per-session Allow step, challenge or confirmation.
- The Owner can turn Show and Tell **off** (or back on) with one click on the Kids page. This
  persistent opt-out is saved as `kids_show_and_tell_enabled` (default on) and survives restarts
  and later sessions.
- If Camera access for AI is **off**, Show and Tell stays off. Turning the Show and Tell setting
  on never widens the camera approval.
- Eligible means an unhosted Kids Chat (Buddy) session in English or Dutch. Other activities,
  Play together, other languages, I Spy and general snapshots are unchanged. I Spy consent never
  allows Show and Tell, and Show and Tell never allows I Spy.

Permission only makes a look *possible*. Startup, enabling, a permission change or a status read
never captures, wakes Reachy or opens the microphone. Each look still needs an individual
explicit request:

- the child says an exact request such as "look at my drawing", "can you look at my toy",
  "kijk naar mijn tekening" or "ik wil je mijn bouwwerk laten zien"; or
- the Owner presses **Look now**.

There is no scanning, motion, tracking or continuous camera.

## One look

1. Reachy derives an internal grant for this session (a random token plus a monotonic serial)
   from the current saved permission. The bridge accepts a look permit only with that grant. It
   refuses older or revoked serials and never accepts a bare permission flag.
2. Reachy says the bridge-moderated line "I'll look once at your toy, drawing or build now." /
   "Ik kijk nu één keer naar je speelgoed, tekening of bouwwerk." using exact approved Kids TTS.
3. Reachy takes **one** frame.
4. The bridge sends it once to the configured Kids vision route. Unless the host sets
   `REACHY_ISPY_VISION_URL` to a local vision server, that route is the bridge's **external**
   model provider. The frame is not stored.
5. The provider can only return a strict enum: toy, drawing or build, plus a colour and shape.
   The bridge renders a fixed sentence such as "I saw a drawing with blue. What would you like to
   tell me about it?", moderates it and returns it with exact-text speech approvals.
6. People, faces, clothing, screens, documents, medicine, weapons, hazards, private items,
   identifying text, sensitive inferences or uncertainty produce the fixed decline "I can't
   safely tell what that is…". Unavailable vision produces "Sorry, I couldn't look just now…".
   Text in the image is never read, repeated or obeyed.
7. Only the three enums stay in the bridge's session RAM, as a system note for later moderated
   Kids Chat turns. A fresh look replaces them.

A voice-requested look runs inside the child's current turn, which then continues as usual.
Look now never wakes Reachy or opens the microphone. There is no fallback voice, no barge-in
during the look, and no adult or agent route.

## Cancellation

| Event | Outstanding look | Description in RAM | Saved Owner setting |
| --- | --- | --- | --- |
| Stop (any conversation stop, including the five-minute warning) | cancelled | cleared | unchanged |
| Show and Tell turned off | cancelled | cleared | saved off |
| Camera access for AI turned off | cancelled | cleared | camera off |
| End, expiry, replacing Start | cancelled | cleared | unchanged |
| Privacy, Meeting or Sleep | cancelled | cleared | unchanged |

Reachy checks its authority at every boundary: permit, announcement, camera, bridge result,
result speech, and immediately before every PCM handoff. Late work cannot speak, capture or keep
a description. After a Stop, a *later explicit request* may look again under the then-current
permission. A new session always derives a fresh grant. Stale work never resurrects: the bridge
refuses its permit or serial.

A saved revocation always cancels outstanding work, even if a later save re-enabled the setting.
Permission is re-read from the saved settings in serialized order, so a slow, stale read can
never restore an older permission.

## Interfaces

Owner routes (pairing plus same-origin CSRF; both usable while Kids controls are locked):

| Route | Purpose |
| --- | --- |
| `POST /api/kids/show-and-tell/setting` | `{"enabled": true\|false}` (strict boolean, no other fields): persistent opt-out |
| `POST /api/kids/show-and-tell/look-now` | One explicit look as the single voice owner; refused while Reachy is busy |

Camera access for AI stays under `/api/settings`, which remains locked during a Kids session.
Status exposes only `kids_mode.show_and_tell = {camera_approved, enabled, session, available,
looking}`.

Bridge routes (bridge API key): `/v1/kids/show-and-tell/permit`, `/look`, `/cancel` and
`/revoke`. A permit is single-use and expires after 25 s. One look runs at a time, with at
least 5 s between looks. Vision has a 15 s ceiling and moderation has 6 s.

Code: `homebody/show_and_tell.py` (grammar, gate, transaction),
`homebody/show_and_tell_runtime.py` (permission, adapters, lifecycle) and
`companion/kids_show_and_tell.py` (bridge validation and copy). These are wired through
`config.py`, `kids_runtime.py`, `vision_runtime.py`, `voice_pipeline.py`, `runtime.py`,
`hermes_client.py`, `main.py`, the bridge and the Kids page UI.

## Known limits

- Hands are allowed, but a visible face makes Reachy decline. Hold the item close to the camera.
- English and Dutch only.
- Recognition quality, provider safety classification and real latency are untested.
- No physical acceptance has been done. The evidence is synthetic unit tests, loopback aiohttp
  and FastAPI tests, the real pipeline conversation loop with fakes, and fixture-driven Chromium
  tests.
