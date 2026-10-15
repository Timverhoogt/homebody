# Kids languages: deployment readiness (ES/DE/FR/IT/PT)

Status: **source-integrated and unit/browser-tested on a development branch. Not deployed, not enabled, not accepted.** Spanish, German, French, Italian and Portuguese stay unavailable on every installation until each has its own owner-selected voice and passes the manual gates below.

## What is in source

- **One strict catalog for all seven languages.** `companion/kids_fixed_copy.py` holds the greetings, the five-minute notice, activity names and game-host lines for `en`, `nl`, `es`, `de`, `fr`, `it` and `pt`.
  - Reachy sends catalog selectors; the bridge renders, moderates and approves the exact text.
  - The bridge never accepts caller text, a nickname, or a voice/model/provider choice for fixed speech.
  - The robot preview copy (`homebody/kids_mode.py`, `homebody/kids_game_master.py`) is pinned byte-for-byte to the catalog by `tests/test_kids_turn_deadlines_lifecycle.py`.
- **Validated profile per session.** The age band, activity and language are fixed per session by the first approval or turn. A later different language is refused.
- **Bridge copy.** Safety, no-answer, retry and I Spy copy is per language (`_KIDS_COPY`, `_KIDS_MODEL_RETRY_REPLY`). An unsupported language raises an error; it never falls back to English.
- **Own voice only.** Each new language speaks only with `ELEVENLABS_KIDS_<LANG>_VOICE_ID` on the bridge host.
  - There is no fallback to the default (Jessica) or Dutch (Ruth) Kids voice.
  - There is no fallback to configured or adult speech: the fallback route refuses these languages.
  - EN/NL voice selection, including the Dutch Ruth override, is unchanged.
- **Start admission.** Start in a new language requires that language's own bridge voice proof (digest plus Flash model). The check runs twice:
  - in the owner start route;
  - in the runtime, before any presentation, camera, session, motion, greeting, timer or microphone side effect.

  The runtime snapshots the Stop/End fence before the network proof and revalidates it inside the motor-before-Kids locks, so a Stop or End during the proof wins.
- **I Spy.**
  - A new-language session requires `hints_<lang>` in the strict vision schema. That schema is sent to the provider and preserved by the subscription native projection.
  - The bridge parser and the robot validator enforce it.
  - Hints are screened and moderated.
  - A session never speaks English hints in place of its own.
- **Commands and recognition.**
  - Language-scoped Stop/help/again/different/switch phrases, including German `weiß`/`weiss` folding and French/Italian elision splitting. Quoted text and long narration stay with the game.
  - The recognition language code is passed to the robot worker, the host-local Whisper hint and the bridge.
- **Waiting clips.** Optional per-language bundle directories are supported, but none is shipped, so these sessions have no waiting filler. Integrity checks on the real EN/NL bundles are unchanged.
- **Pickers.** The pickers offer the five languages; static assets and the PWA shell are at revision 98. Picker availability is not readiness: Start is refused with a clear message until the voice proof exists.

## Source readiness check

`tools/kids_language_readiness.py` is source-only: no environment, secrets, provider, robot or network. For each new language it reports three separate things:

- **source**: catalog completeness and propagation across every layer (bridge catalog and session set, robot profile, game-master copy, I Spy colours and hints, waiting phrases, STT worker, host STT hint, start route, both pickers);
- **config**: whether a voice proof was *supplied* by the caller from bridge health, and whether a waiting-clip bundle exists;
- **acceptance**: manual gates, always `pending_manual`, plus `enable_for_children: false`.

A valid voice digest only proves which voice ID is configured. It never satisfies an acceptance gate. `tests/test_kids_language_readiness.py` pins this behaviour.

## Owner decisions and manual gates (not done)

No new voice ID, regional variant, waiting clip or approval has been chosen or generated. The only owner voice choice on record is Dutch Ruth.

1. **Choose voice and region per language.** Audition native, child-appropriate voices that support `eleven_flash_v2_5`, and decide the regional variant. **Portuguese copy uses provisional European Portuguese forms** (`tu`, `ecrã`, `telemóvel`, `ronda`); the owner must choose European or Brazilian before review.
2. **Configure only the bridge host** (never the robot or the repository): `ELEVENLABS_KIDS_ES_VOICE_ID`, `..._DE_...`, `..._FR_...`, `..._IT_...`, `..._PT_...`. Leave the default, NL and adult settings unchanged.
3. **Native-speaker review** of all fixed copy and phrase lists: the catalog, robot copy, bridge `_KIDS_COPY`, retry replies, I Spy copy and host commands.
4. **Provider and screening acceptance** with the selected voices.
5. **Recognition accuracy on the robot.** Test short answers, Stop/help/switch phrases, silence/noise and latency in each language. Whether the provisioned model honours a fixed language code is a device property.
6. **Optional waiting clips**, generated with the selected voice from the fixed phrases only, under a strict manifest.
7. **Caregiver-supervised acceptance**, microphone to speaker, per language: a story, an I Spy round, a game switch, Stop and End.

Enable a language for children only after its own gates pass.
