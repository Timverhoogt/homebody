# Reachy Mini Hermes Agent Mode Implementation Roadmap

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add an explicit adult-only Agent Mode that gives Reachy useful new capabilities through narrow, auditable tools while preserving fast Realtime conversation and deterministic privacy, Kids Mode, robot-safety, and approval boundaries.

**Architecture:** Keep GPT Realtime as the low-latency voice front end and keep its direct tool list small. Route broad capabilities through `ask_hermes` into a Reachy-specific Agent Broker on the Hermes host. The broker owns capability allowlists, argument validation, risk classification, approval, cancellation, result validation, redaction, and audit history; neither Realtime nor a general model receives unrestricted authority.

**Tech Stack:** Python 3.11, FastAPI/aiohttp, OpenAI Realtime WebSocket, Hermes API Server, Reachy Mini SDK, Pydantic, vanilla PWA JavaScript/CSS, pytest.

---

## Status and next steps (updated 2026-10-03)

Since this plan was written the app was renamed Homebody: files listed under `reachy_mini_hermes/` now live in `homebody/`. `CHANGELOG.md` records each release in detail.

| Release | Status |
|---|---|
| Phase 0 + Agent 0.1: policy, audit, approvals, read-only broker | Shipped |
| Agent 0.2: reversible home control, timers, reminders, media, undo | Shipped |
| Agent 0.3: one-shot vision and bounded gestures | Shipped |
| Agent 0.4: calendar, notes, draft-first messages | Shipped |
| Agent 0.5: multi-step runs with preview, approval, pause and cancel | Shipped |
| Agent 0.6: proactive presence (`2026-07-23-agent-06-proactive-presence.md`) | Shipped (0.6.1–0.6.5) |
| Warm Hermes agents for pipeline and `ask_hermes` (off by default) | Shipped, awaiting live acceptance |
| Phase 6 / Maintenance 0.1 | Not started |

Next steps, in order:

1. **Live acceptance of what has shipped.** Every phase above passed mocked tests and review, but the exit gates also ask for physical and live-system checks. Run them on the reference Reachy: the section 7 demonstration, the Phase 3 camera and motion races on both units, and the Agent 0.5 cancellation and restart checks.
2. **Warm Hermes agents.** Enable `REACHY_HERMES_WARM_AGENTS=1` on the Hermes host, confirm `/v1/warm-agents` reports available against the deployed Hermes release, and measure pipeline and `ask_hermes` latency warm versus the plain API server (baseline ~14 s and ~23 s).
3. **Phase 6, Maintenance 0.1.** The only unbuilt phase. Start with read-only sanitized diagnostics and the separate toggle plus admin PIN, then allowlisted service restart, then signed update and rollback, tested on a non-production install first. It must stay out of Realtime schemas and Kids Mode.

The remaining non-agent work (hardware acceptance, offline speech fallback, CI with the real SDK) is tracked in `plan.md` under Next steps.

---

## 1. Product decision

Agent Mode is an **adult capability profile**, not a third conversation engine and not a synonym for “give the Realtime model every Hermes tool.”

The app retains two transport modes:

- **Realtime:** low-latency voice front end; recommended default.
- **Pipeline:** each turn goes through the full Hermes agent.

On top of either transport, the adult selects one capability profile:

| Profile | Purpose | Authority |
|---|---|---|
| Conversation | Normal everyday interaction | Current bounded robot, camera, power, and `ask_hermes` behavior |
| Agent | Multi-tool assistance with visible progress and approvals | Expanded brokered capabilities |
| Kids | Supervised child activity | Existing dedicated no-memory/no-private-tools routes; Agent Mode impossible |

Switching into Agent Mode starts a fresh agent session and requires a trusted adult UI action. Voice alone cannot enable it.

## 2. Non-negotiable boundaries

1. Emergency stop, fold, torque release, shutdown, Meeting, Sleep, privacy, Kids Mode, and disabled robot tools always override agent work.
2. No unrestricted shell, arbitrary Python evaluation, raw motor commands, credential retrieval, arbitrary filesystem root, continuous camera monitoring, purchases, or silent outbound communication.
3. Secrets never enter model prompts, browser status payloads, audit arguments, or spoken output.
4. Kids Mode cannot inherit, resume, approve, or inspect an adult agent session.
5. Every side effect has a bounded schema, timeout, cancellation path, sanitized result, and audit event.
6. A mode/session change invalidates pending approvals and rejects late results.
7. Direct Realtime tools remain latency-sensitive and robot-local only; broad tools stay behind the Agent Broker.
8. “I did it” is spoken only after the broker positively verifies success.
9. Physical movement and camera use remain explicit, brief, and locally gated.
10. A phone/UI emergency stop cancels agent execution before any follow-up speech.

## 3. Risk and approval model

| Tier | Examples | Default policy |
|---|---|---|
| T0 — Read-only/public | Weather, web search, public facts, robot status | No approval; audit summary |
| T1 — Read-only/private | Personal memory, calendar details, scoped notes/files, camera still | Explicit user intent in current turn; privacy indicator; audit |
| T2 — Bounded local action | Lights, scenes, media playback, timers, reminders, explicitly requested bounded robot gesture/dance | Execute when clearly requested; announce result and offer undo where supported |
| T3 — Private or external side effect | Save/export a camera image, send message/email, create calendar event, modify a file | Show exact action; explicit phone/PIN approval before execution |
| T4 — Privileged/destructive | Service restart, software update, package/config changes, deletes | Separate Maintenance capability; phone + parent/admin PIN; disabled by default |
| Prohibited | Raw shell exposed to Realtime, raw motors, credentials, purchases, security bypass | Never exposed |

Approval records contain only: capability ID, sanitized arguments, risk tier, requesting session, expiry, approval method, executor result class, and timestamps.

---

## Phase 0 — Security foundation and truthful capability inventory

**Outcome:** Agent Mode has a policy boundary before any additional tool is enabled.

### Deliverables

1. Create `reachy_mini_hermes/agent_policy.py`:
   - `CapabilityId`, `RiskTier`, `CapabilityDefinition`, `PolicyContext`, and `PolicyDecision`.
   - Adult/Kids, power mode, privacy, robot state, camera permission, approval, and session-generation checks.
2. Create `reachy_mini_hermes/agent_audit.py`:
   - append-only sanitized JSONL events;
   - bounded retention and no prompt/tool-result bodies by default.
3. Create `reachy_mini_hermes/agent_approvals.py`:
   - one-time tokens bound to exact capability + canonical arguments + session generation;
   - short expiry and atomic consume.
4. Add `conversation`/`agent` capability profile to `AppConfig` in `reachy_mini_hermes/config.py`.
5. Add adult-only profile routes and strict status serialization in `reachy_mini_hermes/main.py`.
6. Add session-generation cancellation in `reachy_mini_hermes/runtime.py`.
7. Add an Agent card to the PWA with:
   - active profile;
   - enabled capabilities;
   - current task;
   - pending approval;
   - Stop Agent button;
   - recent sanitized activity.
8. Remove `terminal` and unrestricted `file` authority from the Reachy-facing Hermes agent path before expanding capabilities. Replace them with narrow Reachy-specific broker operations.
9. Keep the current general Hermes API server available to trusted non-Reachy clients only if it requires those broad toolsets; do not share that unrestricted session with voice.

### Tests

- Create `tests/test_agent_policy.py`.
- Create `tests/test_agent_approvals.py`.
- Create `tests/test_agent_routes.py`.
- Add mode-switch and cancellation cases to `tests/test_runtime_power.py` and `tests/test_kids_routes.py`.
- Assert no secret-like values or unrestricted tool names appear in status/audit payloads.

### Exit gate

- Agent profile can be entered only from an unlocked adult UI.
- Kids, Meeting, Sleep, privacy disablement, emergency stop, and session changes cancel or reject all agent work.
- No newly exposed action tool exists yet.
- Independent security review passes.

---

## Phase 1 — Read-only Agent Mode

**Outcome:** Reachy can investigate and report without changing the world.

### Capabilities

- `get_agent_capabilities`
- `get_reachy_status`
- `get_home_status` for allowlisted Home Assistant entities
- `search_current_information`
- `read_public_web_page`
- `recall_personal_context`
- `search_conversation_history`
- `read_scoped_note` from explicit allowlisted directories

### Implementation

1. Create `companion/reachy_agent_broker.py` as the Hermes-host enforcement point.
2. Add authenticated broker routes to `companion/hermes_reachy_bridge.py`.
3. Add typed broker client code to `reachy_mini_hermes/hermes_client.py`.
4. Keep one Realtime delegation tool—`ask_hermes`—rather than duplicating all schemas in the Realtime prompt.
5. Return structured evidence and freshness metadata to Hermes; generate spoken summaries only after result validation.
6. Add a read-only activity timeline to `static/index.html` and `static/main.js`.

### Exit gate

- Every capability is read-only and can be cancelled.
- Home Assistant returns only allowlisted state attributes.
- File access cannot escape configured roots through symlinks or traversal.
- Realtime simple-conversation latency is not materially increased.
- Full suite, redaction tests, and independent review pass.

---

## Phase 2 — Reversible home, reminders, and media actions

**Outcome:** Reachy becomes useful for everyday hands-free actions with low consequence and clear undo semantics.

### Capabilities

- `control_home_entity` for a configured light/switch/scene allowlist
- `set_timer`
- `create_reminder`
- `cancel_reminder`
- `play_media`
- `pause_media`
- `set_media_volume` with bounded volume
- `undo_last_reversible_action`

### Policy

- No locks, alarms, garage doors, covers, climate safety limits, or security systems initially.
- Require clear present-turn intent.
- Speak the target before ambiguous multi-device actions.
- Record prior state when an action supports undo.
- Verify resulting Home Assistant/media state before claiming success.

### Exit gate

- Each mutation has success, failure, timeout, stale-session, and undo tests.
- Entity allowlist defaults empty.
- Home Assistant unavailable/unknown state fails closed.
- Kids Mode has zero access.

---

## Phase 3 — Vision and richer embodied assistance

**Outcome:** Reachy can combine an explicitly requested camera frame with agent reasoning and safe physical context.

### Capabilities

- `inspect_current_view`
- `read_visible_text`
- `identify_visible_object`
- `point_or_look_at_direction` using bounded semantic poses
- existing emotions/dances through the same audit and cancellation model

### Policy

- One still frame per explicit request; no continuous monitoring.
- Visible camera-use indicator and fresh-frame timestamp.
- Maximum image size and bounded analysis timeout.
- No face recognition, identity matching, child monitoring, or background capture.
- Motion is semantic and bounded; no joint-angle/raw motor interface.

### Exit gate

- Camera permission is rechecked before capture and again before returning the frame.
- Privacy, Kids, Meeting, Sleep, and emergency stop win during every race condition.
- Images are not written to disk unless separately approved.
- Physical safety acceptance passes on both Reachy units.

---

## Phase 4 — Productivity and external communication

**Outcome:** Reachy can prepare and, after exact approval, perform outward-facing work.

### Capabilities

- `list_calendar_events`
- `draft_calendar_event`
- `create_calendar_event`
- `draft_message`
- `send_approved_message`
- `draft_email`
- `send_approved_email`
- `append_scoped_note`

### Policy

- Draft-first workflow.
- UI displays exact recipient, channel, subject, and final text.
- One-time approval binds to that exact content; edits invalidate approval.
- No group messages, bulk sends, attachments, contact creation, forwarding, or auto-replies in the first release.
- Never use child speech or untrusted remembered text as approval.

### Exit gate

- No external send can occur from voice confirmation alone.
- Duplicate/idempotency tests prevent repeat sends after retries.
- Recipient confusion, stale approval, and changed-text tests pass.
- Full activity record can be reviewed from the phone without exposing message bodies by default.

---

## Phase 5 — Multi-step agent runs

**Outcome:** Agent Mode can plan and execute bounded workflows while remaining observable and interruptible.

### Features

- Structured plan preview: goal, steps, tools, expected side effects.
- Foreground and bounded background runs.
- Per-step status: queued, running, waiting approval, completed, failed, cancelled.
- Global Stop Agent control.
- Time, tool-call, and side-effect budgets.
- Checkpoint and resume only when the capability profile and session generation still match.
- Partial-result reporting without pretending the whole plan succeeded.

### Constraints

- No recursive autonomous scheduling.
- No dynamic tool installation during a run.
- No widening permissions after the plan starts.
- Background work cannot retain microphone or camera access.
- A plan requiring T3/T4 actions pauses at each approval boundary.

### Exit gate

- Cancellation stops active HTTP work, queued actions, robot motion, and follow-up speech.
- Reboot/reconnect never resumes a side effect automatically.
- Run budgets and stale-result behavior have deterministic tests.
- Independent adversarial review passes.

---

## Phase 6 — Optional Maintenance Mode

**Outcome:** Trusted maintenance is possible without folding privileged authority into everyday Agent Mode.

### Capabilities

- Read sanitized diagnostics and versions.
- Restart an allowlisted service.
- Run a signed/verified application update.
- Export a redacted support bundle.
- Roll back to a known package version.

### Policy

- Separate Maintenance toggle, disabled by default.
- Phone UI plus parent/admin PIN.
- No arbitrary command strings.
- Fixed commands, fixed service names, signed artifacts, checksum verification, rollback plan, and bounded logs.
- Never available in Realtime tool schemas or Kids Mode.

### Exit gate

- Update and rollback are tested on a non-production installation first.
- Power-loss and interrupted-update recovery pass.
- No credential or unrestricted log leakage.

---

## 4. Recommended capability release order

| Release | User value | Initial tools |
|---|---|---|
| Agent 0.1 | Trustworthy visibility | Robot status, web/current info, memory, allowlisted HA states |
| Agent 0.2 | Daily hands-free utility | Lights/scenes, timers, reminders, media, undo |
| Agent 0.3 | Embodied assistance | One-shot vision, OCR/object help, bounded pointing/gestures |
| Agent 0.4 | Productivity | Calendar, notes, draft-first messages/email |
| Agent 0.5 | Real agent workflows | Multi-step plans, progress, pause/approval/cancel/resume |
| Maintenance 0.1 | Owner operations | Diagnostics, signed update, service restart, rollback |

## 5. UI concept

The Home tab gains a clearly separate **Agent Mode** card:

- Conversation / Agent segmented selector.
- “Adult tools may access private information and perform approved actions” disclosure.
- Capability switches grouped by Read, Home, Vision, Productivity, and Maintenance.
- Each group shows its risk tier and approval behavior.
- Persistent Stop Agent button.
- Current task and next pending action.
- Approval sheet with exact target and arguments.
- Recent activity with success/failure/cancelled status.

Kids Mode hides the entire Agent card and invalidates any existing adult agent session when started.

## 6. Definition of done for every capability

A capability is not shippable until it has:

- [ ] Narrow Pydantic/JSON schema and strict additional-property rejection.
- [ ] Explicit risk tier and mode allowlist.
- [ ] Positive and negative authorization tests.
- [ ] Timeout and cancellation.
- [ ] Session-generation/stale-result rejection.
- [ ] Sanitized audit event.
- [ ] Verified result before success speech.
- [ ] Kids/Meeting/Sleep/privacy/emergency-stop race tests.
- [ ] No credential exposure.
- [ ] Phone UI state and error rendering.
- [ ] PWA cache version update when static assets change.
- [ ] Full package/test/static checks.
- [ ] Independent safety/code review.
- [ ] Physical acceptance when camera, motion, audio, Bluetooth, or motors are involved.

## 7. Immediate implementation slice

*Done: Phase 0 and Agent 0.1 shipped. The demonstration below is still the live acceptance script.*

Implement **Phase 0 + Agent 0.1 only** first. Do not enable home mutations, messaging, maintenance, or multi-step background execution in the first slice.

Initial acceptance demonstration:

1. Adult enables Agent Mode from the phone.
2. User asks, “What can you do in Agent Mode?”
3. Reachy reports the live broker capability manifest.
4. User asks for current web information and a safe Home Assistant status summary.
5. User asks for a remembered preference.
6. Every operation appears in the sanitized activity timeline.
7. Starting Kids Mode immediately cancels the agent session and removes all private capabilities.
8. Stop Agent cancels an in-flight request and prevents late speech.
9. Realtime social conversation remains direct and fast.

## 8. Primary files

- Modify: `reachy_mini_hermes/config.py`
- Modify: `reachy_mini_hermes/main.py`
- Modify: `reachy_mini_hermes/runtime.py`
- Modify: `reachy_mini_hermes/hermes_client.py`
- Modify: `companion/hermes_reachy_bridge.py`
- Create: `reachy_mini_hermes/agent_policy.py`
- Create: `reachy_mini_hermes/agent_approvals.py`
- Create: `reachy_mini_hermes/agent_audit.py`
- Create: `companion/reachy_agent_broker.py`
- Modify: `reachy_mini_hermes/static/index.html`
- Modify: `reachy_mini_hermes/static/main.js`
- Modify: `reachy_mini_hermes/static/style.css`
- Modify: `reachy_mini_hermes/static/service-worker.js`
- Create: `tests/test_agent_policy.py`
- Create: `tests/test_agent_approvals.py`
- Create: `tests/test_agent_routes.py`
- Create: `tests/test_agent_bridge.py`
- Modify: `tests/test_runtime_power.py`
- Modify: `tests/test_kids_routes.py`
- Modify: `README.md`, `SECURITY.md`, `OPERATIONS.md`, and `CHANGELOG.md`

## 9. Validation commands

```bash
export GI_TYPELIB_PATH=/tmp/gst-typelibs/usr/lib/x86_64-linux-gnu/girepository-1.0
/tmp/reachy-test-venv/bin/python -m pytest -q
/tmp/reachy-test-venv/bin/ruff check .
/tmp/reachy-test-venv/bin/python -m compileall -q reachy_mini_hermes companion tests
node --check reachy_mini_hermes/static/main.js
node --check reachy_mini_hermes/static/service-worker.js
git diff --check
uv build
```

Physical and live-system gates are additional; green mocks do not replace them.
