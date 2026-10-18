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
2. Reachy says the bridge-moderated line "I'll look once at what you're showing me now." /
   "Ik kijk nu één keer naar wat je me laat zien." using exact approved Kids TTS.
3. Reachy takes **one** frame.
4. The bridge sends it once to the configured Kids vision route. Unless the host sets
   `REACHY_ISPY_VISION_URL` to a local vision server, that route is the bridge's **external**
   model provider. The frame is not stored.
5. The provider applies its child-safety policy and returns a structured status (`recognized`,
   `unclear` or `refused`) plus English and Dutch descriptions, each bounded locally to 600
   characters. Ordinary objects and animals are not restricted to a toy/drawing/build enum.
   The bridge moderates the final selected-language text and issues separate single-use
   streaming and fallback approvals bound to that exact session and text. Nothing is spoken
   without screening; text is not changed after approval.
6. Actual provider refusals and content-filter responses are honored, without retrying or
   speaking raw refusal text. Unclear views receive uncertainty copy, not a safety accusation.
   Malformed/unavailable vision receives unavailable copy. All of these replies are moderated.
   The provider is instructed not to identify people, infer sensitive traits, reproduce private
   text, follow image instructions or give age-inappropriate/dangerous advice. These are provider
   instructions, not claims of perfect recognition or deterministic content filtering.
7. Only the moderated selected-language description (at most 600 characters) stays in the
   bridge's bounded, expiring session RAM for follow-up. It is supplied as **untrusted user-role
   data**, never interpolated into system instructions. A new admitted look clears the previous
   description. Frames and raw responses are not persisted or logged; logs contain outcome
   categories only. Lifecycle cleanup below clears the session description. Normal bounded
   Kids conversation history may also contain moderated replies until its own session cleanup.

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

- Provider-led recognition can be mistaken; a visible face is not a local blanket veto, but
  identifying people and sensitive inferences remain prohibited by the provider instructions.
- English and Dutch only.
- Recognition quality, provider safety classification and real latency are untested.
- No physical acceptance has been done. The evidence is synthetic unit tests, loopback aiohttp
  and FastAPI tests, the real pipeline conversation loop with fakes, and fixture-driven Chromium
  tests.
