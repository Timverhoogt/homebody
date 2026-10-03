# Homebody — implementation plan and status

## Goal

Ship a public Reachy Mini Python app that wakes locally on **“Hey Homebody”**, **“Hey Hermes”**, **“Okay Nabu”**, or **“Hey Reachy”**, supports low-latency embodied conversation, preserves the user's Hermes identity and tools in normal mode, and provides a separate supervised Kids Mode with materially reduced capabilities.

## Product principles

- Wake-phrase processing remains local; cloud audio starts only after local detection.
- Provider credentials remain on the Hermes host.
- Reachy stores only a private bridge URL and bearer token.
- Ordinary conversation stays fast; tool-using normal requests may delegate to Hermes.
- Kids Mode is a backend-enforced child pipeline, not a visual theme.
- Camera, private memory, files, messaging, devices, purchases, power, and explicit robot actions are unavailable to the child model.
- Voice, privacy, child-lock, timer, and physical power states remain visible and reversible.
- Every torque release is preceded by a verified safe fold when motor power is available.
- Browser and model actions use allow-listed semantic movement rather than raw motor primitives.

## Implemented architecture

### Realtime mode

```text
Reachy local KWS
  → authenticated private companion WebSocket
  → OpenAI gpt-realtime-2.1
      ↳ ask_hermes → Hermes memory, tools, Home Assistant, files, current information
      ↳ one-frame camera request when explicitly enabled
      ↳ allow-listed local look, emotion, dance, and power-mode tools
  → streamed audio + serialized Reachy motion
```

Implemented:

- OpenAI GA Realtime protocol with 24 kHz PCM and device-rate resampling;
- semantic VAD, local playback accounting, queue flushing, and interruption;
- configurable voice and reasoning effort;
- completed/deduplicated tool-call handling;
- optional one-frame vision, local face following, and wake-time DOA;
- credentials isolated to the Hermes host.

### Normal Hermes pipeline

```text
Reachy local KWS
  → adaptive local endpointing
  → selected STT
  → Hermes API Server agent
  → selected TTS
  → Reachy playback + serialized motion
```

Implemented:

- configured and ElevenLabs STT/TTS selection;
- model and account-voice discovery;
- stable Hermes memory scope with rotating conversation IDs;
- continued conversation;
- playback interruption with any configured local wake phrase.

### Supervised Kids Mode

```text
Reachy local KWS
  → adaptive local endpointing
  → configured STT
  → authenticated /v1/kids/chat
      ↳ bounded ephemeral child history
      ↳ input and complete-output moderation
      ↳ no normal Hermes session, memory, or tools
  → authenticated /v1/kids/speech/stream
      ↳ ElevenLabs Flash v2.5
      ↳ fixed 24 kHz PCM
  → interruptible Reachy playback + optional gentle voice-state motion
```

Implemented:

- five activities: Buddy chat, Story maker, Quiz quest, Riddle box, and Calm corner;
- age bands 4–6, 7–9, and 10–12 in English or Dutch;
- parent-selected 15/30/45/60-minute sessions with a monotonic authoritative deadline and five-minute warning;
- direct Start/Stop controls on the trusted local dashboard with no separate parent PIN;
- public transcript, response-preview, nickname, and child-session-ID redaction while a Kids session is active;
- camera, robot, agent/delegation, Home Assistant, messaging, files, purchases, and power tools blocked;
- complete-answer moderation before any child speech starts;
- fixed-policy Flash streaming, prompt cancellation, queue clearing, parent stop, expiry, safe fold, and torque release.

### Local dashboard and physical lifecycle

Implemented:

- five keyboard-accessible tabs: Dashboard, Kids, Announce, Robot, and Settings;
- exact-text announcement queue with provider/model/voice overrides and physical behavior policy;
- bounded manual look, expression, dance, precision Cartesian/head/base controls, live pose, independent centering, and priority Stop;
- BlueZ controller discovery/pair/trust/connect management plus opt-in `/dev/input/js*` monitoring;
- Sony-vendor DualShock 4/DualSense bounded look, center, Happy, Surprised, and Stop mappings routed through the existing safety gates; unsupported identities fail closed;
- installable Android PWA with synchronized cache versioning;
- Standby, Awake, timed Meeting, Sleep, app-off, and confirmed Pi shutdown;
- private Tailscale-compatible HTTPS/WSS camera deployment;
- serialized movement, privacy rechecks, safe-fold interlocks, and motor/microphone lifecycle management.

## Current verification status

Automated and packaging:

- Ruff, Python compilation, JavaScript syntax, and `git diff --check` pass.
- The current pytest suite passes in a clean Reachy SDK 1.9 environment with GStreamer bindings; run the release command for the current count.
- Wheel and source distributions build successfully.
- `reachy-mini-app-assistant check .` passes all repository structure, metadata, isolated-install, entrypoint, and uninstall checks.
- Tracked and untracked publication files pass provider-key/token-prefix scanning.

Live bridge and cloud providers:

- OpenAI Realtime session creation, audio response, reasoning configuration, and Hermes tool delegation succeed.
- OpenAI moderation and the dedicated child chat route succeed.
- ElevenLabs TTS/STT succeed.
- Kids Flash v2.5 streaming returns fixed 24 kHz PCM; the reference Reachy received its first chunk in approximately 375 ms.

Physical Reachy deployment:

- the app installs and starts through the Reachy daemon;
- the deployed five-tab UI and PWA v18 load without browser-console errors;
- one supervised Dutch Kids session completed STT, moderated child chat, synthesis, parent stop, safe fold, torque release, and Standby;
- `Hey Hermes` is verified with live microphone input;
- `Okay Nabu` and `Hey Reachy` are verified against generated acceptance audio and load in the live three-phrase keyword graph;
- camera, motor, power, app restart, bridge, daemon, and watchdog checks have passed on the reference Reachy Mini Lite;
- on the current Raspberry Pi target, BlueZ 5.82, the onboard adapter, RF unblocking, classic/LE discovery, `bluetoothctl` authorization, and `input` group access are verified; final Reachy Mini Wireless DualShock pairing and mapping acceptance is deferred;
- the green GPIO17 button is electrically verified with one debounced falling edge on press and one rising edge on release. The app integrates the buttons through libgpiod: red Stop/Sleep, green listen/Awake/Standby, startup ownership, and stuck-button lockout. Two-button acceptance on hardware is still open.

Human acceptance still required per hardware/audio environment:

- real spoken initial-wake checks for all three wake phrases;
- natural Realtime turn-taking and acoustic interruption at normal room distance;
- microphone/speaker quality after enclosure, cooling, or wiring changes;
- caregiver review of Kids Mode outputs appropriate to the intended age and activity.

## Performance observations

| Path | Reference observation |
|---|---:|
| Native Realtime short response | ~1.2 s |
| ElevenLabs TTS | ~0.6 s |
| Kids Flash PCM first chunk | ~0.375 s |
| ElevenLabs STT | ~1.1 s |
| Full Hermes pipeline agent request | ~14 s |
| Realtime request invoking `ask_hermes` | ~23 s in validation |

These are deployment observations, not service-level guarantees. Hermes pipeline latency is dominated by agent/context preparation; Realtime is the low-latency normal conversational path, while Kids Mode prioritizes moderation and capability reduction before streamed speech.

## Packaging and release scope

- Package, module and app entry point: `reachy-mini-homebody` / `homebody` / `reachy_mini_homebody` (class `Homebody`); previously `reachy_mini_hermes` / `ReachyMiniHermes`.
- Current release line: **0.4.x early alpha**.
- Reachy settings UI: port `8042`.
- Companion bridge: port `8643`, loopback by default.
- License: Apache-2.0 with retained third-party notices.
- GitHub is the source/release surface; the Hugging Face static Space is the public app page and mirrored app-discovery repository.

## Next steps

Updated 2026-10-03. Agent Mode 0.1–0.6 and warm Hermes agents have shipped; see `docs/plans/2026-07-19-agent-mode-roadmap.md` for the agent roadmap status.

### Build

- Agent Mode Phase 6, Maintenance 0.1: separate disabled-by-default toggle with phone plus admin PIN; sanitized diagnostics first, then allowlisted service restart, signed update with checksum verification, redacted support bundle and rollback. Test update, rollback and interrupted-update recovery on a non-production install. Never in Realtime schemas or Kids Mode.
- Add local offline STT/TTS fallback for deployments that require operation without cloud providers.
- Replace the raw watchdog TCP media probe with a WebSocket-aware check to remove benign handshake warnings.
- Persist parent lock/session recovery policy across process restarts if the deployment requires crash continuity.
- Expand simulated robot integration tests. GitHub Actions (`.github/workflows/ci.yml`) now runs lint, JavaScript syntax checks, the full suite with the Reachy SDK stubbed, and the package build. A CI image with the real Reachy SDK/GStreamer imports is still open.

### Live acceptance

- Run live acceptance of warm Hermes agents (`REACHY_HERMES_WARM_AGENTS=1`) against the deployed Hermes Agent release: confirm `/v1/warm-agents` reports available, and measure pipeline and `ask_hermes` latency warm versus the plain API server.
- Run the Agent Mode physical and live gates on the reference Reachy: the roadmap's section 7 demonstration, Phase 3 camera and motion races on both units, and Agent 0.5 cancellation and restart checks.
- Run EU-provider acceptance with live keys: `python tools/llm_provider_check.py --photo desk.jpg` once with Cortecs (start with `mistral-small-3.2-24b-instruct-2506`) and once with LLMrouter.eu, both with `OPENAI_API_KEY` set for Kids moderation. Record the working model names per router in `companion/README.md`. Keyless checks are done: endpoints, Cortecs `eu_native` and `json_schema` support per its API docs, and how each router reports a refused key.
- Finish live MCP acceptance. Claude Code, the TypeScript and Python SDKs (bearer and OAuth) and `mcp-remote` (OAuth) are verified against a live server with a stand-in runtime; see `docs/agent-access-mcp.md`. Still open:
  - run `tools/mcp_check.py --say … --emotion happy` against the real robot in Awake, Standby, Sleep and privacy mode;
  - connect Hermes Agent and OpenClaw with their own model keys;
  - connect ChatGPT dots and Grok Bot through an HTTPS tunnel with OAuth sign-in, and record which vendors support dynamic client registration.
- Run OpenClaw acceptance against a live Gateway: a dedicated tool-restricted `reachy` agent, pipeline and Realtime (`ask_openclaw`) conversations, session continuity, and the Hermes + OpenClaw side-by-side setup.

### Hardware and acoustics

- Run the two-button GPIO hardware acceptance in `OPERATIONS.md` on the Reachy Pi.
- Pair a DualShock controller with the Reachy Mini Wireless and verify the mapping.
- Verify **Hey Homebody** with live microphone input (so far checked only against the KWS vocabulary).
- Add phrase-specific real-room acoustic acceptance recordings and tune per-keyword score/threshold only from measured false-positive/false-negative data.
- Run first-light acceptance on each 🧪/◐ setup in `docs/hardware-setups.md`: Reachy SDK smoke test and app start on a Jetson Orin Nano (JetPack 6, uv-managed Python 3.11+), then local vision with Ollama and TensorRT gesture detection; app acceptance on a Mac and a Windows PC driving a Lite.
