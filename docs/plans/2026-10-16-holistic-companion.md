# Holistic Companion Implementation Plan

> **For Hermes:** implement the approved experience in this isolated branch with one coherent coding handoff and parent-owned review/verification, not a task-board programme. The user's approval covers implementation; live enrollment, public release and physical acceptance remain separate.

**Goal:** deliver coherent presence/engagement, direct visual help and genuinely connected opt-in suggestions, not just renamed settings.

**Architecture:** integrate a deterministic coordinator with the runtime's existing safety/voice/motor/camera ownership. Keep occupancy/source health, power, privacy, interaction, media consent and Workspace authority orthogonal. Reuse voice transports, visual clients, announcements, initiative policy and response tokens.

**Tech Stack:** Python 3.11/3.12, FastAPI/aiohttp, vanilla JS/CSS, pytest, Chromium. No new dependencies without demonstrated necessity.

**Baseline:** origin/main `09a3957`, descendant of deployed 0.5.19 `3878f6a`; robot remains unchanged. Read `docs/companion-design.md` for approved reviewed design. Retain recent safety, shutdown, language, STT and Workspace repairs.

## 1. Coherent companion

- Test unknown/stale vs confirmed occupancy and source freshness separately. Modify `homebody/presence.py`, presence signal schema/routes and `companion/hermes_reachy_bridge.py`: heartbeat health without manufacturing an arrival, changes-only occupancy preserved semantically, failed/unavailable states explicit. Test successful stable occupancy, bridge loss and recovery, no duplicate acknowledgement and no motor wake.
- Add compact deterministic interaction state/receipts to runtime integration (new focused module if helpful). Reuse existing follow-up conversation behavior and selected engine; do not add an independent model loop. Test Stop/privacy/Kids/power/owner invalidation, held callbacks and deliberate later positive controls. Preserve motor-before-Kids lock order.
- Integrate ordinary adult companion readiness without project binding or Agent-only authority; guard private data, media and actions independently. Never grant Workspace or shared primary Hermes tools.
- Build one practical Companion card available in Everyday, preserving Workspace picker and independent work Stop. Reuse scene/status rendering where appropriate. Show unknown/offline truthfully, camera/mic actual-use vs permission, actionable disabled reasons, direct Look and Stop, suggestions enrollment under details. Keep consent off unless saved/explicit. Advance PWA asset/cache versions together.
- TDD: add focused `test_companion*`, `test_owner_companion*`, and `test_companion_browser.py`; run red then green using the reviewed launcher. Keep legacy tests unless changed behavior is explicitly superseded by this plan.

## 2. Complete direct visual help

- Replace the owner-facing presentation ritual with a single explicit visual request, one frame, useful answer and follow-up; available without initiative enabled and in Everyday. Map the legacy presentation entry intentionally: compatibility or explicit deprecation, not silent consent changes.
- Inspect `homebody/vision_runtime.py`, voice Pipeline/Live/Realtime callbacks, `hermes_client.py`, local vision and bridge paths. Implement actual shared orchestration, not a fake answer or unconnected endpoint. UI entry and voice entry use the same admission boundary.
- One look permits one capture. No automatic background image sampling/upload; repeat needs another explicit request. Object already in view works. Follow-ups reuse bounded textual analysis/session context; if more visual evidence is required, request/obtain another explicit look rather than fabricate. Bound request, retention and network work.
- Owner routes require real Owner+CSRF and Kids/privacy checks. Runtime/client/bridge revalidate original generation after every blocking capture/inference and before speech. During a spoken turn allow its owned voice slot without allowing concurrent arbitrary camera capture.
- Test actual runtime/bridge orchestration with synthetic JPEG and fake provider transport: success, stale after Stop/privacy/owner loss, late failure, object initially present, explicit refresh, no automatic recapture, blur/uncertain response, provider failure, expiry. No real camera or motion.

## 3. Actually useful suggestions and requested delivery

- Keep direct replies, explicitly requested timer/reminder deliveries and unsolicited suggestions distinct. Only unsolicited suggestions consume initiative budgets; keep safety/privacy/media gates for all.
- Build actual host producers for an enrolled calendar lead-time source and one enrolled allowlisted Home Assistant condition. Reuse current backend adapters and host credentials; no fake enum-only source, arbitrary URLs, unbounded entity access or copying credentials onto robot. Source disabled until enrollment. Never choose a real owner's entity/calendar implicitly.
- End-to-end fixture proves enrollment -> fresh source -> policy -> successful announcement -> response -> finished read-only help -> dedupe/expiry. Defaults must not speak personal calendar details to an anonymous Yes. Explicit per-source spoken-disclosure approval or owner-only dashboard detail.
- UI/source configuration uses friendly labels, exact stable identifiers for authorization, safe condition enums, bounded timings. Missing account/source capabilities get setup guidance, not success.
- Fix/verify failure propagation and route-specific speech readiness without silent provider/voice migration. Existing failure cancellation must be retained. Do not make a generic bridge health response evidence of working TTS.
- Test unsolicited quiet hours/budget/dismissal, requested delivery bypass of initiative limits, source unavailable/expired/changed, replay, duplicate, No/Later/silence and TTS failure. Scheduled delivery meeting/privacy/absence policy must be explicit and backward compatible; no unsolicited catch-up speech added by default.

## 4. Integration and final checks

1. Add behavior/owner/race tests before each relevant change; record red/green evidence. Execute focused tests via `.artifacts/companion-checks.py` using explicit modules; baseline test environments are read-only.
2. Update current docs/roadmap and change log with observed implemented scope, opt-in migration, required runtime dependencies and remaining actual enrollment/provider/physical gates. Do not write deployed/shipped claims.
3. Run affected browser modules, full units and packaging on final tree. Use exact JUnit case identities and separate skipped/failed/passed results. Verify new bridge and static resources are in a freshly built wheel and source bytes match.
4. Parent reviews diff and reruns core positive/negative acceptance. Visually inspect Chromium screenshots at phone/desktop sizes. Test late UI responses and owner loss, not merely source strings.
5. Build a clearly unpublished development wheel, retain source identity/hash and a finite acceptance checklist. Do not retag, publish, restart the bridge/robot, enable motors, synthesize live speaker output or enroll sources under this implementation handoff.

## Check launcher

Use the exact command, with a verb and explicit test paths:

`python3 /home/timv/homebody-companion/.artifacts/companion-checks.py unit tests/test_presence.py`

Verbs: `preflight`, `unit TEST...`, `browser TEST...`, `lint FILE...`, `format FILE...`, `syntax FILE...`, `build`, `status`. No arbitrary shell, installs, external accounts or service actions. Parent owns final full gate and source commits.
