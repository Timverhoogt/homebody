# Homebody Companion — a complete experience

**Status:** proposed design for owner review, not implementation or permission to activate features.

Prepared from a source-grounded consultation with **Claude Fable 5.1 (`claude-fable-5-1`), high effort**, using saved Claude Max subscription authentication. The consultation completed successfully with that model; no robot tools, file-edit tools or external integrations were available to it. Hermes checked important conclusions against the source afterward. This is a reviewed synthesis, not an unqualified reproduction of the consultation.

## The purpose

**Reachy should be a considerate companion in the room: available without demanding attention, responsive when invited, and useful from the start of an interaction to its conclusion.**

The goal is not an ever-running agent looking for things to do. Nor is it merely a silent robot with a collection of optional automations. It is one coherent social interaction: notice, acknowledge, engage, help, and return to quiet.

Design method: Twyla Tharp’s “spine” discipline—use one clear purpose to decide which parts belong, rather than accumulating features.

## Why the current experience is incomplete

The examined implementation has valuable components: authenticated presence input, a silent acknowledgement, an interruption policy, bounded offers and responses, a visual-change detector, cancellation checks and preference counters. However, an infrastructure component is not a finished user experience.

The clearest example is shared physical context. Its current success path is phone Start → detect a stable change → offer help → accept → instruct the owner to wake Reachy and ask again. It detects but does not complete the help. Explicitly requested looking also depends on initiative settings designed to ration unsolicited interruptions.

Other gaps:

- Presence does not adequately distinguish a current occupancy fact from unavailable or stale information.
- Companion controls are intertwined with Agent/Workspace-related admission, even though acknowledging someone or answering a visual question should not require project-execution authority.
- Allowed offer-source names do not establish that real producers are connected, enrolled and useful. The local presentation producer and an authenticated external ingestion route exist; other integrations must be traced, not assumed absent or complete.
- Speech works through multiple paths. The morning log showed a successful presentation detection followed by failed ordinary announcement synthesis, even though separate Kids speech had worked.
- Follow-up conversation exists in at least the Pipeline implementation behind `continuous_conversation`. We need a consistent, tested experience across the selected voice paths, not a duplicate conversation engine.
- Permissions, power state, conversation state, initiative frequency and project authority are presented as if they are one setting problem. They are not.

## A complete day with Reachy

### Arrival

The office sensor reports occupancy. Reachy records that someone is there, without claiming who it is. If already Awake, allowed to move and idle, it gives one subtle acknowledgement. It does not wake itself, greet every sensor update or start recording the room.

If in Standby, it stays still. If occupancy is already true when another person arrives, it does not invent a second arrival or identify a second person.

### Working nearby

Reachy stays quiet while you work. Continued presence is not an invitation to talk.

If Home Assistant or its bridge becomes unavailable, the dashboard says occupancy is unknown. A healthy person sitting still must not become “unknown” merely because occupancy has not changed. Source-health heartbeats and occupancy transitions need separate treatment. Reconnection is not automatically a new arrival.

### Addressing Reachy

A wake phrase, an explicitly configured button or a deliberate supported interaction starts an exchange. A gaze direction or visual-change guess alone does not authorize conversation, cloud capture or motion.

Reachy acknowledges, listens, answers and leaves a short, indicated opportunity for a follow-up. “And what about tomorrow?” should not need another wake phrase while that opportunity is open. Silence closes the interaction and returns to quiet. The follow-up timeout is bounded and adjustable in Advanced settings; its exact value needs measurement rather than being treated as a model-proven constant.

If a suggestion was about to speak, your direct request wins. Background suggestions do not interrupt a conversation or compete for audio or movement.

### Looking at something together

You hold up a letter: “Hey Hermes, what does this say?”

With camera permission already configured, this authorizes one on-demand capture for that request. Reachy clearly indicates camera activity, captures even if the letter was already in view, analyses the image and answers. It does not require an empty-scene baseline or ask you to start over.

You ask a follow-up about the same letter. Reachy uses the current conversation’s available visual analysis and states uncertainty if it cannot answer. It must not invent unread details. “Look again” or “And this one?” explicitly authorizes another capture within the same bounded interaction.

An optional, separately explained **look-together session** could later allow a limited period of local sampling and bounded refreshes. That is a different permission from one look. Default: no automatic extra upload because the image changed. Before implementing a session mode, agree its duration, frame limit, refresh rule and local/cloud processing.

The existing stability detector can help choose a less-blurry frame inside an authorized look. It must not be mistaken for object recognition or proof that a person intended to share something. It must not prevent capture forever when an object is already held steadily.

### Receiving useful help

A timer you explicitly set finishes: this is requested delivery, not unsolicited initiative. It uses the agreed reminder/quiet/privacy delivery policy, not a generic “too many suggestions” limit.

Separately, an enrolled calendar source could offer a neutral prompt before an event. Acceptance completes the offered read-only help immediately. “Yes” never means permission to send messages, change Home Assistant devices or start project work.

Because occupancy cannot identify listeners, calendar speech needs an explicit disclosure policy. A generic headline followed by “yes” is not proof of owner identity or privacy. Private items default to the protected dashboard or another explicitly configured existing notification channel; speaking details aloud is a separate owner choice.

### Leaving or stopping

Room empty: no new presence-driven acknowledgement or unsolicited offer. Occupancy loss does not cancel independently approved durable project work.

Stop cancels the current companion interaction, speech, capture and pending follow-ups. Delayed model or sensor callbacks cannot revive them. Stop itself must not cause a new neutral/folding movement. Power transitions use the existing safe controls.

Privacy tears down relevant capture/listening activity and prevents new companion perception. Kids retains its own existing consented activities and safety rules; adult companion behaviour cannot intrude into it.

## One understandable model, with separate controls underneath

The user-visible interaction is:

**Quiet → acknowledged → listening → helping/looking → follow-up → quiet.**

Not every interaction passes through every step. Presence can remain quiet indefinitely. Explicit engagement does not require an occupancy sensor to be working.

Keep these separate:

| Dimension | What it answers |
|---|---|
| Presence and source health | Is there reliable evidence someone is here? |
| Interaction | What are we currently doing together? |
| Power and motor safety | Can the robot physically move? |
| Media permissions/privacy | May it listen or capture, and for what purpose? |
| Interruption preference | May it offer something without being asked? |
| Everyday/Workspace authority | Which backend and approved capabilities may this request use? |

Unknown information must remain unknown. Occupancy is not identity; DOA is an uncertain direction; visual stability is not meaning; face following is not recognition or consent. Missing cues reduce optional behaviour, not expand permissions.

## Suggestions must have real producers and finish their job

Separate three kinds of speech:

1. **A reply you requested:** direct conversation or visual help.
2. **A delivery you scheduled:** an enrolled timer or reminder.
3. **An unsolicited suggestion:** optional, source-specific and subject to interruption limits.

Only the third uses initiative budgets and learned dismissal timing. All three retain safety, media consent, privacy, source authority and cancellation checks. Requested reminders still require an explicit policy for Meeting, Sleep, absence, privacy and failure; do not silently choose loud delivery or delayed catch-up speech.

For the first coherent release, implement no more than two enrolled unsolicited sources:

- **Calendar lead-time:** selected calendar, lead time and spoken-disclosure policy.
- **One useful Home Assistant event:** an exact entity and condition selected by the owner, with a friendly name. Do not invent a washing-machine entity or broaden the existing Home Assistant allowlist.

Each source must demonstrate the complete path:

**Enrollment → real source observation → freshness check → eligible moment → successful speech → response → completed read-only help → no repeat.**

Support source unavailable, event changed, expiry, duplicates, No, Later, silence, speech interruption and TTS failure. Avoid backlogs of expired suggestions. “Later” should mean a clearly explained future behaviour, not an arbitrary hidden reschedule.

Source setup must state who/what is accessed, where processing occurs, what might be spoken and how to disable it. Calendar access or an HA capability in a manifest is not proof of completed enrollment.

## UI: show the relationship, not the plumbing

One **Companion** area available from Everyday and reachable without setting up a project. It shows:

- Current truthful activity: Quiet, Listening, Looking, Replying or unavailable.
- Presence with source health: someone present, empty or unknown.
- Camera and microphone use now, separate from configured permission.
- A direct **Look at this** action; **Stop** remains reachable.
- Suggestions: Off / Gentle / More often, with source enrollment and quiet hours under Settings. Final names can be chosen during UI design.
- One useful explanation: “Suggestions paused: voice unavailable”, not a stack of unexplained disabled toggles.

Advanced details hold diagnostics, cooldowns, budgets, source receipts, learned category preferences and reset controls. Keep a visible permission summary and a useful “Why?” explanation. A master Off control must state exactly which companion functions it disables; it must not silently disable ordinary conversation or erase reminders.

Everyday/Workspace stays the primary routing/authority choice. Companion state and visuals are shared where appropriate, but they do not implicitly bind a project, inherit private Workspace context or grant execution permission. Existing Kids scope remains separate.

## Runtime approach: integrate, do not rebuild everything

Use a small deterministic interaction coordinator around existing ownership and safety mechanisms. This is not a constantly running model loop.

- Normalize events with provenance, freshness, requested/unsolicited classification, sensitivity and current ownership.
- Track source health independently from last occupancy transition. The current presence bridge forwards changed states only, so an expiry-only receiver fix would incorrectly age out healthy stationary occupancy.
- Reuse existing voice/motor/camera locks and motor-before-Kids order. Bind asynchronous work to the original relevant cancellation generations and authorization state. A new global counter is not a replacement for these contracts.
- Reuse `InitiativePolicy` and `PreferenceLedger` for genuinely unsolicited behaviour.
- Replace the presentation-as-offer chain with direct visual assistance; preserve compatibility/migration deliberately rather than deleting public routes without checking callers.
- Reuse existing follow-up voice paths and one-frame capture. Prove the complete selected transport rather than implementing yet another independent loop.
- Give all adult speech consistent approved voice intent and truthful readiness, without silently changing provider, paid usage or Kids voices. Health must be specific to the real speech route; a generic bridge 200 does not prove synthesis.
- Preserve existing cancellation on announcement failure. In the examined source, failed contextual-offer speech already cancels its token, and response listening begins only after successful playback. The gap is not a proven orphan response window.
- Keep bounded model calls for actual questions and visual interpretation. Producers and interruption decisions remain deterministic.

Offline behaviour must match real topology. Presence depends on HA/bridge health; local vision may be on a robot or another server; speech and interpretation may depend on the Hermes host or cloud. “Local enabled” is not proof of host-independent availability or spare Jetson memory. Use only verified configured fallbacks and never silently move private media to a different provider.

No persistent image archive. A “no images stored by the app” claim does not imply a cloud provider retains nothing. Explain processing and retention honestly. If bounded RAM/session context is used, define its lifetime and clear it on teardown. Durable records should be minimal operational counters and explicitly configured reminder/preferences data, not room history or raw transcripts.

## Three integrated delivery milestones

### 1. A coherent companion interaction

Deliver reliable presence and source-health handling, subtle acknowledgement, explicit engagement, consistent selected voice/follow-up behaviour, one clear Companion area and truthful failures. Separate interaction state from power/privacy/authority. Fix the actual adult announcement route before relying on proactive speech.

**Demonstration:** arrive → at most one safe acknowledgement → work undisturbed → address Reachy → follow up without unnecessary wake → return to quiet. Drop/reconnect HA without false arrivals. Stop, privacy and Kids win over held callbacks. Standby never gains motors because someone arrived.

### 2. Complete looking together

Replace detect/offer/wake-again with direct, consented capture and useful interpretation. Support already-present objects, follow-ups, explicit refresh, blur, ambiguous targets, unavailable provider and visible start/finish. Bounded look-together session is separately approved if included; one-shot remains the safe default.

**Demonstration:** hold up a real letter → ask → get useful reading → ask a grounded follow-up → show another item and explicitly refresh → Stop during a held response → no late speech/capture. No private media uploaded outside the admitted request/session.

### 3. Useful initiative, actually connected

Unify requested reminder/timer delivery policy and implement the two enrolled suggestion sources. Include freshness, shared-room disclosure, quiet hours, duplicate protection, response outcomes, speech failure and adaptation explanations. Test what No/Later do in practice.

**Demonstration:** a requested timer is not discarded as too much initiative; a real enrolled event produces one useful offer; Yes finishes the help; no source access or speech occurs without enrollment; an expired or duplicated event stays silent. No unreviewed external action follows acceptance.

## Definition of complete

For each milestone distinguish:

1. **Implementation evidence:** deterministic and race tests, selected voice-transport tests, permission boundaries, migration and real browser lifecycle checks. Held old callbacks after Stop/privacy/owner loss must be deliberately released and rejected.
2. **Silent integration evidence:** actual configured provider/source operations with synthetic or consented inputs, bounded timeouts and no robot playback/motion. Mock tests remain separate from actual provider proof.
3. **Deployed evidence:** exact installed app and bridge identities, effective enrollment/settings, real UI/status/readiness and retained configuration. Do not equate a host bridge update with robot deployment.
4. **Supervised physical acceptance:** Tim present, clear space and explicit camera/motion consent. Test audible speech, acknowledgements, capture, interruption and Stop on the real robot.
5. **Household trial:** an agreed short period using real routines. Record coarse failures/interruptions only. Tune or remove behaviour that annoys, exposes private information or cannot reliably finish.

A disabled broken path is not completion. A passing mock is not provider readiness. An accepted request is not audible or semantic success. A useful implementation must include graceful failure, not claim guaranteed perception or network availability.

## Decisions before implementation

Recommend proceeding with the coherent design, then settle only permissions/preferences that genuinely change behaviour:

- **Camera:** one-shot on explicit request by default. Any repeated-capture look-together session requires separately explained opt-in.
- **Suggestions:** disabled until source enrollment and acceptance. Recommend gentle frequency after enrollment, not enabling all sources on upgrade.
- **Adult speech:** keep the selected voice intent; verify/fix its real route. A provider change or paid fallback requires explicit agreement.
- **Sources/disclosure:** select the calendar and one exact HA event, and decide which details may be spoken in a shared room. Recommend private/phone-only for sensitive items.
- **Reminder delivery:** agree behaviour during Meeting/Sleep/privacy/absence, and use only an existing configured notification destination unless new setup is authorized.

No face/voice identity, emotional profiling, room surveillance, autonomous project execution, automatic external actions, mobile navigation or second Hermes installation is required. No Kanban programme is necessary for this design. Publication, deployment and supervised physical tests remain separate approvals.

## Review corrections to the raw Fable consultation

The raw consultation is useful but not authoritative. Its unsupported or overbroad points were not adopted:

- “Only real offer producer” was too broad: an external structured-ingestion endpoint also exists; actual enrollment and producer deployment require discovery.
- Failed TTS does **not** establish an orphan response window; source already cancels the offer token on failure.
- Follow-up listening is not wholly missing; Pipeline has `continuous_conversation`.
- An occupied sensor forwards changes only. Timestamp ageing requires a heartbeat/source-health design, not an arbitrary timeout alone.
- A single spoken look must not implicitly authorize four captures or automatic cloud refreshes.
- Occupancy and an anonymous “yes” cannot authenticate the owner for speaking sensitive details.
- Local-vision configuration does not prove offline availability or verified Jetson placement.
- Mocked provider tests are not actual silent provider tests.
- Kids companion suspension must not remove existing consented Kids camera activities.
- Exact coordinator line count, latency guarantees and timeouts were suggestions, not measured engineering facts.
