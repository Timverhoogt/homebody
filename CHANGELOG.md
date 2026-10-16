# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project currently remains in early alpha and has not published a stable compatibility promise.

## [Unreleased]

## [0.5.21] — Owner-authorized shutdown over Tailnet

- Correct the legacy desktop-refusal test to spy on the current helper boundary, install the independent scipy oracle in hosted CI, and isolate HTTPS shutdown fixtures from mocked PWA installation. The 0.5.20 draft failed hosted test gates and was not published or deployed.

- Admit host shutdown through an explicitly provisioned, narrowly scoped root-owned Unix-socket helper while retaining the app's non-root identity and NoNewPrivileges sandbox. Require measured safe sleep pose and disabled torque before scheduling, and revalidate both before OS poweroff. Missing helper provisioning fails closed.
- Return success only after the fixed OS timer is active; expose Owner-only readiness and truthful failures. Preserve Owner pairing, same-origin HTTPS, CSRF and Kids management locks.
- Treat phone/Tailnet proxy errors or disconnects before acknowledgement as an unknown outcome, with no automatic retry or duplicate shutdown submission. A scheduled acknowledgement is not proof of physical poweroff.
- Preserve Everyday fast paths, bounded robot STT prewarming, all seven Kids languages and selected voices, Bluetooth and enforced Workspace discussion. No physical shutdown, microphone, camera or motion acceptance was performed for this release.

## [0.5.19] — Everyday read fast paths and bounded robot STT prewarming

- Answer standalone English/Dutch Everyday clock/date, existing configured weather and exact allowlisted single-device status requests through existing read-only tools, bound to the original generation and authorization state. Context-bearing or ambiguous turns retain the narrow reasoner; Workspace, Kids and Realtime routing and permissions are unchanged.
- Prewarm the existing trusted on-robot STT worker only during an admitted endpointed Pipeline/Kids audio turn, reuse inference warmth across turns and languages, and release idle memory after 120 seconds. Stop cancels pending work; privacy, Meeting and Sleep release the worker. No boot/status warming, model downloads, provider changes or new dependencies.
- Preserve all seven configured Kids languages, selected voices, adult speech engines, enforced tool-free native Workspace discussion, settings and safety boundaries. Synthetic recognition/clock measurements are not supervised microphone, playback, camera or motion acceptance.

## [0.5.18] — Speech safety, five Kids languages and enforced Workspace discussion

- Correct Python 3.11 Bluetooth-agent teardown so cancellation cannot cancel its own cleanup supervisor. The 0.5.17 draft failed the hosted compatibility gate and was not publicly released or deployed.

- Integrate strict server-rendered Kids speech selectors, exact moderated-text capabilities, bounded turn deadlines and Stop/End fences across recognition, game transitions and synthesis.
- Negotiate tool-free same-session native Hermes discussion with enforced dispatch, plugin and auxiliary-work isolation. Requires a compatible separately deployed native listener; unavailable enforcement fails closed. Lost admission responses can leave tool-free transcript context, never work authorization.
- Repair owner-loss and stale native display/readiness responses, preserve existing consents, and keep Bluetooth controller pairing explicit and movement disabled by default.
- Report daemon face following as requested rather than confirmed; provide pinned, checksum-verified model-cache provisioning without camera activation.
- Add Spanish, German, French, Italian and Portuguese Kids sessions on the strict server-rendered catalog.
  - Selector-only fixed speech, session-pinned language, per-language bridge and robot copy, retry and safety copy.
  - I Spy requires session-language hints in the strict native schema.
  - Language-scoped host commands, and recognition-language transport.
- Each new language speaks only with its own `ELEVENLABS_KIDS_<LANG>_VOICE_ID`. There is no default, Dutch or configured-speech fallback.
  - Start is refused before any side effect without that language's voice proof, and a Stop/End during the proof wins.
  - EN/NL and Dutch Ruth are unchanged.
- Add the source-only `tools/kids_language_readiness.py`. It reports source, config and manual acceptance separately.
- Voice IDs remain host-configured, not shipped defaults; new-language waiting clips are optional and absent. Portuguese copy targets European Portuguese. Owner-selected reference-host voices do not establish native-language or supervised gameplay acceptance (see `docs/kids-language-deployment-readiness.md`).
- Synchronize PWA shell/query v98.

## [0.5.16] — Dutch Kids voice and latency visibility

- Add a host-configured Dutch-only Kids voice override, resolved from the validated language bound to each single-use moderated-text approval. English Kids voice and adult speech engines are unchanged; the package does not activate Ruth on other installations implicitly.
- Package three short Netherlands-Dutch Ruth waiting clips as a separate fixed, integrity-checked voice bundle. Keep existing Jessica clips for compatibility; use only a current per-language bridge voice/model proof, with silent refusal on missing, malformed or mismatched evidence. No per-turn live filler synthesis.
- Remove the blocking processing cue before Kids recognition, leaving adult Pipeline behaviour unchanged. The previous real story-time cue cost about half a second; this does not claim the remaining live recognition/reasoning delay is solved.
- Add numeric, text-free native/client recognition timings, first provider PCM and SDK enqueue marks while preserving currentness, privacy, Stop/End and output quarantine. Enqueue is not an audible-output measurement.
- Preserve on-robot Parakeet and explicitly local host fallback. Nemotron is not activated by this release; synthetic inference and streaming/full-conversation acceptance remain distinct.
- Synchronize PWA shell/query v95. Reference deployment and caregiver-supervised Dutch speech acceptance are separate milestones.

## [0.5.15] — Marketplace-first onboarding foundations

- Add a resumable Owner/CSRF-protected setup flow for marketplace and manual arrivals: confirm the actual host, save language, use existing backend settings, check connectivity/authentication silently and review privacy before next-Start listening. Initial owner provisioning and a standalone no-agent backend installer are not included yet.
- Only genuinely fresh configurations enter quiet setup; no app-owned microphone/playback, wake-model download or actions/GPIO/hosted-agent startup is admitted. Fresh motion/tools default off; established settings/startup are preserved.
- Bind short-lived connection evidence to saved speech/backend settings and a check generation; reject late checks after configuration drift or newer attempts. Connection success never claims provider-model or physical speech acceptance.
- Fence private browser setup responses on owner loss and Kids locks; render setup microphone status as Off. Finishing saves setup only; explicit Control Stop/Start admits listening.
- Remove unconditional Unix-locking imports from the desktop app import path while refusing unsupported managed-update writes before filesystem changes. This is not native Windows certification.
- Package setup JS/CSS, synchronize PWA shell/query v94 and document marketplace lifecycle boundaries and remaining acceptance work. Native marketplace and physical acceptance remain separate from package publication and reference deployment.

## [0.5.14] — On-robot recognition trial

- Add an owner-provisioned Parakeet TDT 0.6b v3 native worker for completed Kids and Pipeline utterances. Reuse the existing microphone owner and RAM samples; recognition stays on Reachy. The generic package remains on configured recognition by default.
- Offer one explicitly opted-in, confirmed-local Hermes host fallback for decoder infrastructure failures. A misheard word does not itself trigger fallback. Adult Live/Realtime speech engines are unchanged.
- Fence robot finals, fallback and acknowledgement work across Stop, End, privacy, power and actual game-master switches. Preserve the outer caregiver play deadline and all 0.5.13 game, I Spy and phone/power behavior.
- Show actual per-turn recognition location separately from the selected provider; clear transient provenance on teardown.
- This is completed-utterance recognition, not streaming partials. Synthetic native tests confirm fast GPU decoding but expose short-Dutch accuracy gaps; supervised microphone/adult/child testing remains required.
## [0.5.13] - 2026-10-14

### Fixed
- Kids leads with Play together and Reachy’s game-master role, with an explicit setup route before the unchanged supervised Start. Single activities are secondary; the idle host illustration is a still preview, not a live session.
- Active Kids play groups the confirmed host phase/current activity and original remaining time with reachable End/input controls; the review and single-game picker step aside. Both Start paths suppress duplicate in-flight requests.
- Shared power card shows the reported mode first, groups microphone/camera/motor status, exposes Wake up, Standby and Stop movement, and labels Meeting/Sleep and their effects. Camera permission is distinguished from this page’s viewer; unknown/offline status and unconfirmed requests do not claim power success or active listening.
- The setup entry remains uncovered on short phones with enlarged text, and bottom navigation labels wrap rather than overlap. Meaningful phase/power feedback respects reduced motion. PWA shell/query v92.
- Confirmed Kids Start brings the phone back to the animation once, respecting reduced motion and owner/End/offline fences; normal active polls do not reset the user’s scroll position. Shared Start is disabled alongside single-activity Start while offline.
- I Spy vision proposes up to four distinct candidates. Code validates and de-duplicates them before a randomized, moderated choice, preferring unused objects within the current session when possible. The chosen target stays fixed for the round; an empty valid pool fails closed. End, expiry, cancellation and superseded searches fence late selection replies.

### Deployment notes
- Dashboard and bounded I Spy target-selection update: caregiver deadline, camera consent, Stop/privacy/moderation authority, installer and settings remain unchanged. Update the companion host bridge as well as the canonical robot wheel; session-scoped recent objects are RAM-only and never exposed/persisted. Retains the 0.5.12 Agent/updater fixes and host transcription; no Jetson STT activation.
- Fixture/Chromium evidence does not replace supervised physical games/movement or acceptance on the phone’s Android WebView.

## [0.5.12] - 2026-10-13

### Fixed
- Agent tab follows the confirmed assistant mode. Everyday shows only its home/voice card and Companion; the project picker, request forms, Advanced home actions and session details exist only in a status-confirmed Workspace. Pending, offline, owner-loss and Kids states fail closed, and every reveal path (view button, Open card, approval or run prompts) is refused outside Workspace.
- Approved project work that keeps running in Everyday, or whose status cannot be confirmed, shows a compact badge and keeps Stop all Agent work reachable; it is hidden only when the badge confirms no work. Badge replies are ordered and mode changes force fresh status, so an older idle reply cannot hide a newer running-work Stop.
- Workspace explains its three separate steps (Show conversation = display only, Open project = connection, “Hey Homebody” = voice) and reports display, connection and voice state honestly. An unconfigured repository explains the exact setup needed beside the disabled Open project button.
- Clear is now “Clear & disconnect…” with an inline confirmation naming its full scope (display erased, selected project work stopped, project disconnected) and says to Show conversation and Open project again. Backend Clear/Stop behaviour is unchanged.
- Neutral “Homebody” labels where backend provenance is unknown; Pipeline/Realtime progress no longer claims Hermes is answering Everyday turns.
- Private Agent polls pause during a mode switch; admitted replies are also fenced on owner loss, offline state and Kids transitions. Server-side 423 enforcement is unchanged. Project connection copy distinguishes opening/history from submitting work and does not claim enforced read-only native discussion. PWA shell/query v91.
- Update dialogs own dashboard scroll locking, preserve the previous page position and contain long-note scrolling; Close, Escape, acknowledgement, install admission and errors release the lock. Owner loss closes them immediately and fences prior catalog replies. Text-only release notes are grouped by semantic version, with a current-to-target summary and honest coverage for one direct installation across skipped releases.
- Only the newest relevant verified update completion is shown automatically once per page session. Older unacknowledged results stay in History rather than alternating messages; expanded history sections survive polling. Install is bound to the exact reviewed offer and is disabled if the catalog changes or withdraws it. Acknowledgement errors remain visible inside the dialog.

### Deployment notes
- No installer, worker, permission or lifecycle policy changes; this release retains the existing Kids game master and Hermes-host transcription. Jetson-native recognition is not included.
- Chromium verifies modal lock/gesture contracts and phone/desktop layout; reported Android WebView background scrolling still requires owner acceptance on that device.

## [0.5.11] - 2026-10-13

### Added
- Play together: one supervised Kids session with Dutch/English game selection, mid-game switching, help, explicit round completion and a choice of another round or a different activity. Stop finishes without persuasion; the caregiver deadline stays fixed across switches.
- A shared typed game-input box, with one-use Stop-only admission during switching/search. Caregiver selection and camera consent remain paired-owner/CSRF protected.
- Illustrated game-host choosing, switching and completion scenes, with accessible End/input controls, reduced-motion states, hidden-page pause and stale-poll/owner-loss fencing. PWA shell/query v90.

### Fixed
- Bind child Stop to the outer play session so an activity-generation change cannot discard it; reject late Stop from a replaced caregiver session.
- Keep hints, give-up and replay phrases with the active game, and distinguish direct switch requests from narration and questions.
- Preserve hosted I Spy success/reveal outcomes without automatically starting another camera search; fence old game replies, speech, capture and listening tickets.

### Deployment notes
- This release retains Hermes-host local transcription. Jetson-native recognition is not included.
- Companion bridge must be updated separately. Unit/Chromium verification does not replace supervised physical gameplay acceptance.

## [0.5.10] - 2026-10-13

### Fixed
- Localize deterministic Kids introductions, warnings and safety/fallback replies in the selected English or Dutch; open the first listening turn after the introduction without a wake phrase.
- Bind automatic first-listen admission to an immutable cancellation receipt and the exact child session, checked before motor admission. Stop, End, privacy, expiry and replacement invalidate delayed starts.
- Retry empty Kids speech up to twice without demanding another wake phrase; preserve bounded inactivity and adult behaviour.
- Pass the selected Kids language to local Whisper and reuse its cached model through an owner-private, hard-time-bounded local worker. Keep input/output moderation, exact speech approvals and local-only STT; synthetic timings are not physical child-speech acceptance.
- End I Spy rounds after three meaningful guesses, handle English/Dutch give-up and hint requests, and avoid repeating approved hints.
- Play one short, cached, language-matched Kids "thinking" clip only when local transcription is still pending after about a second. The clips are a verified static catalog, eligible only when the bridge's current health proves the effective Kids streaming voice matches. There is no live TTS fallback. Stop/End/privacy/replacement cancel the clip, settle is bounded, and audio fails closed (quarantine) while an SDK push is blocked. The clip never overlaps the moderated answer.
- STT locality provenance follows each transcription's actual provider receipt, so a stale "local" claim is never shown after a cloud, custom or failed transcription.
- Show "Local transcription" only when the bridge confirms Hermes-host STT; otherwise show a neutral or cloud-accurate "Transcribing", distinct from "Thinking". The child-locked status gains only a categorical STT-locality flag. PWA shell advanced to v89.
- Align family instructions with first-turn listening and advance the PWA shell to v88. Host bridge/STT source must be updated separately; installing a robot wheel alone does not change a running host bridge.

## [0.5.9] - 2026-10-13

### Fixed
- Make confirmed Everyday/Workspace mode explicit, distinguish pending/offline state, and show only the appropriate mode card. Workspace summaries distinguish display consent, repository choice, actual binding and approved work status.
- Keep repository/session selects stable through normal polls: reads do not disable controls; identical options are not replaced. Writes supersede stale reads, and failed reads retain separate Stop while clearing unconfirmed session/target state.
- Clear private summary copies on owner loss; keep successful mode changes pending until a matching status reply rather than briefly reverting to an old mode.
- Add real Chromium repeated-poll/option-identity and mode/privacy regression coverage.

## [0.5.8] - 2026-10-12

### Added
- Owner-only GitHub repository picker backed by the host’s saved GitHub login. Private/archive markers and exact registered-session availability; no automatic clone, session creation or tool permission.

### Fixed
- Replace the obsolete “adult Agent profile” instruction with actual Workspace / Show conversation steps and a nearby Enable Workspace button. Rename the lower view selector Conversation & projects and open that view after confirmed Workspace activation.
- Clear private repository metadata on display/owner lifecycle loss and reject late catalog responses.

## [0.5.7] - 2026-10-12

### App-wide motion language
- **Home.** The hero's static Reachy mark is now a `presence` figure from the shared scene component. It shows awake, standby, listening, thinking, speaking, meeting, asleep, trouble and offline using only `runtime.state` and `power_mode`. Unknown and starting states stay neutral. The old always-running hero CSS loops are gone.
- **Workspace.** A small `workbench` strip (you · Reachy · worker) reflects the native run status enum, whether an approval is pending, and connection uncertainty. "Ready to test" uses the same exact receipt check as the status text; "completed" alone reads "Finished · not verified". A pending approval gets three soft swells, then a steady still. There is no progress estimate.
- **Camera.** A `lens` glyph beside the status badge and a quiet edge tone outside the video. The lens opens only while decoded frames are fresh: stale video reads "held", and privacy, owner loss and backgrounding read closed or offline. Nothing is drawn in fullscreen, and the lens makes no requests.
- **Updates.** An `updater` rail shows the five durable journal steps. Success needs an unacknowledged `succeeded` job. An unreachable updater reads "Reading saved progress", not success. Reads are numbered, so a late answer cannot move the rail.
- **Shared micro-interactions.** `motion.js` (`HomebodyMotion`) adds token-bound pending/confirmed/failed button feedback for selected requests, one-shot entrances for the update dialog, the approval section and the view switch, and opt-in `details[data-hb-disclosure]` entrances. It uses no DOM observers, storage or network.
- **Lifecycle.** Every adult mount reuses the owner guard on each update, pauses when off screen, hidden or under reduced motion, and resets on owner loss. Scenes now re-check geometry once on a real change, so a live phase that starts during page start-up is not left paused.
- **Accessibility.** Scroll padding keeps focused controls clear of the sticky top bar and the fixed phone tab bar. Touched Workspace and Update controls are at least 48px on phones.
- **Showcase.** `docs/scenes/ui-motion-showcase.html` is labelled SIMULATED, is local-only and is not packaged. `motion.js` is listed in the PWA shell at the existing query version; bump it at release.

## [0.5.6] - 2026-10-12

### Activity scenes
- **Kids stage.** The Kids display now has an illustrated stage with a distinct scene for each of the six activities. It replaces the static CSS mascot.
- **Bound to existing status.** Phases come from the existing categorical runtime state, plus I Spy `camera_active` for the camera search. No new status fields were added. Unknown states show a neutral "Waiting".
- **No content shown.** The stage never shows transcripts, replies, nicknames, guesses, targets, scores or answers.
- **One shared component.** `scene.js`/`scene.css` is reused for a compact Reachy companion in the Everyday card. It has a mount/update/reset/destroy lifecycle and pure status adapters.
- **Motion rules.** CSS-only loops run in live phases only. They pause off screen, in hidden tabs or pages, and under reduced motion. Identical polls are ignored. End, offline and owner loss show motionless neutral stills, and a status poll that started before End cannot revive the scene.
- **Showcase.** `docs/scenes/showcase.html` is a simulated, local-only review page. The PWA shell lists the two new assets at the existing query version; bump it at release.

## [0.5.5] - 2026-10-12

### Integrated I Spy typed guesses
- Add an 80-character Your I Spy guess field and Guess/Enter submission to an active integrated Kids I Spy session. Objects, hints and yes/no confirmations use the existing screened game handling and exact-approved child speech path; typed input skips microphone recording and STT, while Reachy still replies aloud.
- Serialize one submission with voice/announcement activity. Require an already-Awake active unexpired I Spy session and a short-lived-in-memory one-use input capability; rotate on lifecycle changes and spoken/typed turns. Do not auto-wake or enable new camera/motor authority. End, privacy, replacement and expiry cancel stale work. Reject oversize, extra-field, stale/replayed, busy and non-I-Spy submissions.
- Keep guesses/tokens out of browser storage, locked status and error details. Clear the draft on End/activity change; guard asynchronous UI results and disable duplicate submits. Phone input/Guess row has bounded width and 48px button height.
- PWA shell/query v83, including the new bundled input script. Synthetic/local browser and runtime tests are not physical camera/speaker acceptance.

## [0.5.4] - 2026-10-12

### Kids speech and voice-setting clarity
- Provide a cached-model CPU-only local STT command for a bridge whose managed subscription runtime lacks faster-whisper but whose existing separate Python environment already supports it. Host-only command configuration restores local transcription without a cloud fallback or changes to primary Hermes dependencies. Language auto-detection supports English and Dutch.
- Keep Kids speech-recognition settings visible even when adult speech uses Live/Realtime. Label adult voices separately and explain dedicated moderated Kids reply voice versus Pipeline greetings/fallback. No child moderation, exact speech approval or runtime routing changes.
- PWA shell/query v82. Reference host verified real generated English/Dutch speech through configured STT, moderated subscription Kids reply and approved ElevenLabs PCM generation without microphone, physical playback, camera or movement tests.

## [0.5.3] - 2026-10-12

### Mode selector spacing
- Everyday and Workspace use padded, auto-height cards with a minimum 100 px height, readable wrapped descriptions and an 18 px gap before helper text. Very narrow phones stack the cards; selected/disabled state and routing are unchanged.
- Mobile Stop controls avoid min-content overflow from the running-work badge. PWA shell/query revision v81 refreshes cached styles.
- Includes the separately configurable subscription host support below; installing the robot package does not configure or restart a host bridge, change saved models or install an Android APK.

### Host subscription routing (after robot v0.5.2)
- Optional ChatGPT/Codex subscription text routing through the installed Hermes runtime for narrow Everyday, isolated Kids chat, I Spy vision/guess judging and restricted planning. Saved supported authentication stays on the trusted host; no paid text API fallback or implicit full-agent authority.
- Genuine reasoning off (`enabled=false`, `effort=none`) is required and checked against the emitted wire payload. Account-tested reference choices are `gpt-6-sol` (Everyday/planning/Workspace) and `gpt-6-luna` (Kids/I Spy), not universal defaults. Unsupported off settings fail closed.
- A dedicated authenticated loopback native Hermes listener pins one owner-registered project session, subscription provider/model and confirmed runtime lock without changing the primary gateway. Native discussion uses the worker's configured tools; instructions and a working directory are not a sandbox. Planned execution retains exact phone approval, fixed checks and matching receipts.
- Runtime bootstrap preserves bridge arguments across a managed Python re-exec. Subscription HTTP routing leaves speech, moderation, HA and native-session HTTP on their existing transports; Kids moderation, strict validation, privacy and lifecycle guards remain in place.
- These companion changes landed in PRs #77 and #78 after v0.5.2 and were deployed separately to the reference host bridge. They do not replace that immutable robot wheel, install an APK or automatically configure every paired installation. Speech API charges and moderation remain separate.

### Documentation
- Refresh GitHub README and the mirrored Hugging Face landing page around Everyday, Workspace and Kids, separately from Pipeline/Realtime/GPT-Live speech transports.
- Add a dated source/release/deployment/acceptance snapshot and an explicit subscription/native-session host setup recipe. Correct the obsolete never-run-on-Jetson statement without claiming blanket GPU or physical acceptance.
- Document private phone camera fallback and recent lifecycle repairs, measured speaker volume/mute, managed update boundaries and robot/dashboard versus Android APK updates.

## [0.5.2] - 2026-10-12

### Clear update messages
- Update controls now say **Reachy software update**: they update Homebody on the robot and its dashboard, not the Android app (APK) on your phone.
- A successful update opens with **Reachy update installed**, the previous and new versions, a verified restart result and a clear Done button. Technical release notes are collapsed below the result.
- Checking again after installation says **Reachy is up to date** and **No installation is needed**, instead of continuing to offer the already installed version from a stale catalog.
- Check failures remain visible instead of being hidden behind an earlier successful update. These wording changes do not change pairing, device permissions or speech settings.
- Speaker settings have consistent spacing, a single primary Apply action, side-by-side secondary controls and a short explanation with optional details. Helper text no longer touches the buttons.
- Everyday shows the selected voice engine before the separate text backend. Provider details and technical device IDs are collapsed; tool readiness is not labelled as a verified live voice session.

## [0.5.1] - 2026-10-12

### Managed-updater bootstrap
- Release-bound artifacts use this small follow-up commit so the installed 0.4.0 updater can verify GitHub's commit response within its existing 500 KB download bound. The large 0.5.0 integration merge exceeded that bound and was safely refused before shutdown; its published artifacts are not changed.
- Includes the Everyday/Workspace, GPT-Live preview, speaker-volume and managed-update features from 0.5.0 without widening device permissions or changing saved speech-engine settings.

## [0.5.0] - 2026-10-12

### Everyday and Workspace (behaviour change)
- **Routing changes for existing installs.** Adult voice turns outside Workspace now use **Everyday**: a fast, narrow host backend (Home Assistant status/on-off control from the existing allowlists, weather from a configured source, the host clock, direct knowledge answers). Everyday never reaches the full Hermes agent, memory, notes, projects or coding tools, and there is no fallback to them. Saved configs without `assistant_mode` load as Everyday with a one-time "routing changed" notice. The saved voice engine (Pipeline/Realtime) is kept.
- **Workspace** is the adult Agent profile, entered only from the phone. A bound native Hermes session answers discussion turns through `/v1/runs` only when the host target sets `discussion_enabled`; otherwise answers are labelled "Restricted roadmap reader — not your Hermes session". Execution keeps the existing exact approval, fixed checks and receipts.
- Separate **Stop speaking** (ends the voice turn and late results; native work continues) from **Stop all Agent work**. An explicit switch to Everyday parks approved native work (display hidden, running badge only); Kids, privacy, Meeting/Sleep and emergency stop still revoke it.
- **Advanced voice engine**: Pipeline, Realtime (gpt-realtime-2.1 / 2.1-mini, voice, effort, turn-taking eagerness) and **GPT-Live preview** (gpt-live-1, Marin/Cedar, session limit) with a real bridge relay `/v1/live`, transcript-based delegation, bounded queues and cost/cloud disclosure. Physical echo/barge-in behaviour for Live is unverified.
- Host: `openrouter` provider preset and an independent Everyday text provider (`REACHY_EVERYDAY_PROVIDER`, `REACHY_EVERYDAY_MODEL`, default `gpt-4.1-mini` / `openai/gpt-4.1-mini`). Keys stay on the host.
- Owner-only, text-free `GET /api/voice/timings`; offline `tools/voice_timing_report.py`. PWA shell v79.

### Silent speaker volume
- Settings → Speaker volume adds an explicit Apply slider, measured output level, and mute/unmute. Drafts are not hardware confirmation; failed writes require fresh readback. PWA shell v78.
- MCP adds `get_volume`, `set_volume` and `set_mute` with strict arguments, existing authentication and serialized privacy/Kids/startup/shutdown guards. OAuth consent discloses speaker control.
- Named Linux ALSA playback-only control avoids SDK test tones and capture-reset side effects. No microphone change, wake, movement or default/HDMI fallback; one bounded transaction and readback confirm every change.
- Home Assistant uses the same silent service, retains standalone read-only telemetry, and reports true 0%/muted instead of incorrectly defaulting zero to 100%. Existing telemetry-only deployments remain read-only.

### Daemon-compatible managed updates
- Preserve the existing Python 3.12 Reachy daemon and Home Assistant bootstrap; atomically replace only Homebody's package and distribution metadata through immutable slots.
- Require the installed SDK's exact sleep-pose distance before app stop, in addition to verified folded Standby, disabled torque, owner/CSRF and idle-session admission.
- Keep the updater outside the replaceable app, refuse unverified import/metadata drift and incompatible release Python/dependencies, and retain authenticated exact-identity health plus rollback.
- Preserve serialized safety folding after shutdown while rejecting fresh wakes and ordinary motor enable. Retain all previously accepted Kids, camera, HA and workspace repairs.
- This feature does not publish a stable release or change firmware, SDK, models, pairing, privacy settings or host agent bridge.

### Added

- Owner-only Homebody update notice and explicit install flow with accumulated release notes, persistent Later, progress, and a completion changelog retained until acknowledged (PWA shell v66).
- An independent unprivileged updater for one documented Linux/Python 3.11/Reachy SDK 1.9.x immutable-slot user-service topology. It preserves settings and dependencies, verifies exact release identity and folded Standby startup health, and recovers interrupted or failed jobs to the prior artifact; unresolved recovery is reported as rollback_failed.
- Canonical-repository, draft-release-bound wheel/manifest build workflow (no automatic publication), plus one-time setup and service examples in [managed update operations](docs/managed-updates.md). Target systemd/robot acceptance remains required; no deployment is implied.

### Security

- Hosted agents are now approved per request in Settings on the home network instead of with a shared approval code typed on the public consent page. Settings shows the agent's name, return address, registration age and a match code that the consent page repeats, so a phishing sign-in link cannot borrow an approval meant for another agent, and strangers can no longer burn the owner's code by guessing.
- The OAuth store validates every record on load and drops malformed or orphaned ones instead of raising errors later.
- Hosted-agent sign-in moved to its own listener (default `127.0.0.1:8043`) serving only `/mcp` (OAuth tokens only) and the OAuth endpoints. Before, these shared the dashboard port behind a `Host`-header check, and a proxy that rewrites `Host` (nginx's default `proxy_pass`, `ngrok --host-header=rewrite`, `cloudflared httpHostHeader`) exposed the dashboard API and accepted the static token through the tunnel. Point the tunnel at the new listener.
- Open authorization requests are capped (50 in total, 5 per client, oldest evicted), so an unauthenticated flood no longer grows memory and CPU without bound.
- Never-approved client registrations expire after an hour and are evicted when the 20 slots are full, so strangers can no longer lock the owner out of connecting an agent.
- `POST /api/vision/describe` (the camera card's Ask box) now requires the bridge bearer token. Before, any LAN client could ask the local vision model to describe the room.
- `POST /api/vision/test` needs the current API key to probe an unsaved vision URL, so it can no longer be used to make Reachy fetch from arbitrary hosts. Testing the saved server is unchanged.
- The camera snapshot and agent reminder-delivery routes stay closed when no bridge API key is configured, instead of accepting an empty bearer token.
- Changing the bridge URL or API key from the unauthenticated settings UI now requires the current key. Before this, a LAN client could swap in its own key (unlocking the camera snapshot route) or point the bridge URL at a host it controls and receive the real key. The settings form has a new Current API key field.
- The companion bridge now has its own Kids latch. While a Kids session is live it refuses adult chat, Realtime, and Agent routes with 423 and moderates generic speech, instead of relying only on the Reachy client. Reachy reports session start and end through the new `/v1/kids/session` route. Realtime agent tools now default to off unless the client explicitly requests them.

### Fixed

- Update status and maintenance reads no longer rewrite the private journal or create state/locks. Reads preserve bytes, inode, owner, mode and timestamps, reuse shared locks, and remain possible with read-only access; only mutations use atomic write transactions. This prevents root verification from locking the app and worker out of their 0600 journal.
- Native project approvals use equally strong random hexadecimal identifiers, avoiding intermittent API-key-pattern collisions that could redact an approval ID before it reached the owner UI. Exact-match, expiry, source-drift and single-use checks remain unchanged.
- Kids I Spy reconnects a failed local SDK camera reader with a bounded, consent-aware retry before waking. Awake is serialized and checked against measured hardware readiness, so an already-awake robot does not perform a second wake animation. Stop, privacy transitions and session replacement invalidate startup and search; failures after movement fold safely, while stale cleanup cannot tear down a newer Kids session.
- Camera joystick directions match the Reachy SDK axes. Center no longer waits while holding the admission locks, uses request-scoped cancellation and reports actual completion. Streaming sends combined head/body targets, levels roll at a bounded rate, and rejects nonfinite measured poses.
- Owner camera readiness checks no longer request media during pairing. Explicit viewing can fall back from WebRTC to authenticated private HTTPS frames, with decoded-frame freshness gates, serialized capture, and teardown on Stop, tab exit, backgrounding, Kids or privacy changes. Phone-friendly connection reports retain bounded sanitized diagnostics in browser memory only.
- Camera fullscreen and mobile controls remain readable and accessible when streaming or movement permission changes; every tab has its own heading and compact truthful robot status instead of an oversized shared hero.
- GPIO buttons: a long press is now recognised from its edge timestamps, so holding green or red still means Standby or Sleep when the press and release arrive together after the monitor was busy with an earlier action.
- GPIO buttons: saving new button settings while an action is still running no longer revives the old monitor or double-dispatches presses. Each monitor has its own stop signal, stale queued presses are dropped, and the new monitor waits for the old one to release the lines.
- Realtime camera calls answered by the local vision model run on a worker thread, so a slow or unreachable vision server no longer freezes audio, barge-in and Stop for up to a minute.
- A stopped voice turn can no longer be resumed by a later Awake transition. Before this, preparing an I Spy round cleared the stop request, so an adult or Agent reply that was already in flight could be spoken during Kids Mode.
- One lock order (motor transition, then Kids) is now enforced. Manual, precision, camera-control, and presence paths nested the locks the other way round and could deadlock against `status()`. The HTTP Kids-lock middleware no longer waits on the Kids lock during slow robot transitions.
- Concurrent approvals of the same pending Agent draft now execute it exactly once.
- An abandoned camera-joystick session no longer blocks other features. Before this, closing the tab mid-drag left the session marked active until the next power change or Stop, which blocked presence, gestures, presentation and manual and precision controls. Every check that asks "is camera control active?" now expires a session idle for more than 30 seconds and stops its stream.
- PWA shell advanced to v46 for the settings credential field.
- `tests/test_kids_mode.py::test_runtime_generated_kids_session_id_passes_real_bridge_handler` no longer depends on `OPENAI_API_KEY` being set on the machine.

### Changed

- Native Home Assistant bridge migration can retain the existing device name, node name and MAC through validated host-owned settings. Adult HA controls are blocked during Kids sessions; camera frames require ready Awake state and are rechecked after capture and encoding. HA live viewing remains request-driven MJPEG, not native WebRTC.
- OPERATIONS documents private HTTPS camera fallback, phone connection reports, signaling-port collisions, SDK media side effects, and separate desktop/phone acceptance limits.
- Every collapsible section in the dashboard now opens with the same round + / − key on the right, replacing the browser's default triangle on the Settings sections, Session details and the initiative disclosures. The app page card on Hugging Face uses the orange gradient of the new icon.
- The Home Assistant voice and announcement temporary files now use the `homebody-ha-` and `homebody-announcement-` prefixes, matching the rest of the app.
- Redesigned the dashboard as a cream enclosure with dark displays set into it. The Home tab opens on a device-style display: a large Reachy face whose eyes and antennas follow the runtime state, the state written as a sentence ("Standing by."), the power control, and a microphone, camera, motors and agent telemetry rail. Live-state surfaces on the other tabs (motor state, Agent Mode, Kids session, announcement queue) use the same dark display; controls and settings sit on the enclosure. Tabs move to a numbered top bar on desktop and a fixed bottom bar on phones. Instrument Serif and DM Mono are bundled as subset WOFF2 files, so the page still makes no outside requests, and the app icon was redrawn to match; PWA shell v56. A night palette follows the system dark setting. The Hugging Face landing page (`index.html`, `style.css`) uses the same enclosure-and-display language and the same bundled fonts, so it no longer loads Google Fonts.
- Renamed the app from Reachy Mini Hermes to **Homebody**: package `reachy-mini-homebody` 0.4.0, module `homebody`, Reachy app `reachy_mini_homebody`, class `Homebody` and CLI `homebody`. Hermes Agent remains the reference agent backend alongside OpenClaw.
- The dashboard screenshots in `docs/assets` were retaken with the Homebody header using the new, reproducible `docs/tools/capture_ui_screenshots.py`.
- New defaults `~/.local/share/homebody/config.json`, `~/.cache/homebody/`, `HOMEBODY_CONFIG` and `HOMEBODY_MODEL_DIR`. Pre-rename config, caches and `REACHY_MINI_HERMES_*` variables are still used when present, and `reachy-mini-hermes` remains a CLI alias. See OPERATIONS.md for the upgrade steps.
- The dashboard header, PWA description and status `wake_phrase` now read Homebody. Browser settings saved under the old `reachy-hermes-*` keys (open tab, Kids profile, agent run, announcement draft) carry over to `homebody-*` keys once; PWA shell v54.
- Internal thread, task and temp-file names, and the camera's WebRTC peer name, now use a `homebody-` prefix instead of `reachy-hermes-`; PWA shell v55.
- Home Assistant keeps the same ESPHome device identity and project name; only the displayed model and manufacturer read Homebody.
- Added the local wake phrase **Hey Homebody** alongside Hey Hermes, Okay Nabu and Hey Reachy (BPE tokens verified against the bundled GigaSpeech KWS vocabulary; live-microphone acceptance pending).
- README reworked around the always-on household story, and a CONTRIBUTING guide with the household promises added.
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

### Added

- Optional warm Hermes agents for the pipeline and Realtime `ask_hermes` routes (`REACHY_HERMES_WARM_AGENTS=1`, off by default). The bridge keeps one Hermes agent per conversation instead of the API server building a new one every turn. Turns for the same conversation run one at a time. A cache signature over model, provider, credential hash, toolsets, system prompt, memory scope and Hermes config files rebuilds an agent when any of them change. Idle, age, turn and capacity limits apply, and Kids Mode, failures, cancellation and shutdown close agents. Owner routes `GET`/`DELETE /v1/warm-agents` report per-route hits, cold builds, rebuild reasons, latency and token use. When an agent cannot be built, requests fall back to the API server; a started turn is never replayed.
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
- GitHub Actions workflow that mirrors `main` to the Hugging Face Space behind the Reachy Mini app store listing after CI passes (requires an `HF_TOKEN` repository secret).
- Gesture-bound camera-control API sessions with random identifiers, monotonic anti-replay sequences, bounded finite pan/tilt, cancellable head interpolation, server-owned base assistance, release-to-hold, settings/power/privacy/Kids revocation, and generic-control ownership exclusion.
- Optional ESPHome-native Home Assistant bridge on TCP 6053 with the existing stable Reachy device/entity identity, mDNS discovery, real telemetry, truthful unavailable states, and independently gated controls and camera snapshots.
- Opt-in Home Assistant Assist ownership after local wake detection, including 16 kHz PCM streaming, HA pipeline state/motion cues, bounded same-peer TTS/media playback, announcements, follow-up support, and privacy/disconnect/timeout cancellation.

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
- Local AI: gesture detection now runs on TensorRT/CUDA (Jetson), Core ML (Mac) or DirectML (Windows) when available, with a TensorRT engine cache and automatic CPU fallback (local_ai_accelerator auto|cpu). New optional local vision model: an OpenAI-compatible server such as Ollama on a Jetson Orin Nano answers Realtime camera requests and a camera-card Ask box, so frames stay on your network and only text reaches the conversation (POST /api/vision/describe, /api/vision/test; redirecting the vision server needs the current API key). Kids I Spy can select its object with a local vision server via REACHY_ISPY_VISION_URL on the bridge; target validation and moderation are unchanged.
- Docs: new hardware setups guide (docs/hardware-setups.md) with the supported setups (Lite + PC, Mac, Raspberry Pi, Jetson Orin Nano, and Wireless), a feature-by-setup matrix with reference-tested / supported / experimental labels, where Hermes and local AI can run, and setup notes per option including Jetson steps. README, the Space page, OPERATIONS and SECURITY now match it.
- OpenClaw support: the companion bridge can route Reachy's conversations to an OpenClaw agent through the Gateway's OpenAI-compatible endpoint, instead of or alongside Hermes Agent (--agent-backends hermes,openclaw). Requests only reach allowlisted agents (REACHY_OPENCLAW_AGENTS, default reachy); the owner's main/default agent is refused unless explicitly allowed; x-openclaw-* overrides and client tools are never forwarded; the Gateway token stays on the bridge host. Realtime delegates through ask_openclaw, each Reachy conversation maps to one OpenClaw session, health/models report every backend, and Settings labels OpenClaw agents.
- European model providers: the bridge's own text-model calls (Agent Mode planning and tool loop, Kids chat, I Spy selection/judging/guessing) can go to Cortecs (EU-only routing via eu_native), LLMrouter.eu or any OpenAI-compatible endpoint instead of OpenAI (REACHY_LLM_PROVIDER, REACHY_LLM_MODEL). Each provider gets only its own key; model names are required, never guessed. Realtime voice and Kids Mode moderation deliberately stay with OpenAI. /health reports the active provider and region.
- Agent access (MCP): Homebody can serve a token-protected Model Context Protocol endpoint (/mcp, Streamable HTTP, stateless JSON) so Hermes Agent, OpenClaw, Claude Code and other MCP agents can use Reachy: get_status, announce, express_emotion and (opt-in) look_and_describe via the local vision model. Off by default; every tool goes through the existing Meeting/Sleep/privacy/Kids/motor gates, agents never wake Reachy for a gesture, calls are rate-limited, tokens are stored hashed and managed with the owner's API key, and foreign browser origins are refused. PWA shell v53.
- - **Hosted agents can sign in to agent access (MCP) with OAuth 2.1.** ChatGPT dots, Grok Bot and other cloud agents reach Reachy through an HTTPS tunnel. They use discovery metadata, dynamic client registration, mandatory PKCE, tokens bound to the endpoint, and rotating refresh tokens. The owner approves each agent with a one-time code from Settings. Through the public address only the agent paths exist, and the static token is refused there.
- - **Agent access (MCP) was tested with live clients, and agents now learn why an action is unavailable.** Claude Code, the official TypeScript and Python SDKs, the Python SDK's OAuth client and mcp-remote's OAuth client were run against a live server. Privacy mode was missing from status (`can_announce` read true and the refusal said Meeting/Sleep), `announce` relied only on the runtime's own gate, and agents had to guess why looking was off. Now status carries `privacy_mode` and a `why_not` reason per action, and every action refuses up front in Meeting, Sleep, privacy and child sessions. New `tools/mcp_check.py` (standard library only) checks a real endpoint.
- - **New `tools/llm_provider_check.py` checks a text-model provider (Cortecs, LLMrouter.eu, OpenAI or custom) against every request the bridge makes.** It starts the real bridge in-process and runs I Spy judging and guessing (strict JSON schema), Agent Mode planning (forced tool calls) and answers (tools plus JSON schema with a tool round trip), plus Kids chat and I Spy object picking when moderation is available. Each check reports pass or fail with a reason: a refused key (including LLMrouter.eu's HTTP 500 auth errors), an unlisted model, or a reasoning model cut off by the bridge's short answer budgets. The EU provider docs now warn against reasoning models for I Spy and Kids chat, suggest EU-hosted Mistral models from Cortecs' live catalogue, and fix an example model name that Cortecs does not offer.
- - **OpenClaw was tested against a live Gateway, and the recommended `reachy` agent config is now safe.** On OpenClaw 2026.9.8 the documented deny-list still let the agent read files by absolute path (including the Gateway token), reconfigure OpenClaw through its `openclaw` tool, and reach `web_fetch` and `sessions_spawn` through tool search. The docs now use `profile: "minimal"` and deny `gateway`, `presence` and `session_status`, which leaves an empty tool catalogue. They also mark the Docker sandbox as optional, since without Docker every turn fails, and add a spoken self-check. Verified live: health, models, pipeline and `ask_openclaw` turns, per-conversation sessions, the allowlist and `main` refusal, header stripping (sent directly, the headers switch to `main` with full tools), and Hermes side by side.
- - **Your agent can now connect Reachy itself.** Settings → Connect your agent creates a message with a one-time code for Hermes Agent or OpenClaw. The agent follows a version-matched guide served by the robot. It downloads the bridge from the robot and checks it against a SHA-256 manifest, creates a dedicated tool-restricted Reachy profile or agent, starts the bridge and pairs. Reachy saves the bridge only after reaching it with the agent's key and finding a ready, restricted agent. It can also hand the agent an MCP token so the owner's agent can use Reachy as a tool. Verified with Claude Code acting as the agent against fresh Hermes Agent 0.19 and OpenClaw 2026.9.8 installs. Fixes found on the way: the bridge failed every Hermes 0.19 turn because `/v1/toolsets` now wraps its list; `/health` now reports the Hermes tool boundary; the boundary also blocks `delegate_task`, `cronjob`, `skill_manage`, `computer_use` and raw browser control; MCP clients newer than Homebody were refused with HTTP 400 during `initialize`; and the Hermes setup docs now use a restricted profile with the key in `.env`.
- - **Agent-led setup is now front and centre in the docs.** The README and the app page get a "Your agent sets itself up" section with the three steps, the safety model and a screenshot (`docs/assets/ui-agent-setup.webp`, made by the reproducible capture script). The capability tables, feature list, install steps, Raspberry Pi guide, hardware setups, operations runbook (where the agent keeps the bridge, and refreshing it after an upgrade), MCP guide and contributing guide (how to make a new connector agent-led) all point to it. The setup guides are now safe to repeat: they restart an existing bridge instead of starting a second copy, and they replace the MCP token instead of appending another.
- - **Guided GPIO button acceptance.** New `tools/gpio_acceptance.py` has three parts. `preflight` checks gpiod v2, chip access, the gpio group and line owners. `wiring` tests each button's idle level, clean presses, contact bounce and crosstalk on raw edges. `run` walks an adult through every runbook step from any LAN computer and confirms each from the app's own state, writing a PASS/FAIL report. It covers green listen and wake, red stop during an answer, red long to Sleep, green long to Standby, red ending Kids Mode with a fold, and a button held while the lines open, plus the stuck lockout with `--stuck`. `/api/gpio/status` now numbers each gesture with its time and result, and counts each button's presses and ignored presses.

### Build

- Distribution metadata matches the existing registered `reachy_mini_homebody` app ID. GitHub README stays free of Hugging Face card front matter; Space export adds the canonical metadata separately.
- The CI example is now an active GitHub Actions workflow with a complete dependency set. The old list was missing `aioesphomeapi`, `onnxruntime`, `sherpa-onnx` and others, so 18 test modules failed to import. The workflow runs ruff, JavaScript syntax checks, the full suite on Python 3.11 and 3.12, and the package build.

### Verified

- Regression coverage includes camera lifecycle and measured-pose cancellation, owner-protected transport, Home Assistant identity/privacy, and Kids startup recovery. Exact local, target-SDK, browser and physical acceptance results are recorded with the corresponding repair pull requests; passing synthetic tests is not a claim of phone WebRTC or updater deployment acceptance.

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
