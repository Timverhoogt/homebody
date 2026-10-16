# Kids waiting speech (0.5.23)

During longer silent waits in Kids activities, Reachy can now play one short pre-recorded phrase. It reuses the existing STT acknowledgement catalog, player, voice proof, anti-repeat/cooldown and output quarantine (see [kids-interaction-fixes.md](kids-interaction-fixes.md)). It does not add new audio, live TTS filler or a separate speaker thread.

## Phrases and triggers

Only the packaged, hash-verified clips are used: `homebody/assets/kids_acknowledgements/` (EN + NL) and `ruth-nl/` (NL, Ruth).

| Wait | Trigger | Stage | NL | EN |
| --- | --- | --- | --- | --- |
| STT still pending (unchanged) | 1.0 s after transcription starts | any | "Even denken." / "Hmm." / "Eens kijken." | "Let me think." / "Hmm." / "Let's see." |
| I Spy initial search (Start) | 1.0 s after the camera search starts, still searching/choosing | see | "Eens kijken." | "Let's see." |
| Shared play (game master) → I Spy search: first pick, "again" and switching into I Spy (`_kids_host_transition`) | 1.0 s after the camera search starts, still searching/choosing | see | "Eens kijken." | "Let's see." |
| I Spy later search (continuation) | 1.0 s after the search starts, still searching or fetching the clue | see | "Eens kijken." | "Let's see." |
| Kids backend reply (all non-Calm activities, including shared game-master sessions) | 1.5 s after the reply request starts, still waiting | think | "Even denken." / "Hmm." | "Let me think." / "Hmm." |

The delays live in `homebody/kids_acknowledgements.py` (`SEARCH_WAIT_DELAY_SECONDS`, `REPLY_WAIT_DELAY_SECONDS`).

## Rules

- **One clip per wait at most.** It never loops. If the work finishes before the delay, nothing plays. If it finishes after the clip was chosen but before the first PCM push, nothing plays either.
- **Shared 8 s cooldown and variation where possible.** The shared selector avoids the previous clip when another eligible clip is available. Search currently has one matching phrase per voice, so it can repeat on a later search; reply waits rotate between the available thinking phrases. A turn whose recognition clip already started gets no reply clip: that clip may finish, it is not cut, and the reply wait is not armed.
- **The real answer never waits for the clip.** Before the clue or reply speech, the producer is settled with a bounded join (0.5 s) and any admitted audio is flushed.
- **Microphone stays closed.** The clip is never played while recording or listening, and it never opens the microphone, camera or motors. The initial search never enables the microphone or adds camera/motion consent.
- **Fails silent when not verified.** The clip plays only if a fresh bridge proof of the session language's Kids voice (digest + Flash model) matches the bundle. The proof is read after the delay, outside the motor and Kids locks. Silence results from a wrong or missing proof, rejected or missing assets, no clip for that stage (for example EN in the synthetic test catalog), or a language without a verified bundle. There is no wrong-language, wrong-voice or live-TTS substitute.
- **Excluded:** adult routes, Calm, the game master's own fixed lines, the Start greeting and the five-minute warning.
- **Typed I Spy guesses stay silent (explicit choice).** A typed guess is a single on-screen submission with no microphone turn, so it gets no recognition or reply waiting speech even if the reply is slow.

## Ownership and cancellation

- **Continuation search and reply waits** use the existing turn fence: the exact Kids session, the immutable turn stop generation, the stop flag, privacy, Meeting/Sleep and shutdown.
- **Initial Start search and shared-play I Spy searches** cannot use the turn flag: Start or the game-master transition raises it itself, and Awake later clears it. Each binds the immutable Kids generation (which also covers the unchanged outer deadline/expiry) and its own conversation-stop receipt, captured inside the same motor+Kids admission lock that issued it. Any later Stop, End, game switch/replacement, expiry, privacy, Sleep or shutdown therefore silences it. The clip is settled before the clue is handed to the announcement queue.
- **Rechecks:** the fence is checked after the delay, after the proof read, before and after every PCM chunk, and while queued audio is audible.
- **Single producer:** `install_producer` refuses to replace a filler producer that is still running (in flight, playing, watching its queue or resolving a proof). An old blocked SDK push therefore always remains the observed owner. Clue, reply and announcement speech, re-listen and new engines keep honouring the common `output_quarantined` gate until that push returns and flushes its own chunk.

## Tests

`tests/test_kids_waiting_speech.py` exercises `_run_conversation` (plain Kids, shared-game and typed replies), `start_kids_mode` (I Spy), `_kids_host_transition` (shared play → I Spy) and `_continue_ispy_reachy_turn`. It covers:

- healthy controls and stage choice;
- fast-wait skip, STT+reply dedupe and cooldown;
- calm, adult, typed-guess, wrong-proof, wrong-model, missing-asset and unsupported-language silence;
- cancellation during a held proof and between PCM chunks (Stop, End, privacy, replacement/game switch, Standby, shutdown, outer deadline expiry);
- a held SDK push that quarantines output and blocks new producers, after which a healthy later producer is admitted and plays;
- a hung push during a shared-play search that makes the real announcement worker refuse the clue at the common quarantine gate.

## Not done here

- **Unprovisioned bundles:** ES/DE/FR/IT/PT have no verified bundle, so those sessions stay silent.
- **Physical acceptance:** audibility and timing on Reachy's speaker, overlap with the search motion, and real proof latency are untested. That needs a separate live run.
