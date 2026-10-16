# Everyday capability expansion

Source baseline: origin/main 5b77d9b (includes immutable 0.5.21 and subsequent 0.5.22 fixes). Source-only implementation; no publication, deployment, media activation, speech or motion authorization.

## Deliverables
1. Host tools: bounded timers/reminders with inspect/list/cancel and honest lifecycle/delivery, bounded public information lookup with citations, allowlisted light brightness and explicitly allowlisted scenes. Preserve existing HA permissions and no unrestricted Hermes fallback.
2. Consistent local tools across Pipeline/Realtime/Live: power, one-shot vision, temporary camera session start/stop, speaker volume/mute/status, robot status and semantic head/emotion/dance. One common local schema/dispatch contract; never raw motors or persistent camera-setting writes. Temporary camera requires separate owner opt-in and expires; no tracking or ongoing cloud capture implied.
3. Compact Agent Everyday UI: brief capability summary, one grouped Manage permissions disclosure, existing weather/provider/entity detail collapsed. Reuse settings/permission controls, distinguish permission from activity and backend readiness, fail closed on owner/Kids/offline/stale responses, readable phone/desktop with 48px targets.
4. Integrated tests: real transport loops with fake providers/hardware; positive and refusal cases, cancellation and replay, timer lifecycle, exact HA readback, SSRF/privacy/source limits, browser ownership/staleness/overflow. Build an unpublished artifact and verify resource mapping; report implemented, test evidence and physical/deployment gates separately.

## Cross-component contract
Local tools use canonical specs in companion/everyday_manifest.py (stdlib-only; robot source imports companion, packaged robot imports homebody.bridge). Names: get_reachy_status, set_reachy_power_mode, capture_reachy_camera, start_reachy_camera, stop_reachy_camera, get_reachy_volume, set_reachy_volume, move_reachy_head, express_reachy_emotion, dance_reachy. start duration_seconds integer 5..120; power uses existing modes/duration_minutes; set volume has volume integer 0..100 OR muted boolean (host/runtime validate exactly one); capture purpose string. Runtime advertises only currently owner-allowed tools. Camera stop/status always available within adult Everyday, start requires everyday_camera_enabled, capture requires camera_enabled OR active temporary camera consent plus everyday_camera_enabled.

Pipeline uses authenticated /v1/everyday/session websocket: client sends everyday.start {text, language, session_id, agent_context, local_tools:[names]}; server sends everyday.tool_call {call_id,name,arguments}; client replies everyday.tool_result {call_id,result}; terminal everyday.answer {text}, or everyday.error. IDs one-shot, tool/result bounds and original context/currentness validated. Existing /v1/everyday/turn remains backward-compatible and has no robot callback.
Live uses same bridge-local tool_call/result messages on its existing socket; never forwards those messages upstream. Results use original request/session fence. Realtime adds the same local specs and uses existing completed function-call/result protocol.
Host EverydayTools.answer accepts local_tools iterable of specs and async local_executor(name,args), default none. All tools still go through bounded turn validation and budgets. Host core capabilities expose grouped readiness for household/time/weather/timers/public lookup; robot /api/everyday/capabilities augments with local permissions/activity without hardware writes.

## Verification approach
Implement focused tests first, run relevant existing suites in isolated HERMES_HOME; use complete project release gate after integration if resources permit. Independent spec then quality/safety review before claiming readiness; no expanding task board or repeated full gates per component.

## Implementation and verification result

Source implementation is complete across Pipeline, Realtime and Live. The Agent page uses a compact summary and a single grouped **Manage permissions** disclosure, with provider/device details nested rather than a flat expanded tool list.

- Final frozen-tree unit gate: **2,479 passed, 0 failed/errors, 1 skipped**, with two warnings. The skip is an optional local-only probe script that is not shipped, not an Everyday feature. The warnings are an aiohttp test subclass deprecation and the expected busy-port fixture's listener thread exit.
- Full browser gate: **327 passed**, reconciled with fresh collected case identities; covers phone/desktop, enlarged text, grouped permissions, owner/privacy loss and stale replies.
- Real robot-client → authenticated host WebSocket → model loop → runtime dispatch integration: **11 passed**, using fake hardware/providers and synthetic JPEGs through the adult vision endpoint; included in the unit gate, not additive coverage.
- Camera-specific final regression batch: **39 passed**, including actual Owner/CSRF settings disable/re-enable with RAM-only saving and held Start/Look across camera Stop. The independent final review approved the exact two camera race fixes after the earlier spec review passed.
- Ruff, `git diff --check` and development wheel build pass. The wheel retains the baseline package version and is an unpublished development artifact, not an updater-ready release. Setuptools emits package-data/discovery warnings; package/source mapping tests pass.

Camera lease commitment revalidates camera generation inside its lock; visual admission carries the camera-specific fence. Disabling general camera permission invalidates pending captures even while on-request permission remains enabled, so later re-enable cannot revive old authority. Temporary camera sessions describe app capture authority, not physical shared-daemon camera power.

Timers/reminders are RAM-only and adult-device-scoped: they survive ordinary wake/engine changes but clear on host restart. Delivery remains unconfirmed unless verified; public lookup and reminder delivery still require their configured host providers. Existing home allowlists, Kids/privacy controls and Workspace separation remain intact.

No publication, deployment, running-settings change, real provider visual request, speech, motion or camera activation was performed. Supervised robot/provider acceptance is separate and remains outstanding.
