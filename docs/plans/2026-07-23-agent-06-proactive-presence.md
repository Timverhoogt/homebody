# Agent 0.6 — Proactive Presence

**Product direction:** Reachy should become a quiet, context-aware home presence that can help without always waiting to be asked. This release is about social timing, local context, and embodiment—not enterprise workflow orchestration or broader authority.

## Product principles

1. **Silence is a successful outcome.** Most observations must not produce speech or movement.
2. **Embodiment before interruption.** Reachy may acknowledge presence physically before it ever speaks proactively.
3. **Local and ephemeral first.** Presence signals are sanitized facts, not raw camera/audio streams. No face recognition, identity matching, or silent image storage.
4. **Existing safety always wins.** Kids Mode, Meeting, Sleep, privacy, Stop, motor state, an active conversation, camera control, and explicit robot actions suppress proactive behavior.
5. **No authority expansion.** Agent 0.6 does not add shell access, autonomous writes, unattended external communication, or automatic plan recovery.
6. **Explainable initiative.** The trusted phone shows whether Presence is enabled, the latest sanitized signal, whether Reachy acknowledged it, and why behavior was suppressed.

## Goal 1 — Presence and attention

**Outcome:** Reachy can accept bounded, authenticated presence/attention signals, maintain an ephemeral local presence state, and give a subtle silent acknowledgement only when it is already Awake and physically safe.

### Inputs

- Trusted Home Assistant occupancy/presence automation through an authenticated local endpoint.
- Existing local wake-word/DOA event as an attentive-presence observation.
- Existing validated local gesture event as an attentive-presence observation.
- No raw image, audio, person name, room history, or arbitrary metadata enters the presence state.

### State

- `away`, `present`, or `attentive`.
- Sanitized source enum.
- Optional bounded direction in degrees.
- Observation age and confidence band.
- Last acknowledgement and suppression reason.
- Count of silent acknowledgements for the current process only.

### Silent acknowledgement

- A small head/antenna pose; never speech or sound.
- Never wakes Reachy from Standby.
- Requires confirmed Awake mode and enabled motor torque.
- Suppressed during Kids Mode/lock, Meeting, Sleep, privacy, active voice activity, announcements, camera control, face tracking, explicit robot movement, runtime startup/shutdown, and cooldown.
- Occupancy alone may produce at most one acknowledgement per cooldown. An attention signal may use a bounded direction when supplied.
- Stop or a safety transition invalidates pending presence behavior immediately.

### Trusted-phone UI

- One compact Presence section in the Agent workspace.
- Enable/disable switch.
- Current state and latest sanitized source.
- Last acknowledgement or suppression reason.
- Explicit statement: “Silent acknowledgement only · no proactive speech.”
- No new top-level tab and no raw event history.

### Acceptance gate

- Disabled by default after upgrade.
- Authenticated endpoint rejects unknown source names, extra fields, invalid directions/confidence, and missing/wrong bearer credentials.
- Every suppression rule has a deterministic test.
- Repeated signals respect cooldown and cannot queue movement.
- Presence never changes power mode or enables motors.
- Status contains no identity, raw sensor payload, image, audio, or secret.
- Safe live test first verifies Standby produces state only and no movement; optional Awake physical acceptance requires clear space and explicit owner control.

## Goal 2 — Initiative policy

**Outcome:** Reachy can deterministically decide whether a contextual moment is eligible for proactive speech while still remaining silent in most cases.

**Implemented foundation:** disabled by default; deterministic and process-local; phone-visible Quiet/Balanced/Engaged controls, quiet hours, budgets, cooldowns, duplicate suppression, dismissal backoff, and sanitized decision reasons. Goal 2 emits eligibility outcomes only and cannot generate speech.

- Quiet/Balanced/Engaged modes.
- Quiet hours, per-hour/day budgets, topic cooldowns, dismissal backoff, and duplicate suppression.
- Hard suppression during active conversation, media/announcement ownership, Kids, privacy, Meeting/Sleep, camera control, and robot actions.
- Eligibility engine produces `remain_silent`, `physical_acknowledgement`, or `offer_candidate`; it does not generate speech itself.

## Goal 3 — Contextual offers

**Outcome:** Reachy can make one concise, high-confidence offer based on allowlisted context.

**Implemented foundation:** disabled by default; authenticated structured ingestion from a hard allowlist of calendar, reminder, timer, selected Home Assistant, weather, and explicitly scoped project sources; deterministic Goal 2 eligibility; one bounded spoken question; a 5–30 second English/Dutch yes/no response window; and trusted-phone Yes/No controls. Yes only reads pre-rendered, read-only help and never executes an Agent capability or consequential action.

- Upcoming calendar event, active reminder/timer, selected Home Assistant states, weather, and explicitly scoped project context.
- One sentence followed by a yes/no response opportunity.
- No automatic consequential action.
- Every offer includes a short phone-visible explanation and obeys initiative budgets.
- Raw offer text, accepted text, fingerprints, and voice transcripts are omitted from polled status; the phone receives only sanitized source/topic/state/explanation.

## Goal 4 — Shared physical context

**Outcome:** Reachy can notice when the owner intentionally presents an object or text and offer help.

**Implemented foundation:** disabled by default; an unlocked-phone Start/Stop gate; a 5–30 second safely-Awake window; local central grayscale change detection with three stable samples; immediate JPEG discard and ephemeral feature cleanup; sanitized phone status; and one internal `presentation` candidate routed through the existing Goal 3 policy. A Yes response performs no vision or other action—it instructs the owner to wake Reachy and make a separate explicit look/read request through the existing one-frame cloud-vision path.

- Local, visible, ephemeral perception while Awake and camera-enabled.
- Intentional-presentation gate; no continuous semantic surveillance.
- No face recognition, child monitoring, identity inference, or background recording.
- Cloud vision only after explicit interaction and through the existing bounded image path.

## Goal 5 — Personal adaptation

**Outcome:** Reachy adapts timing and category preferences without silently changing authority.

**Implemented foundation:** an owner-visible preference ledger (`reachy_mini_hermes/adaptation.py`) for eight categories: presence plus the seven offer sources. It holds only decayed counts of welcomed, dismissed and snoozed responses (14-day half-life), dismissals per part of the day, a snooze deadline and a disabled flag. "Yes" welcomes a category, "no" dismisses it, and "later" / "not now" snoozes it for four hours without counting as a decline. The policy may scale the topic cooldown between 0.5× and 4×, honour a snooze or a disabled category, and keep quiet in a part of the day after repeated dismissals there. Confidence thresholds, budgets, permissions and risk tiers are never changed. Every decision carries a plain-language "Why did Reachy do that?" explanation. Preferences persist next to the app config as small counters only, and can be reset per category or entirely.

- Learn only coarse preference signals: welcomed, dismissed, snoozed, disabled category.
- Adapt frequency and timing, not permissions or risk tier.
- Transparent controls, reset, and “Why did Reachy do that?” status.
- No hidden behavioral profile or raw interaction archive.

## Release sequence

1. **0.6.1 Presence and attention** — state, authenticated signals, silent acknowledgement, phone status.
2. **0.6.2 Initiative policy** — eligibility, budgets, quiet hours, dismissal backoff.
3. **0.6.3 Contextual offers** — concise proactive speech from allowlisted context.
4. **0.6.4 Shared physical context** — intentional object/text presentation.
5. **0.6.5 Personal adaptation** — transparent preference tuning.
