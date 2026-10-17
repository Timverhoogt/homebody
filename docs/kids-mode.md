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

## Play together (game host)

This section describes the implemented candidate in this source tree. It is not yet deployed or physically accepted on Reachy.

**Play together** is the primary Start on the Kids page. Reachy hosts one time-boxed session and the child picks games by voice or by typing in the same box I Spy uses. **Start chosen activity** still starts one activity directly.

How it works for families:

1. Reachy greets in the selected language and offers two games, for example: "Shall we do a story or riddles? You can also say stop." It then listens with no wake phrase.
2. The child names a game in their own words ("verhaaltje", "let's do riddles", "ik zie ik zie"). Exact names are not needed.
3. When a round ends, Reachy offers two options: another round or one different game. "Stop" always finishes. Reachy does not try to persuade the child to keep playing.
4. During a game the child can say "stop", ask "what can I say?", or name a different game. A plain "help", "again", "no", "I don't know", or "ik weet het niet" stays with the game, for example as a hint or an I Spy give-up. The host treats these as help only while the child is choosing a game. Longer sentences and quoted words go to the game too, so a story line like *the bus had to stop* is not a command.
5. If an answer to a choice isn't recognised, Reachy asks again at most twice. Then it waits quietly. The child can wake Reachy and name a game, or a grown-up can choose one.

Caregiver controls stay on the dashboard: **End now & fold safely**, plus **Choose the next game** buttons while a session runs.

Camera: a hosted session can only offer I Spy if a grown-up allows it. There are two ways: tick **Allow I Spy camera during Play together** before Start, or press **Play I Spy (allows camera)** and confirm. If the child asks for I Spy without that permission, Reachy says a grown-up must allow the camera first. The dashboard shows the same notice. A child's voice never grants camera access. Switching away from I Spy cancels any capture or search still in progress, and late results are discarded. After a finished I Spy round, Reachy offers choices before it looks around again.

Rounds end in one of two ways:

- For chat, story, quiz, riddles, and calm, the bridge returns a strict `game_state` (`playing` or `finished`). There is also a fixed cap on child turns per round.
- For I Spy, the finished player round is the end.

Moderation is unchanged. A flagged reply is always reported as `playing`.

A child's "stop" is tied to the outer play session. Each caregiver Start creates that session identity, and game switches keep it, so a Stop that races a game switch still ends the session and no new-game greeting plays. A Stop left over from an earlier session never ends a later Start. While a switch or I Spy search is running, the shared typed box accepts only "stop", using the same single-use token; other game input is refused until the next game is ready. In a hosted I Spy round the bridge keeps the real outcome: "Yes! I guessed it!" or "You beat me! Clever object!". It drops the automatic "I'll look around" search line.

Each game runs under its own bridge session ID and Kids generation. Switching games cancels every reply, audio stream, camera search, first-listen ticket, and bridge response still in flight from the old game. Switching does not move the session's time limit. Stop, End, privacy, Meeting/Sleep, and the time limit always win.

Owner-safe status adds a categorical `kids_mode.host` object with these fields: `enabled`, `phase` (`lobby`, `transition`, `playing`, `offer`, `waiting`), `activity`, `choices`, `notice`, and `available`. It never contains answers, transcripts, targets, or scores. The Kids stage has its own authored "game host" scene for the lobby, switching, and round-done phases. It pauses when the page is hidden or offscreen, and shows only a still image with a caption when reduced motion is on.

The host's own lines (greeting, offers, help, camera-needed) are fixed, deterministic text. They are spoken through the same Kids announcement path as the Start greeting, with the same Kids voice. Model replies still go through bridge moderation and the single-use exact-text speech approvals.

Evidence so far: unit tests, real-middleware owner-boundary tests, and Chromium tests using explicit status fixtures. A supervised check on a physical Reachy is still needed. It should cover hearing NL/EN game names, the time from greeting to first listen after a switch, and Stop during a hosted I Spy search.

## I Spy

I Spy alternates roles between Reachy and the child.

**Reachy's turn:** Reachy turns its rotating base and head through a bounded five-frame search of nearby surroundings at `0° / −60° / −120° / +60° / +120°` with non-capturing 60° transit waypoints and a neutral return. Its strict bridge schema accepts only a stable, child-safe target visible across frames. Local game state controls approved hints and reveals the answer after three meaningful incorrect guesses; hint requests do not consume guesses, and giving up reveals immediately.

**Target language:** the vision request and its per-session schema explicitly require `object_name` in the selected play language (for example Dutch `bal` or `mok`, not English labels). The screened name is retained unchanged for that round and used in correct-answer and reveal sentences. JSON keys and the internal colour enum stay unchanged; language selection is not a translation of those machine identifiers. This is a model-language instruction, not a deterministic language detector; generative naming can still make mistakes. This logic runs on the host bridge, so installing a robot wheel alone does not activate a bridge fix.

**Target variety:** vision proposes up to four candidates. The bridge validates each one against the same target policy, drops unsafe, malformed or low-confidence entries, and merges repeats and aliases, including same-frame overlapping boxes. It then shuffles the remaining safe objects with a system random source and moderates them in that order. The first approved object becomes the round's only target and stays fixed until the round ends. Objects already used this session go last; that short in-memory list is deleted on End, cancel or expiry. One safe candidate is used as is. If none are safe, selection fails without inventing a target. A result that arrives after Stop, cancel, expiry or a newer search is discarded.

**Child's turn — visual guessing (0.5.26, matching host bridge required):** When the child takes a turn, Reachy performs a consented five-view search and retains a small, screened list of visible safe objects. It combines the child's clues with that list and chooses only an observed object, using its stored session-language name. The model cannot supply an arbitrary new object name. Wrong guesses are excluded, and the existing three-guess limit remains. If nothing fits, Reachy asks for another clue rather than claiming to see an invented object. It may request one additional bounded look when needed; there is no continuous camera and no scan after every clue. An empty view stays empty.

The scan belongs to the current I Spy activity, including Play together. Interrupted handoff speech can re-offer the same unused view request after the next explicit input. Stop during a search abandons only that token-bound view, clears its observations and charges the interrupted attempt against the two-look budget; it does not delete the still-active game or automatically resume motion. End still deletes the game. A confirmed answer or reveal still finishes a hosted round and offers choices; it does not automatically launch another robot-picker round. Small thinking gestures are not themselves camera searches.

Camera access is active only during an admitted search and revoked before return movement and guessing. Each child-turn view request has a one-use round-bound token, in addition to existing caregiver consent, Stop/privacy and session fences. Images are transient; observed-object metadata is RAM-only and cleared when the turn/session ends. Observation and guessing errors must never fall back to ungrounded household-object guesses. The host bridge and robot client need a coordinated update for this protocol; deploying only the bridge is not supported.

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
- **I Spy:** explorer's view with a lens

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
