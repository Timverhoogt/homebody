# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project currently remains in early alpha and has not published a stable compatibility promise.

## [Unreleased]

### Added

- Opt-in local HaGRID ONNX gesture pipeline with pinned Apache-2.0 model checksums, 3 FPS in-memory inference, repeated-frame confirmation, edge triggering, cooldowns, truthful HA telemetry, and no-auto-wake/Kids/privacy/action-ownership gates. Palm produces a welcome, peace an excited response, and rock one short dance.
- Home Assistant `Awake` switch gated by the explicit robot-controls opt-in; On uses the serialized verified wake transition, while Off folds safely and releases torque through Standby.
- Agent 0.1 read-only Reachy Agent Broker with eight typed capabilities for live manifest/status, allowlisted Home Assistant state, current public information/pages, personal context, conversation history, and scoped notes.
- Authenticated execute/ask/cancel/activity bridge routes, typed Reachy client contracts, evidence/freshness metadata, strict private-intent and session-generation checks, bounded redaction, and a sanitized PWA activity timeline.
- Traversal/symlink-safe scoped reads, public-page SSRF/redirect/size protections, empty-by-default entity/root allowlists, and cancellation that prevents stale results from reaching speech.
- Agent owner actions for verified reversible Home Assistant control and undo, authenticated timer/reminder delivery, approval-gated media, calendar reads/drafts/creates, single-recipient message drafts/sends, and symlink/hardlink-safe note drafts/appends.
- Agent 0.5 generation-bound multi-step runs with exact 1–5 step previews, five-call/two-side-effect/120-second budgets, per-step evidence and status, phone heartbeat, approval pauses, safe read-only pause/resume, authoritative cancellation, restart-fail-closed in-memory checkpoints, and no automatic side-effect replay.
- Trusted-phone Agent 0.5 controls for goal planning, exact argument review, Start, Pause, Resume, per-step approval, Cancel, live budget use, and final verified/failed/uncertain summaries. Voice multi-tool requests now stage the same preview instead of executing hidden chains.
- Trusted-phone exact approval sheet with five-minute device/session-scoped drafts, one-shot execution, edit/replay rejection, and Kids/privacy/generation invalidation.
- BlueZ-backed Bluetooth discovery, pairing, trust, connect, disconnect, and forget controls in the trusted Robot tab.
- Opt-in Linux joystick monitoring restricted to Sony-vendor DualShock 4 and DualSense identities with the validated PlayStation mapping; other layouts fail closed.
- Safe gamepad mapping for bounded look, center, Happy, Surprised, and cooperative Stop actions.
- Integrated Kids Mode I Spy with explicit caregiver camera consent, a visible bounded three-frame search, strict stable-target validation, deterministic hint/guess/reveal state, authoritative Stop/expiry cancellation, and bridge-session deletion.
- Bluetooth/controller operational guidance, service-account permissions, explicit Reachy Mini Wireless-only hardware scope, security boundaries, and hardware-free regression tests.
- Optional camera-feed thumb joystick with separate off-by-default opt-in, left/right placement, dead zone, spring-return visuals, keyboard support, in-overlay Stop, explicit head/base Center, native and fallback fullscreen handling, and mobile safe-area layout.
- Gesture-bound camera-control API sessions with random identifiers, monotonic anti-replay sequences, bounded finite pan/tilt, cancellable head interpolation, server-owned base assistance, release-to-hold, settings/power/privacy/Kids revocation, and generic-control ownership exclusion.
- Optional ESPHome-native Home Assistant bridge on TCP 6053 with the existing stable Reachy device/entity identity, mDNS discovery, real telemetry, truthful unavailable states, and independently gated controls and camera snapshots.
- Opt-in Home Assistant Assist ownership after local wake detection, including 16 kHz PCM streaming, HA pipeline state/motion cues, bounded same-peer TTS/media playback, announcements, follow-up support, and privacy/disconnect/timeout cancellation.

### Changed

- Agent single-request UX now keeps the sanitized broker timeline pollable during an active voice request, shows the running capability or exact approval wait in the trusted UI, and prompts for concise natural speech without reading internal capability IDs aloud.
- Agent 0.6 Goal 1 introduces disabled-by-default Proactive Presence: authenticated, identity-free local presence signals; ephemeral state; existing voice/gesture attention observations; and a cancellable silent head acknowledgement that never wakes Reachy or enables motors.
- The Hermes companion can now poll one explicitly configured Home Assistant occupancy entity and forward only changed `on`/`off` state to Reachy; Home Assistant credentials remain on the Hermes host.
- Agent 0.6 Goal 2 adds a deterministic, disabled-by-default Initiative Policy with Quiet/Balanced/Engaged modes, local quiet hours, rolling hourly and calendar-day budgets, topic cooldowns, duplicate suppression, exponential dismissal backoff, and phone-visible decisions. Goal 2 cannot produce speech.
- Agent 0.6 Goal 3 adds disabled-by-default contextual offers from a hard allowlist of structured local context sources. Eligible candidates can speak one concise question and accept one bounded English/Dutch voice or trusted-phone yes/no response; Yes only reads prepared help and never executes an action.
- Agent 0.6 Goal 4 adds an explicit, disabled-by-default shared-physical-context window. While safely Awake, the trusted phone can start 5–30 seconds of local central-frame grayscale change/stability detection; JPEGs are discarded immediately, no semantic or identity analysis runs, and a stable intentional presentation can only produce the existing bounded Goal 3 offer. Cloud vision remains restricted to a later explicit wake-and-look request through the existing single-frame path.
- Presence motion is suppressed by Standby, Meeting/Sleep, privacy, Kids Mode, active voice/announcement/camera/face-tracking/robot owners, runtime transitions, and a bounded cooldown; the trusted Agent workspace shows only sanitized state and outcomes.
- PWA shell advanced to v41 for the compact Proactive Presence controls and status.
- PWA shell advanced to v42 for compact Initiative Policy controls, limits, and explainable status inside the Agent workspace.
- PWA shell advanced to v43 after the Goal 2 safety audit: runtime/audio faults and active voice ownership now suppress initiative, policy fields resist status-poll overwrites while editing, and Presence/Initiative controls fail closed while offline.
- PWA shell advanced to v44 for contextual-offer opt-in, sanitized explanation/state, response-window control, and single-use phone Yes/No controls.
- PWA shell advanced to v45 for the visible, time-bounded Goal 4 presentation gate, local-only status, explicit Start/Stop, and privacy boundary.
- PWA navigation and information architecture were redesigned to keep Home glanceable, give Agent Mode a dedicated workspace, move optional robot controls behind progressive disclosure, and provide a compact mobile bottom navigation.
- PWA shell advanced to v40 for the streamlined control surface.
- OpenAI tool declarations use broker-validated non-strict decoding for intentionally optional arguments; strict structured answer decoding and all broker schema/policy validation remain enforced.
- PWA shell advanced to v39 for the Agent 0.5 run planner and progress UI.
- PWA shell advanced to v37 for the documented Home Assistant Awake control.
- Home Assistant camera snapshots are normalized to metadata-free baseline 4:2:0 JPEGs before ESPHome transport, preventing intermittent green/magenta rendering on older Android/WebView hardware decoders while leaving native and AI camera frames unchanged.
- Kids Mode no longer requires setup, entry, lockout, or unlock of a parent PIN. Start is direct from the trusted local UI; the child-only dashboard remains active for the session, and Stop immediately restores management controls.
- PWA shell advanced to v36 for the PIN-free Kids Mode controls.
- Kids I Spy now retains five bounded viewpoints across a 240° base arc, uses non-capturing 60° transit waypoints, revokes camera access before its neutral return, and keeps the head aligned with the base.
- Manual base control now uses separate 5°/15°/30°/60° steps, clear-space confirmation for wide turns, coupled head yaw, and a ±120° application limit inside the SDK's ±160° range.
- Camera joystick motion now follows a Pollen-inspired 20 Hz smoothed target stream: horizontal input rotates the head and base together, vertical input tilts the head, and a short watchdog stops abandoned browser gestures.
- PWA shell advanced to v35 for the Home Assistant bridge settings and live connection status.
- Fullscreen Exit uses explicit horizontal and vertical centering, and the PWA shell advances to v34 for the new controls.
- PWA shell advanced to v33 for the pinned joystick geometry and stable status layout.
- PWA shell advanced to v32 for the camera-control overlay and fullscreen-safe controls.
- PWA shell advanced to v31 for the wider base controls and five-frame I Spy status.
- Agent profile `ask_hermes` now uses only the fixed bounded T0–T3 broker surface; Realtime still advertises a single delegation tool and ordinary social conversation remains direct.
- PWA shell advanced to v30 for visible Kids I Spy camera-search state and Stop access during startup.
- PWA shell advanced to v29 for exact Agent action review and approval.
- PWA shell advanced to v27 for the Agent 0.1 timeline and Kids-mode card hiding.
- PWA shell advanced to v21 for the reviewed Bluetooth controller UI.

### Security

- Changing the bridge URL or API key from the unauthenticated settings UI now requires the current key. Before this, a LAN client could swap in its own key (unlocking the camera snapshot route) or point the bridge URL at a host it controls and receive the real key. The settings form has a new Current API key field.
- The companion bridge now has its own Kids latch. While a Kids session is live it refuses adult chat, Realtime, and Agent routes with 423 and moderates generic speech, instead of relying only on the Reachy client. Reachy reports session start and end through the new `/v1/kids/session` route. Realtime agent tools now default to off unless the client explicitly requests them.

### Fixed

- A stopped voice turn can no longer be resumed by a later Awake transition. Before this, preparing an I Spy round cleared the stop request, so an adult or Agent reply that was already in flight could be spoken during Kids Mode.
- One lock order (motor transition, then Kids) is now enforced. Manual, precision, camera-control, and presence paths nested the locks the other way round and could deadlock against `status()`. The HTTP Kids-lock middleware no longer waits on the Kids lock during slow robot transitions.
- Concurrent approvals of the same pending Agent draft now execute it exactly once.
- An abandoned camera-joystick session no longer blocks other features. Before this, closing the tab mid-drag left the session marked active until the next power change or Stop, which blocked presence, gestures, presentation and manual and precision controls. Every check that asks "is camera control active?" now expires a session idle for more than 30 seconds and stops its stream.
- PWA shell advanced to v46 for the settings credential field.
- `tests/test_kids_mode.py::test_runtime_generated_kids_session_id_passes_real_bridge_handler` no longer depends on `OPENAI_API_KEY` being set on the machine.

### Refactored

- The six duplicated runtime safety checks now live in `reachy_mini_hermes/safety_gate.py` as named rules and ordered, unit-tested policy tables. The six checks are presentation, presence, gesture, robot action, camera control and camera capture. Behaviour, reason strings and precedence are unchanged. Each check reads state under its existing lock and short-circuits, so evaluation adds no new lock nesting.

- Kids Mode moved out of `runtime.py` into `reachy_mini_hermes/kids_runtime.py` as `KidsModeMixin`. This covers the start/stop lifecycle, timers, I Spy rounds, the Kids voice policy, bridge session reporting and moderated Kids speech streaming. The 17 methods moved unchanged. Kids bridge calls create their client through `HermesVoiceRuntime._new_bridge_client`, so tests that patch `reachy_mini_hermes.runtime.HermesBridgeClient` still reach Kids paths. `runtime.py` shrinks by about 560 lines.

- Announcements moved out of `runtime.py` into `reachy_mini_hermes/announcements.py` as `AnnouncementsMixin`, together with the `Announcement` dataclass and its limits. This covers the queue, cancellation, worker and playback. The 9 methods moved unchanged and create bridge clients through `_new_bridge_client`. `runtime.py` still re-exports `Announcement`, and the shared wake prompt now lives in `wakeword.WAKE_PROMPT`. `runtime.py` shrinks by about another 320 lines.

- Owner robot controls moved out of `runtime.py` into `reachy_mini_hermes/manual_control.py` as `ManualControlMixin`. This covers manual and precision actions, live pose, Stop, and the whole camera-joystick session lifecycle, including the idle expiry. The 13 methods moved unchanged. `runtime.py` is now about 2,880 lines, down from about 4,090 before the split began.
- Proactive behaviour moved out of `runtime.py` into `reachy_mini_hermes/proactive.py` as `ProactiveMixin`. This covers presence acknowledgement, initiative evaluation, contextual offers and their yes/no capture, and the presentation window. The 17 methods and their state moved unchanged. `runtime.py` re-exports `Announcement` explicitly so linting can't remove it.
- Agent session handling moved out of `runtime.py` into `reachy_mini_hermes/agent_session.py` as `AgentSessionMixin`. This covers the capability profile, generation, request lease, broker context, bridge session publication and activity log. The 12 methods and their state moved unchanged. Bridge clients are created through `_new_bridge_client`.
- Vision moved out of `runtime.py` into `reachy_mini_hermes/vision_runtime.py` as `VisionMixin`. This covers camera capture, the gesture worker, face tracking, voice direction-of-arrival and `doa_yaw_degrees`. The 11 methods and their state moved unchanged, and `runtime.py` re-exports `doa_yaw_degrees`. The `HermesVoiceRuntime` base classes are now listed one per line.
- The three voice front-ends moved out of `runtime.py`: Home Assistant Assist and media went to `voice_ha.py`, Realtime conversations went to `voice_realtime.py`, and the STT → Hermes → TTS pipeline went to `voice_pipeline.py`. The 12 methods moved unchanged, and the Realtime event parsers and dataclasses moved with them. `runtime.py` re-exports those helpers, power modes are now shared from `safety_gate.POWER_MODES`, and Realtime sessions are created through `_new_realtime_session`. `runtime.py` is now about 980 lines, down from about 4,090.
- Power handling moved out of `runtime.py` into `reachy_mini_hermes/power.py` as `PowerMixin`. This covers Standby, Awake, Meeting and Sleep transitions, the safe fold before torque release, and the confirmed wake. The 8 methods and their state moved unchanged. Tests now patch `httpx` on the module that makes each daemon call. `runtime.py` is now about 700 lines and keeps only the orchestrator role: setup, status, the run loop, the wake loop and shared audio I/O.
- The Home Assistant ESPHome server copes better with bad input. One failing state read no longer stops state publishing for every client. Shutdown still closes the sockets and server after the publisher has crashed. A peer that declares an inbound packet over 1 MiB is disconnected instead of buffered without limit, and a malformed length now goes through the connection's error handling.
- The Realtime client is more robust. It keeps a reverse-proxy path prefix in the bridge URL, skips malformed or non-object messages instead of ending the session, and under backpressure drops only audio deltas, never control, tool-call or error events. `clear_output` raises `RealtimeBridgeError` instead of a raw socket exception.
- Six runtime robustness fixes. A Realtime Agent request no longer leaves its lease stuck when session setup fails, and it reports its real outcome. A failed voice turn stops suppressing presence, presentation and offers 60 seconds after Reachy is back at wake. Home Assistant media waits at most 30 seconds for the voice slot instead of piling up threads. A failed startup now tears down the actions thread, media and workers. An empty STT result is a normal no-speech turn instead of an error. Stop now silences pipeline TTS file playback as well as streamed audio.
- Settings and config writes no longer overwrite each other. Every read-modify-write (settings, Agent profile, Kids start, gamepad and Home Assistant switches) runs inside `config_transaction()`. Kids start now re-reads the config instead of saving one loaded before its bridge health check. Settings strings have length limits, and a failed save returns a clear 500 instead of a bare error.
- Companion bridge hardening, in seven parts:
  1. `/v1/agent/approve` executes only the exact staged draft, claimed once.
  2. STT accepts one audio file and bounded option fields, and leaves no temp files behind.
  3. Home Assistant actions are validated and allowlisted before any authenticated state read.
  4. The Realtime model is URL-encoded.
  5. Timer and reminder tasks are capped at 32, are cancelled on shutdown, and log delivery failures.
  6. JSON routes refuse bodies over 1 MiB.
  7. Upstream error details are no longer echoed to the robot.
- Dashboard and PWA reliability fixes, in five parts:
  1. The service worker no longer lists the same app shell twice, never caches error responses, and the page and worker shell versions are now tested to agree.
  2. Status polling backs off to 15 seconds while Reachy is unreachable, pauses while the page is hidden, and times out after 5 seconds.
  3. An Agent run survives a network blip.
  4. Stop app and Shut down report failures instead of claiming success.
  5. Kids-locked tabs can't be reached with the keyboard or the URL #hash, and countdowns no longer re-announce to screen readers every 1.5 seconds.
  PWA shell advanced to v47.
- Agent 0.6.5 personal adaptation core. A transparent preference ledger learns only coarse per-category signals: welcomed, dismissed, snoozed and disabled, decaying with a two-week half-life. It adapts timing only: the topic cooldown is scaled between 0.5× and 4×, snoozes are honoured, and Reachy stays quiet in a part of the day where you often declined. Confidence thresholds, budgets, permissions and risk tiers are unchanged. "Not now" and "later" now snooze a category instead of declining it. Initiative status includes a plain-language explanation of the latest decision and every learned preference. Preferences persist as small counters next to the app config.
- Agent 0.6.5 owner controls. The initiative card shows "Why did Reachy do that?" and has a Later button next to Yes and No. A "What Reachy learned" panel lists every category with its state, an explanation, an Allowed switch and a Forget button, plus "Forget everything". The new routes are adult-UI only (`/api/initiative/preferences` and `/api/initiative/preferences/reset`). PWA shell advanced to v48.
- Physical GPIO buttons (off by default): red short press stops voice, Agent work, Kids Mode, announcements, offers and movement (always honoured, also while starting or Kids-locked); red long press stops then sleeps; green short press wakes from Sleep/Meeting or starts a turn like the wake word; green long press enters Standby. libgpiod v2 with pull-ups, kernel plus software debounce, startup ownership, stuck-button lockout, and fail-safe status when the library, permissions or lines are unavailable.
- Robot tab: Physical buttons card shows GPIO button status, pins and last press, and lets the owner set the green/red pins, hold time and on/off without a restart (GET /api/gpio/status, POST /api/gpio/buttons; parent-locked during Kids Mode). PWA shell v49.
- Host portability: the app now installs on macOS and Windows (evdev is Linux-only), detects whether a Raspberry Pi, NVIDIA Jetson, Linux PC, Mac or Windows PC drives Reachy, reports it in /api/status, and shows only the hardware it can drive (GPIO buttons, Bluetooth controller). Remote power-off is limited to dedicated Pi/Jetson hosts. GPIO accepts chip line offsets up to 1023 for Jetson headers. Home Assistant identity no longer collides on hosts without /etc/machine-id. PWA shell v50.

### Build

- The CI example is now an active GitHub Actions workflow with a complete dependency set. The old list was missing `aioesphomeapi`, `onnxruntime`, `sherpa-onnx` and others, so 18 test modules failed to import. The workflow runs ruff, JavaScript syntax checks, the full suite on Python 3.11 and 3.12, and the package build.

### Verified

- Ruff, Python compilation, JavaScript syntax, and all 437 automated tests pass.

## [0.2.0] - 2026-07-18

### Added

- Dual conversation modes: configurable Hermes pipeline and OpenAI `gpt-realtime-2.1` speech-to-speech.
- Authenticated GA Realtime WebSocket proxy on the companion bridge.
- Streaming PCM audio, semantic VAD, transcript events, selectable Marin/Cedar voices, and configurable reasoning effort.
- `ask_hermes` function delegation for persistent memory, current information, Home Assistant, files, and consequential actions.
- ElevenLabs Scribe and TTS provider/model selection with account voice discovery.
- Pipeline interruption by repeating **“Hey Hermes”** during playback.
- Additional local **“Okay Nabu”** and **“Hey Reachy”** wake phrases, available for initial wake and pipeline playback interruption.
- Realtime natural interruption and streamed-output flushing.
- Standby, Awake, timed Meeting, Sleep, app-off, and confirmed Pi shutdown controls.
- Motor torque and microphone lifecycle management for privacy/power states.
- Tabbed settings UI with Dashboard, Kids, Announce, Robot, and Settings workspaces for clearer desktop, mobile, and Reachy Control use.
- Supervised Kids Mode with five activity profiles, 4–12 age bands, English/Dutch speech, 15–60 minute monotonic server sessions, salted `scrypt` parent-PIN controls, status/transcript redaction, automatic safe folding, optional gentle voice-state motion, and a dedicated moderated child pipeline with no camera, normal Hermes memory, files, messaging, devices, purchases, power tools, or explicit robot actions.
- Kids-only ElevenLabs Flash v2.5 low-latency speech streaming with fixed 24 kHz PCM, private bridge credentials, immediate chunk playback, and configured-TTS fallback.
- Full announcement console with exact-text TTS, provider/model/voice overrides, quick templates, repeat/pause controls, a bounded serialized queue, independent Stop/clear, session-scoped browser draft preservation, and voice-only, wake-and-return, or stay-awake behavior.
- Manual semantic robot controls now include live Cartesian pose readout and bounded 1/2.5/5/10-unit precision steps for X/Y/Z translation, head roll/pitch/yaw, rotating-base yaw, and independent head/base/all centering, alongside confirmed motor/fold state, safe wake/fold power actions, nine-way head direction, curated expressions, dances, and cooperative movement cancellation.
- Priority Stop behavior, privacy revalidation at execution time, busy-request rejection, persistent action state, and mobile-safe controls prevent delayed or post-privacy motion. Precision motion uses app-owned 50 Hz interpolation so Stop/Meeting/Sleep can cancel both head and base movement; folding now waits for action-worker idle and re-verifies the physical sleep pose before torque release.
- Installable Android PWA metadata, branded icons, a root-scoped service worker, Dashboard install UX, and an HTTP Add-to-Home-Screen fallback.
- Realtime client, silence playback asset, and tests for Realtime audio and power-state behavior.
- Optional on-demand Reachy camera tool with local diagnostics and Realtime image input.
- Authenticated, non-cacheable one-frame snapshot route for explicit image sharing.
- Opt-in Robot-tab live viewer for the daemon's existing local WebRTC camera feed, with explicit Awake-only policy, muted audio, no public STUN dependency, and automatic disconnect on privacy/background transitions.
- Privacy-controlled daemon-local face following that runs only during an active post-wake conversation.
- Optional wake-time DOA orientation using Reachy's local microphone-array direction estimate.
- Curated Realtime robot tools for look direction, authentic recorded emotions, and three recorded dance styles.
- Local Realtime power-mode tool for explicit Standby, Awake, timed Meeting, and Sleep commands.
- Native Reachy `goto_sleep()` transition before Sleep releases torque, with fail-safe torque retention if the movement fails.
- Pose-aware safe folding before every Standby/Meeting/startup torque release, preventing a head drop when the app is restarted while Reachy is upright.
- Serialized action worker that yields face/voice motion during explicit moves and cancels actions on Meeting/Sleep.
- Operations runbook covering deployment, rollback, cooling maintenance, health checks, and acoustic acceptance.

### Changed

- Reachy starts in Standby with motor torque disabled while local wake processing remains active.
- Provider secrets remain on the Hermes host; Reachy receives only a private bridge bearer token.
- Companion health output reports Realtime, moderated Kids chat, and Kids Flash streaming availability.
- Voice status exposes power mode, Meeting timer, provider state, and interruption count.
- Documentation now describes the dual-mode architecture and security boundaries.
- The local camera viewer selects `ws://` on direct LAN HTTP and `wss://` on trusted HTTPS deployments, allowing Tailscale Serve to secure both the PWA and WebRTC signaling.
- The Robot tab now groups confirmed torque/fold state with Wake, safe Fold, and Stop controls; adds bounded diagonal looks, descriptive expression presets, dance-footprint labels, and clear-space confirmation for wide motion.
- Power and wake/fold transitions are serialized across clients; daemon, wake-motion, and torque-release failures now return explicit errors, keep the last confirmed motor state visible, and prevent false-success UI messages or post-release action execution.
- Hugging Face app page now presents wake phrases, supervised Kids Mode, voice, interruption, camera, Hermes-tool, and power/privacy architecture.

### Fixed

- Updated OpenAI integration from the retired beta Realtime API shape to the GA protocol.
- Added the required Realtime output sample rate.
- App-off no longer waits on the daemon response from inside the process being stopped, removing a ten-second shutdown cycle and traceback.
- Realtime interruption tracks locally buffered audio after server generation finishes, immediately flushes Reachy playback, and truncates the unplayed OpenAI conversation audio.
- Camera access defaults to off and captures only one fresh JPEG per explicit Realtime visual-tool call.
- Camera capture waits for a completed tool item, deduplicates call IDs, and remains blocked in Meeting/Sleep; authenticated snapshots and local diagnostics now enforce and recheck the same privacy boundary while waiting for a frame.
- The local viewer closes both media sessions and signaling sockets, accepts only Reachy's named camera producer, and fails closed when runtime status disappears or the voice app stops.
- Awake now runs Reachy's physical wake-up motion instead of only enabling motor torque.
- Meeting and Sleep stop active playback and microphone capture before disabling motors.
- Meeting/Sleep action cancellation now restarts Reachy's playback backend when returning to Standby/Awake.
- Motion cancellation uses generation checks so dequeued actions cannot start after a privacy transition.
- Wake activation rechecks privacy before motors, face tracking, and cloud conversation startup.
- Pipeline synthesis and playback recheck privacy before starting TTS audio.
- `ask_hermes` now executes only for completed, deduplicated Realtime function-call items.
- Robot function-call output now reports the actual physical execution result instead of queue acceptance.
- DOA orientation accepts only a recent speech-validated, finite microphone-array estimate.
- Realtime barge-in now tracks only assistant audio-message item IDs, so function calls cannot make `conversation.item.truncate` target a non-audio item and terminate the session.
- A rejected truncation remains non-fatal after the local playback queue has already been cleared.

### Verified

- Ruff passes and 134 automated tests pass.
- Reachy's app assistant passes the repository structure and metadata checks. Its isolated-install phase remains host-blocked by the upstream `PyGObject`/Cairo build dependency; the complete suite passes in the Reachy SDK 1.9 validation environment.
- Realtime session creation, audio response, configurable reasoning, and Hermes tool delegation succeed against the live API.
- ElevenLabs TTS/STT round trip succeeds; Kids Flash streaming reaches Reachy as 24 kHz PCM with a measured 375 ms first chunk on the reference network.
- Automated checks cover moderated child chat, lockout, status redaction, timer generation guards, stream cancellation, and fold-outcome reporting; the earlier reference hardware run verified parent stop, safe fold, torque release, and Standby.
- `Okay Nabu` and `Hey Reachy` detect in synthesized acceptance audio; `Hey Hermes` remains verified with live microphone input.
- Reachy power states, clean app stop/restart, API soak tests, motor mode, and daemon health pass on the reference Reachy Mini Lite deployment.
