# Kids Mode

Kids Mode is a supervised beta feature, not a babysitter, therapist, medical, or emergency service. Generative replies can still be wrong.

## Setup

A parent picks an optional nickname, age band (4–6, 7–9, or 10–12), English or Dutch, a 15–60 minute limit, and one of six activities:

- Buddy chat
- Story maker
- Quiz quest
- Riddle box
- Calm corner
- I Spy

I Spy is the only activity that uses the camera.

## I Spy

I Spy alternates roles between Reachy and the child.

**Reachy's turn:** Reachy turns its rotating base and head through a bounded five-frame desk search at `0° / −60° / −120° / +60° / +120°` with non-capturing 60° transit waypoints and a neutral return. Its strict bridge schema accepts only a stable, child-safe target visible across frames. Local game state controls approved hints and reveals the answer by the sixth incorrect guess.

**Child's turn:** The child chooses a safe household object and supplies one clue at a time. Reachy makes up to six schema-bounded guesses with small base-and-head thinking turns and no camera. A confirmed answer or reveal starts Reachy's next consented search round.

Camera access is revoked before the return movement and guessing. Targets use a strict broker schema. Bridge state is deleted on Stop or expiry.

### Say or type your guess

In an active Homebody I Spy game, the Kids tab now shows **Your I Spy guess**. Type an object, ask for a hint, or enter a yes/no confirmation for Reachy's guess, then press **Guess** or Enter. The field accepts up to 80 characters. It is disabled while Reachy is searching, speaking or processing a turn; wait for that turn to finish. Spoken guessing remains available.

Typing skips microphone recording and speech recognition for that turn. **Reachy still replies aloud**, using the same moderated game and exact-approved Kids speech path. The field does not start/wake a session or enable new camera or motion permissions; a normal caregiver-started, already-Awake game is required. Any next consented round retains the existing I Spy choreography.

Guesses and input capabilities stay out of browser storage and status. The draft is cleared when the activity ends/changes. Expiry, End, privacy, replacement and replayed/stale submissions fail closed. Input uses a separate ephemeral one-use capability, not an exposed private game-session ID.


## Activity stage

The Kids display shows an illustrated scene for the chosen activity:

- **Buddy:** golden-hour orbit
- **Story maker:** paper theatre
- **Quiz quest:** compass island
- **Riddle box:** lantern-lit box
- **Calm corner:** moonlit pond
- **I Spy:** explorer's desk with a lens

Before a session starts, the stage is a still preview. During a session it shows waiting, listening, thinking or speaking from the existing categorical runtime state. In I Spy, a "Camera on" search appears only while status reports the bounded camera search.

The stage never shows what was said, guessed or chosen, and never shows scores or answers. It does not move while hidden, off screen, ended, offline or under reduced-motion settings. End, typed guesses and all controls stay outside it. See [Scenes](scenes.md).

## Safety boundaries

Kids Mode is deliberately separate from the normal Hermes agent session. It opens a fresh, bounded child pipeline through the private `/v1/kids/chat` route, with pre/post moderation and no Hermes memory or tool session.

Outside the explicitly consented I Spy search, these are all unavailable:

- Camera
- Agent and delegation tools
- Files and messaging
- Home Assistant
- Purchases
- Power tools
- Explicit robot actions

## Speech

Normalized, approved complete responses receive separate short-lived, single-use bridge capabilities for streaming and configured-TTS fallback, each bound to the child session and exact text. Replies stream through fixed-policy ElevenLabs Flash v2.5 as 24 kHz PCM. Unmoderated model tokens are never sent directly to speech. The configured app voice is retained as a failure fallback.

Optional motion is limited to gentle local listening, thinking, and speaking cues, except for I Spy's bounded base-and-head search poses and camera-free player-turn guessing poses.

## Lifecycle

Starting Kids Mode closes any prior conversation. Ending it:

- Invalidates the child session and deletes I Spy target state
- Interrupts active streaming TTS and audio playback
- Cancels movement and clears queued speech
- Runs the verified safe fold before torque release

Synchronous STT, chat, and provider HTTP calls are not transport-aborted and may run until their bounded timeout, but their returned output is discarded. A monotonic server timer enforces the limit and gives a five-minute warning.

## Data flow

- **Child audio** goes to the configured STT provider.
- **Moderated child text and consented I Spy frames** go to OpenAI.
- **Approved reply text** goes to ElevenLabs.
- The optional nickname is included in the deterministic ElevenLabs greeting.
- I Spy frame bytes are discarded after target selection and are not retained in child session state.

Exclusion from Hermes memory is not a provider-retention guarantee. Review each provider's data controls before use.
