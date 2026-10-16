# Companion setup and boundaries (0.5.29)

Companion connects presence, deliberate visual help, optional suggestions and quiet recovery. It reuses the existing voice, camera, safety, power and announcement owners; it is not a second always-running reasoning agent. See the [reviewed design](companion-design.md).

Release 0.5.29 retains the accepted 0.5.28 single Owner camera permission and native-verification baseline. Installing it does not enroll sources or enable media permissions.

## Everyday experience

The Everyday Companion card provides presence, silent arrival acknowledgement, **Look at this**, bounded text follow-up, **Stop**, suggestions, explanations and source enrollment. Older initiative, offer and stable-change detector diagnostics remain under Advanced.

Occupancy and source health are separate. Healthy stationary occupancy may remain present; a failed or silent source becomes unknown, not a fabricated departure. Recovery is not a second arrival. Occupancy is not identity, attention or motion consent. Standby remains folded and motor-off.

Suggestions are optional. Source enrollment alone does not enable speech: the relevant policy, presence, timing, budget and adult speech readiness are also required. Known announcement failure suppresses further unsolicited offers. Requested reminders retain their separate delivery path and do not consume unsolicited interruption budgets. Existing host timers are RAM-only: inspect and resolve them before a bridge restart.

## One asked look, then bounded follow-up

Phone, Pipeline, GPT-Live and Realtime converge on one bounded capture. Consent comes from the user's actual words, never a model tool-purpose string. Negations, quoted examples and project/file requests do not authorize a photograph. Realtime waits for the current completed user transcript when a camera event arrives first; duplicates do not authorize another capture.

- With local vision enabled, use exactly the configured local model, without cloud image fallback. Its text analysis can enter the selected conversation.
- Otherwise use the configured host vision route, with provider credentials retained on the host.
- A fresh explicit request is required for another picture. Bounded follow-ups reuse text without capture or project, household, robot or memory tools. Once visually engaged, unrelated voice actions require Stop and a fresh wake.
- One Owner-controlled AI-camera permission permits explicitly requested looks and short sessions; the retired Everyday flag never overrides it. Camera feed and tracking remain separate. An active short session also bounds the original look authority and retained text lifetime; renewal cannot extend old work. Permission alone is not an active capture grant. A camera-on command may take one local readiness frame, never analysis or upload; generic model-selected captures are refused. Camera Stop invalidates queued starts using their original camera revision rather than granting fresh authority when a worker begins.

Authority is bound before blocking work and worker scheduling, then checked through capture, client/request preparation, submission, answer retention and delivery. Phone-private context stays with its admitting owner and surface; another owner or anonymous voice cannot adopt it. Stop, privacy, Kids/profile changes, owner revocation and relevant media/camera-grant changes invalidate pending work. Barge-in supersedes Realtime visual workers, and host continuations remain bound to the original call generation. Already-sent bytes cannot be recalled; late results are discarded rather than spoken or retained. No raw frame is retained in Companion context.

## Opt-in sources

The first source set supports at most one exact allowlisted Home Assistant condition and one exact authorized Home Assistant calendar. Either kind can be used alone. Neither is enrolled automatically.

Host prerequisites:

- `HASS_URL` and `HASS_TOKEN`, held on the host only.
- `REACHY_COMPANION_SOURCES_FILE`, a host-owned enrollment file.
- `REACHY_OFFER_URL`, the intended robot's private HTTPS `/api/initiative/offers` endpoint.
- Existing Everyday read-entity authorization for selectable conditions.
- Optional `REACHY_COMPANION_CALENDARS`, containing exact owner-authorized `calendar.*` IDs, not every discovered calendar.
- `REACHY_COMPANION_TIMEZONE` or the existing Everyday timezone for owner-local appointment times.

The owner dashboard selects catalog entries, not arbitrary entities, paths, URLs, templates or credentials. Conditions and accepted text describe read-only help. Yes does not operate a device or schedule an unrequested action.

Enrollment is revision-bound. The robot persists a fresh `companion_sources_revision` **before** contacting the host. It is omitted from public configuration. The host document and automatic offer carry matching `source_revision`. Before observing a source the host checks the robot's bearer-authenticated `/api/initiative/source-state`; the robot checks revision again at delivery. A timed-out old POST cannot queue after removal or another enrollment.

Failed network changes fail closed locally and report an error; re-save sources to reconcile them. Old unscoped manual files must be re-saved through the owner flow. Removal remains available with incomplete provider/catalog setup. Editing only a host file is not an acknowledged revocation of requests already sent to a robot.

Delivery is freshness- and acknowledgement-aware: only queued results commit a moment's hashed fingerprint; suppression preserves a still-fresh opportunity, expired opportunities do not replay, and delivered moments do not repeat within retained process memory. Enrollment changes reset that memory. This is not durable exactly-once delivery.

### Calendar privacy

By default the offer omits title, location and exact time. Anonymous spoken Yes receives only a neutral acknowledgement; sensitive detail goes to the owner dashboard for a bounded RAM-only lifetime. Speaking details requires explicit disclosure selection. Accepted speech and private insertion retain their original cancellation/enrollment scope, including held bookkeeping callbacks. Normal voice-session end clears visual context but not unrelated accepted private deliveries; Stop and privacy clear both.

## Presence input

An already-approved occupancy input uses `REACHY_PRESENCE_ENTITY_ID` and private `REACHY_PRESENCE_URL`, bounded polling and heartbeats. Failures are reported separately; recovery resends state without another arrival. Robot-side Presence and acknowledgement remain owner controlled. Configuring an input does not enable them or wake motors.

## Installation and evidence

The wheel contains robot code, static Companion assets and standalone bridge resources. The host bridge is a separate, backed-up maintenance step: an app-only managed update neither upgrades speech packages nor enrolls sources.

Use the canonical managed updater, exact release-bound wheel and checksum manifest. Preserve settings, pairings, SDK/dependencies, daemon and Home Assistant bootstrap. Require independent review, complete unit/Chromium coverage, packaged resource equality and exact post-install release identity with measured folded, torque-off safety. Keep actual run receipts outside the public source tree rather than treating an earlier development test count as final release proof.

Fake devices, loopback transports, synthetic inference and generated audio files do not establish supervised physical camera, wake-to-speaker, interruption or motion acceptance. Deployment is not fresh media consent. Do not enable sources, camera viewing, movement or unsolicited policies merely to prove installation.
