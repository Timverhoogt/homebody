# Holistic Companion — implementation record (development branch, unpublished)

**Status:** implemented and unit/browser-tested in the isolated worktree on top of `09a3957` (descendant of deployed 0.5.19 `3878f6a`), then corrected after the parent's independent review (`tests/test_companion_review.py`: 16/16 passing). Nothing here is deployed, published, enrolled on a real account, or physically accepted. Canonical version identity (`0.5.21`) and package dependencies are unchanged; the built wheel is a development artifact only.

Design: `docs/companion-design.md`. Acceptance contract: `docs/plans/2026-10-16-holistic-companion.md`.

## Entry points (what actually runs)

| Entry | Path | Admission and fences |
|---|---|---|
| Phone **Look** | `POST /api/companion/look` → `CompanionMixin.look_at_this(origin="phone", authority=<owner device>)` | Owner pairing + CSRF (real middleware), Kids lock → 423. The route binds the exact admitting owner device (`OwnerStore.device_active`) into the ticket and re-checks it after the look. |
| Phone follow-up | `POST /api/companion/look/follow-up` → `follow_up_look` → bridge `POST /v1/everyday/look/follow-up` (tool-free) | Same as above; no capture; expired/replaced context → "look first". |
| Pipeline voice (Everyday) | `voice_pipeline.py`: `looks_like_look_request(transcript, engaged=look_context_live())` → `voice_look_turn` → `look_at_this(origin="voice")`; follow-ups while a look is live → `voice_look_follow_up` (tool-free route) | `origin="voice"` is honoured only while the voice slot is actually held by the turn; Stop/privacy cancel silently. Workspace profile keeps its own turn path (no look there). |
| Live voice | Host relay `companion/live_transport.py`: delegated user words classified by the same narrow classifier → `bridge.look_request` to the robot → `voice_live._start_live_look` runs `look_at_this(origin="voice")` off the loop → `client.look_result` (text only) → appended as the delegation result | Works in Everyday and Workspace Live sessions without a project session or engine switch; no frame or tool enters Live; late/unknown results dropped; 45 s bound. |
| Realtime voice | `voice_realtime._describe_for_realtime` → `look_at_this(origin="voice")` | The model's `capture_reachy_camera` tool call now obeys the same ticket/cancellation contract; text-only result; cancelled looks are silent. |
| Legacy presentation window | unchanged routes; **Yes** → `_schedule_presentation_help` (ticket bound at acceptance) → one `look_at_this(origin="offer")` | Cancellation (Stop/privacy/Kids/profile) is silent: no late answer and no failure speech. Detect/offer is no longer the primary experience; the Companion card is. |

### The look lifecycle (`homebody/companion.py`)

1. Ticket admitted **before** any blocking work: freezes Stop generation, capability profile, origin, optional owner authority.
2. Ownership: phone/offer looks acquire the voice slot (non-blocking; busy → refused), voice looks require it held.
3. Preflight (`camera_enabled`, Kids, `CAMERA_CAPTURE_POLICY`, camera control, bridge/local vision configured) → **fence** → capture with `is_current` passed into `_capture_camera_jpeg` (a Stop while waiting for a frame yields zero frames) → **fence before upload** (camera permission revoked during capture → frame dropped) → analysis on exactly the configured route (local vision or bridge, no fallback) → **fence after inference** → atomic context store under the context lock only if still current.
4. Teardown (`_companion_teardown`) on every `_request_conversation_stop` (Stop, privacy, Meeting/Sleep, Kids start, profile change, emergency stop) and on `companion_stop`: look context and owner-only private details leave RAM; TTL timers (120 s context, 15 min private details) are real `threading.Timer`s, cancelled on teardown.
5. Public status carries only categorical `last_failure` (`cancelled|blocked|busy|camera_unavailable|vision_unavailable|empty_answer|bridge_unavailable`), never provider bodies, questions or answers. Stale failures never overwrite a newer interaction.

### Narrow visual intent (`homebody/look_intent.py`, shared with the host by file)

Anchored EN/NL imperative/question shapes about a physical thing being shown; rejects negations, quoted speech, "explain the phrase", and non-physical targets (project, roadmap, repository, file, mail…). "And this one" counts only while a look is live. Model tool choice is never evidence of intent.

## Milestone mapping

### 1. Coherent companion
- Presence: `PresenceState.observe()` returns transitions; heartbeats refresh `source_health` without arrivals; `mark_unavailable` → occupancy `unknown`; only an arrival acknowledges; Standby never gains motors (`tests/test_companion.py`, legacy presence tests updated with reasons).
- Host bridge: changes-only forwarding kept; unchanged state re-sent as a heartbeat at most every `REACHY_PRESENCE_HEARTBEAT_SECONDS`; read failures reported once per streak as `unavailable` (`tests/test_companion_bridge.py`).
- Coordinator tickets + receipts; unsolicited offers in Everyday need **present** occupancy (`room_empty`/`occupancy_unknown` suppression); **project**-sourced offers keep Agent-profile authority; requested reminder/timer delivery bypasses initiative gates but not privacy.
- Companion card (`static/companion.js`, assets v100): truthful activity/presence/camera/mic, Look, Ask about it, Stop, Suggestions Off/Gentle/More often with "Why", Details with receipts and **source enrollment**. Page fences: action generation advanced on owner loss, Stop, offline, privacy/Meeting/Sleep, Kids and profile change; held replies admitted earlier are never rendered; private text and the draft question leave the page; `update()` cannot re-enable controls while unpaired (`tests/test_companion_browser.py`, 390/1440 screenshots in `.artifacts/browser-screens/companion/`).

### 2. Complete direct visual help
- See lifecycle above. Tests: `tests/test_companion.py` (one capture, follow-up without capture, Stop/privacy/sleep/Kids/profile during held analysis, Stop before capture admission → zero frames, categorical failure, no fallback), `tests/test_companion_review.py` (parent's held-preflight and camera-revoked-during-capture regressions), `tests/test_owner_companion_lifecycle.py` (owner revoked mid-look → 409, no adoption by a new owner, fresh positive control), `tests/test_companion_lifecycle.py` (Live relay request/result incl. Stop during held look and a non-visual positive control; robot Live loop off-loop look; tool-free follow-up with hostile OCR → zero tools), `tests/test_local_vision.py` (Realtime camera call via the fenced look).
- Tool-free follow-up: bridge `POST /v1/everyday/look/follow-up` sends system policy, a quoted untrusted DATA block and the question as separate messages, with no `tools`/`tool_choice`, never touches the Everyday tool executor, and refuses provider replies that contain tool calls. The runtime never routes follow-ups through the tool-capable Everyday turn.

### 3. Useful opt-in sources and requested delivery
- Host producers (`companion/companion_sources.py`): at most one HA `state_equals` condition and one calendar lead-time source; exact ids; accepted/offer text must be read-only help (texts promising "I will remind/turn/send…" are rejected); calendar details private by default (owner dashboard via `GET /api/companion/status`, never spoken to an anonymous Yes), `spoken` only when enrolled so; clock times in the owner's zone (`REACHY_COMPANION_TIMEZONE` or host local, never bare UTC).
- Lifecycle: hashed, bounded moment fingerprints; a moment is committed only after the robot answered `queued: true`; suppressed/failed submissions are deferred (short backoff), never lost; delivered moments never repeat even after draft expiry until the source changes; expired drafts never replay; enrollment drift (file changed) clears host memory and is re-checked right before each submission.
- Enrollment UI: `GET/POST /api/companion/sources` (owner + CSRF) proxy the host catalog (`GET/POST /v1/companion/sources`): only already-allowlisted Everyday status entities and `REACHY_COMPANION_CALENDARS` ids are offered; the host validates against its own allowlists and writes `REACHY_COMPANION_SOURCES_FILE`. Missing host capability shows precise setup-needed items, never "enabled". Nothing is enrolled automatically.
- End-to-end: `tests/test_companion_lifecycle.py::test_enrolled_source_offer_travels_from_host_to_runtime_once_and_responses_finish_help` drives the real `/api/initiative/offers` route and runtime from the bridge poller: one delivery, same moment silent, No → backoff, Yes → prepared read-only text (no action), room empty → deferred not committed, enrollment change → host memory cleared.

## Speech readiness (truthful, not repaired here)

The parent found the installed Edge TTS 7.2.7 returns 403 while an isolated official 7.3.1 works with the same Aria voice (Hermes `text_to_speech_tool`, 1.6 s, valid 24 kHz MP3). No live change was made. Recommended bridge-scoped repair: pin `edge-tts==7.3.1` in the **bridge's own** environment only (no provider/voice switch, no shared Hermes upgrade). Until that route is exercised, the Companion card reports `speech.last_announcement_failed` and "Suggestions paused: voice unavailable" after a failed announcement; a generic bridge 200 is not TTS proof. Failed TTS still cancels the offer token (unchanged).

## Component placement

- Robot (`homebody/`): `companion.py`, `look_intent.py` (new); `presence.py`, `proactive.py`, `announcements.py`, `contextual_offers.py`, `hermes_client.py`, `voice_pipeline.py`, `voice_live.py`, `live_client.py`, `voice_realtime.py`, `vision_runtime.py`, `owner_auth.py`, `runtime.py`, `main.py`; `static/companion.js` (new), `index.html`, `style.css`, `main.js`, `owner.js`, `service-worker.js`.
- Host bridge (`companion/`, packaged as `homebody/bridge/`): `companion_sources.py` (new), `live_transport.py`, `hermes_reachy_bridge.py` (loads `look_intent.py` by sibling path; fails closed if absent).

## Exact test commands and receipts

All via `python3 /home/timv/homebody-companion/.artifacts/companion-checks.py VERB ARGS`.

- Parent review red (before corrections): `unit tests/test_companion_review.py` → 12 failed / 4 passed (`.artifacts/unit-1792143188936275625.log`).
- Corrections green: `unit tests/test_owner_companion_lifecycle.py tests/test_companion_lifecycle.py tests/test_companion_review.py tests/test_companion.py tests/test_local_vision.py tests/test_owner_companion.py tests/test_companion_owner_probe.py` → 93 passed (`.artifacts/unit-1792144706746957050.{log,xml}`).
- Broad regression (33 modules incl. bridge, owner auth, pipeline STT, Everyday fast paths, Kids fences, Live/Realtime, MCP): 423 passed, 2 legacy expectations updated with reasons (`tests/test_adaptation.py` presence; `tests/test_kids_mode.py` v100) (`.artifacts/unit-1792144942678110353.{log,xml}`); re-run receipts in the handoff summary.
- Browser: `browser tests/test_companion_browser.py` — see handoff summary for the final receipt (fences for owner loss, Stop, privacy, offline, Kids, profile; enrollment panel; 390/1440).
- Lint: `lint <explicit files>` → all checks passed (`.artifacts/lint-1792145027076698757.log` and the final receipt). JS: `syntax homebody/static/companion.js homebody/static/main.js homebody/static/owner.js` → `JS_SYNTAX_OK`.
- Build: `build` → `dist/reachy_mini_homebody-0.5.21-py3-none-any.whl` (receipt in the handoff summary). Parent verifies wheel bytes against source.

## Opt-in migration and consent

Nothing turns on by upgrade. Presence, offers, initiative and camera flags keep their saved values. The card's Suggestions control writes existing settings only when the owner changes it. Enrollment requires the owner to select catalog identifiers and save; the host must have `REACHY_COMPANION_SOURCES_FILE`, `REACHY_OFFER_URL`, `HASS_URL`/`HASS_TOKEN`, Everyday allowlists and `REACHY_COMPANION_CALENDARS`. `REACHY_LOOK_VISION_URL` is optional. One look = one capture; no look-together session mode exists.

## Remaining gates (honest)

1. **Enrollment:** no real calendar/HA entity is enrolled; the host env above must be configured and the owner must save a selection. Only `state_equals` conditions exist.
2. **Provider proof:** `/v1/everyday/look`, `/v1/everyday/look/follow-up` and local vision are tested with fake transports; silent integration with the configured provider/Jetson (synthetic JPEG, no playback) is the parent's gate.
3. **TTS:** bridge-scoped `edge-tts` pin is documented, not applied; adult announcement route not exercised here.
4. **Live/Realtime on hardware:** protocol and fences are tested with loopback relay and fake sessions; real-session timing (45 s look bound, audio continuity during a look) needs supervised acceptance.
5. **Deployment/physical:** no bridge restart, robot deploy, motion, capture, microphone or audible acceptance performed.
