# Dutch Kids voice configuration

Homebody 0.5.16 supports an optional Netherlands-Dutch Kids voice without changing English Kids or adult Live/Realtime speech. The fixed Ruth voice is **Ruth — Friendly Children's Storyteller**, ElevenLabs voice `yO6w2xlECAQRFP6pX7Hw` (`nl-NL`). Voice availability and charges depend on the host's ElevenLabs account.

On the trusted bridge host, configure:

```dotenv
ELEVENLABS_KIDS_NL_VOICE_ID=yO6w2xlECAQRFP6pX7Hw
```

Keep the existing `ELEVENLABS_KIDS_VOICE_ID` and adult voice settings unchanged. A package install does not edit this setting or restart the host bridge; host and robot updates are separate operations. Do not put provider credentials in the robot, public repository or release notes. Restart the bridge only after confirming no live Kids/conversation session is interrupted.

The streamed voice follows the validated language captured in the exact, single-use moderated-text speech approval. Speech callers cannot override it with a voice/language field. The existing configured speech fallback is unchanged; provider failure does not guarantee Ruth playback.

Fixed Kids lines (Start greeting or first I Spy clue, game-host prompts, the five-minute notice) use the same voice.

- Reachy sends `POST /v1/kids/speech/approve` with the session, its validated age band, activity and language, and one or two **catalog selectors** (`greeting`, `host` with a key and activity-id slots, `notice`, or `ispy_clue`). It never sends text.
- The bridge renders the line from `companion/kids_fixed_copy.py`. The I Spy clue comes from the bridge's own validated target. The bridge moderates the rendered line and returns it with two single-use exact-text approvals. Reachy speaks that returned text unchanged.
- The first approval pins the session's age band, activity and language. A later chat, I Spy selection or approval with a different profile is refused.
- The caregiver-typed nickname is free text, so it is no longer part of any spoken fixed line (the host lobby says "Hi!").
- The line plays through `/v1/kids/speech/stream`, or the child-only `/v1/kids/speech/fallback`, never the generic `/v1/audio/speech`.
- A line whose session or generation ended while it waited is dropped. If a line that should hand over to listening (greeting, host prompt) fails to be approved or synthesized while its session is current, Kids Mode ends with reason `speech_failed` instead of staying live and silent.
- **Update the host bridge before the robot package.** An older bridge has no approval route, so a newer robot's Kids Start would end immediately.

Kids chat deadlines on the bridge:

- Each moderation call: 6 s.
- The Kids model call: 15 s.
- An I Spy judge or player-guess call: 8 s.
- The whole bridge turn: 25 s, whichever runs out first.
- Reachy's Kids chat client waits 30 s, and 10 s for a fixed-line approval.

A transient failure answers with a fixed localized retry line ("Sorry, that didn't work just now…"). This covers a moderation timeout or outage, a model timeout or error, an unusable model answer, and an I Spy judge timeout. History and game state stay unchanged, and an unscreened model answer is never committed or approved. A hosted round reports `game_state: playing`. A held result after End/expiry/replacement gets HTTP 409 and no approval. Request cancellation propagates and is never turned into speech.

These budgets cover the bridge's text turn only. Speech streaming, fallback synthesis and playback have their own limits. On the subscription transport, a timed-out provider call may still finish in its worker thread after the bridge has moved on; its result is discarded.

The package includes three short, content-free Ruth Dutch waiting clips in a separate fixed bundle. Original Jessica English/Dutch clips remain for compatibility. Each bundle retains the strict manifest, model, file hash, path, PCM and duration checks. Clips are eligible only when the current bridge digest/model proof for the caregiver-selected turn language matches. Missing, mismatched or malformed proofs are silent; there is no per-turn filler synthesis or weakened voice check. Older language-agnostic clients receive no proof when Dutch and English voices differ.

Waiting clips remain delayed, sparse and interruptible during transcription. They do not reliably cover later reasoning and are not a claim that Reachy understood the child. The Kids-only blocking processing cue is removed, and new timing marks distinguish native/client recognition, approved text, first provider PCM and SDK enqueue. Enqueue is not a measurement of audible speech.

After exact-resource/configuration/provider readback, a caregiver must still try an actual Dutch story and Stop/End to accept pronunciation and conversational pacing. Silent generated clips and automated fixtures do not establish microphone/speaker or child-speech acceptance.
