# Dutch Kids voice configuration

Homebody 0.5.16 supports an optional Netherlands-Dutch Kids voice without changing English Kids or adult Live/Realtime speech. The fixed Ruth voice is **Ruth — Friendly Children's Storyteller**, ElevenLabs voice `yO6w2xlECAQRFP6pX7Hw` (`nl-NL`). Voice availability and charges depend on the host's ElevenLabs account.

On the trusted bridge host, configure:

```dotenv
ELEVENLABS_KIDS_NL_VOICE_ID=yO6w2xlECAQRFP6pX7Hw
```

Keep the existing `ELEVENLABS_KIDS_VOICE_ID` and adult voice settings unchanged. A package install does not edit this setting or restart the host bridge; host and robot updates are separate operations. Do not put provider credentials in the robot, public repository or release notes. Restart the bridge only after confirming no live Kids/conversation session is interrupted.

The streamed voice follows the validated language captured in the exact, single-use moderated-text speech approval. Speech callers cannot override it with a voice/language field. The existing configured speech fallback is unchanged; provider failure does not guarantee Ruth playback.

The package includes three short, content-free Ruth Dutch waiting clips in a separate fixed bundle. Original Jessica English/Dutch clips remain for compatibility. Each bundle retains the strict manifest, model, file hash, path, PCM and duration checks. Clips are eligible only when the current bridge digest/model proof for the caregiver-selected turn language matches. Missing, mismatched or malformed proofs are silent; there is no per-turn filler synthesis or weakened voice check. Older language-agnostic clients receive no proof when Dutch and English voices differ.

Waiting clips remain delayed, sparse and interruptible during transcription. They do not reliably cover later reasoning and are not a claim that Reachy understood the child. The Kids-only blocking processing cue is removed, and new timing marks distinguish native/client recognition, approved text, first provider PCM and SDK enqueue. Enqueue is not a measurement of audible speech.

After exact-resource/configuration/provider readback, a caregiver must still try an actual Dutch story and Stop/End to accept pronunciation and conversational pacing. Silent generated clips and automated fixtures do not establish microphone/speaker or child-speech acceptance.
