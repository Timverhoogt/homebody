# Settings structure

Settings is one page with one visible category at a time. The markup lives in
`homebody/static/index.html` (`#panel-settings`), the navigation controller in
`homebody/static/settings-layout.js`, the styling in the Settings block of `homebody/static/fable.css`.

## Categories

| Key | Label | Purpose |
| --- | --- | --- |
| `everyday` | Everyday | Everyday preferences: how Reachy converses and moves, presence and suggestions. First view. |
| `voice` | Voice & sound | Speaker volume, language, voice engine, speech recognition and voices, timing tuning. |
| `privacy` | Privacy & access | Camera access grants, household permissions, read-only "what Everyday can use". |
| `connections` | Connections | Agent connection status, agent bridge, one-time agent setup, local vision, MCP, Home Assistant. |
| `system` | System | Reachy software updates, remembered devices, home-screen shortcut, app and host lifecycle. |

Desktop (≥900px) shows a side list of buttons (`[data-settings-nav]`, `aria-current="page"` on the
active one, Arrow/Home/End keys move between them). Phones show one labelled `<select
id="settings-category-select">`. Both drive the same controller. The last category is remembered in
`localStorage` (`homebody-settings-category`) and restored on load; restoring never writes a setting.

## Control ownership

Every interactive control from the pre-redesign inventory
(`tests/fixtures/settings-controls.json`) exists exactly once. "Form" means the
control is submitted by the single Save (`#settings-form`); fields outside the `<form>` element carry
`form="settings-form"`. "Own" means the control keeps its own button or explicit-change save.

### Everyday
- Conversation and motion (form): `continuous_conversation`, `barge_in_enabled`, `motion_enabled`,
  `face_tracking_enabled` (+ `face-tracking-status`). Movement tuning (`doa_enabled`,
  `face_tracking_weight`) is secondary; agent movement permission lives under Privacy & access.
- Presence and suggestions (own, saved on change as before): `presence-enabled`,
  `presence-acknowledgement-enabled`, `companion-suggestions-level`.
- Disclosure "Sources & activity" (`companion-details`): the Presence/Camera/Microphone readout
  (`companion-grid`), `companion-why`, source catalog, enrollment fields `companion-calendar`,
  `companion-calendar-lead`, `companion-calendar-disclosure`, `companion-device`,
  `companion-device-value`, `companion-device-offer`, `companion-device-accepted`,
  `companion-enroll-save`, `companion-enroll-clear`, receipts.
- Disclosure "Advanced presence options" (`companion-advanced`): Proactive Presence status,
  Initiative & Offers (`initiative-policy-enabled`, `initiative-mode`, `initiative-preferences-reset`,
  quiet hours and budgets, `contextual-offers-enabled`, `contextual-offer-response-window`,
  `contextual-offer-yes|later|no`), Show Reachy something (`shared-physical-context-enabled`,
  `presentation-window-seconds`, `presentation-start`, `presentation-stop`, readiness list).

### Voice & sound
- Speaker volume (own): `speaker-volume`, `speaker-apply`, `speaker-mute`, `speaker-refresh`.
- Language and voice engine (form, inside the `<form>` element): `language`, `conversation_mode`,
  `engine-disclosure`, `realtime-settings` (`realtime_model`, `realtime_semantic_eagerness`,
  `realtime_voice`, `realtime_reasoning_effort`), `live-settings` (`live_model`, `live_voice`,
  `live_session_timeout_seconds`), `kids-speech-scope`. Realtime voice remains directly editable;
  model, eagerness and reasoning effort are under Realtime tuning.
- Disclosure "Speech recognition and spoken voices" (closed by default): `pipeline-settings`
  (`stt_provider`, `stt_model`, `stt_robot_fallback`, `tts_provider`, `tts_model`), `robot-stt-note`,
  `tts_voice`, `voice-provider`.
- Disclosure "Advanced voice timing": `end_silence_seconds`, `max_utterance_seconds`, `vad_min_rms`,
  `vad_noise_multiplier`, `wake_keyword_threshold`, `wake_keyword_score`.
- The engine-conditional containers keep their IDs; `toggleModePanels()` in main.js still owns
  their `hidden` state.

### Privacy & access
- Agent movement access (form): `robot_tools_enabled`.
- Camera access (form): `camera_enabled`, `camera_feed_enabled`, `camera_controls_enabled`,
  `camera_controls_handedness`. Concise help keeps the privacy facts: nothing starts capture, a look
  needs an explicit request, one frame may go to a cloud vision provider unless local vision is set,
  Homebody keeps no images, provider retention is the provider's, local face following is separate.
- Household permissions (`household-card`, own, owner-only as before): `household-camera_on_request`,
  `household-following_and_movement`, `household-voice_power_controls`, `household-kids_ispy_camera`,
  `household-ack`, `household-approve`, `household-camera-off`, `household-revoke`, standalone
  connection `household-url`, `household-credential`, `household-durable`, `household-connection-ack`,
  `household-connection-save|validate|connect|disconnect`.
- What Everyday can use (`everyday-settings-card`, read-only): `everyday-permissions` disclosure with
  the robot/camera/household/information readouts and the `data-everyday-setting` links, then
  `everyday-technical-details` holding the Everyday connection status (`everyday-card`), selected voice
  engine, weather, text backend, time source, entity IDs.

### Connections
- Agent connection (`agent-connection-details`): status detail, `agent-connection-retry`.
- Agent bridge (form): `bridge_url` (required), `api_key`, `current_api_key`, `model`, `test-button`.
- Disclosure "Connect a new agent · one-time setup" (`agent-setup-details`): `agent-setup-card`
  (`agent-setup-backend`, `agent-setup-mcp`, `agent-setup-button`, `agent-setup-cancel`,
  `agent-setup-message`, `agent-setup-copy`) and `setup-review-open`.
- Disclosure "Local vision and AI acceleration" (form): `local_vision_enabled`, `local_vision_url`,
  `local_vision_model`, `local_ai_accelerator`, `local-vision-test-button`.
- Disclosure "Agent access (MCP)" (form + own): `mcp_enabled`, `mcp_vision_enabled`, `mcp-endpoint`,
  `mcp-token-button`, `mcp-revoke-button`, `mcp-token`, `mcp_public_url`, `mcp_public_bind`,
  `mcp_public_port`, `mcp_oauth_enabled`, `mcp-oauth-disconnect-button`, pending approvals list,
  hosted agents list.
- Disclosure "Home Assistant" (form): `home_assistant_enabled`, `home_assistant_controls_enabled`,
  `home_assistant_camera_enabled`, `home_assistant_assist_enabled`, `home_assistant_port`.
- `assistant-mode-notice` (+ `assistant-mode-notice-ack`) stays here as a routing migration notice.

### System
- Reachy software updates (`updates-card`): `update-check`, `update-preview`, rail, status, history.
- Remembered devices (`owner-device-controls`: `owner-logout`, `owner-devices-refresh`) and the
  home-screen shortcut (`install-card`: `install-button`) are moved in front of `lifecycle-card` by
  main.js at load, as before.
- App and host (`lifecycle-card`): `app-off-button`, `shutdown-button`.

Legacy route and Stop controls (`agent-conversation-button`, `agent-enable-button`,
`voice-stop-button`, `agent-stop-button`, `agent-approve-button`) retain their IDs/handlers outside
the new category hierarchy. Control preservation is checked against the full DOM, not just categories.

## Save semantics

- One `Save settings` button (`#settings-save-button`, `type="submit" form="settings-form"`) sits
  under the visible category whenever that category contains settings-form fields (all categories
  except System). It submits the unchanged `payloadFromForm()` to `/api/settings`; the payload is
  collected by ID, so it covers every category, including unsaved drafts on other categories. The
  help text under the button says so.
- `form-message` lives next to the button; existing notification feedback surfaces Save success/errors
  as a toast, with inline text retained as its fallback.
- Drafts survive category switches: switching only toggles `hidden`.
- Browser validation (`bridge_url` is `required`, URL fields) fires `invalid` on fields that may be in
  a hidden category or a closed disclosure. The controller listens on the panel in the capture phase,
  reveals the field and writes "Not saved: check the highlighted field." so the native message lands
  on a visible control.
- Nothing gained an autosave. Presence, suggestions, speaker volume, household choices, companion
  enrollment, MCP tokens, initiative and presentation keep their existing buttons or explicit-change
  handlers.

## Navigation API

`window.HomebodySettings`:

- `reveal(controlOrId, {focus = true})`: selects the containing category, opens every ancestor
  `<details>`, scrolls the control into view and focuses it (or its first focusable child). Returns
  the element or `null` when it is not inside Settings. No writes.
- `select(key, {userInitiated = false})`: shows one category; user-initiated switches scroll to the
  top of the page.
- `current()`: the active category key.

Callers wired through it: `goToControl()` in main.js (presentation readiness links,
`data-everyday-setting` buttons from the Everyday and Settings pages), the camera controls link in
camera.js, the onboarding "Open settings" button (`bridge_url`), and the agent connection badge
(`agent-connection-details`). Each still activates the Settings tab through the existing tab button.

## Removed versus moved

Removed (presentation only, verified duplicates):
- The seven-link settings index and the four `<details>` task groups built by the old
  settings-layout.js (replaced by the category navigation).
- `<p class="label">` eyebrows inside Settings sections (already hidden by CSS).
- Duplicate headings: "Agent connection and voice" card heading and its bridge paragraph (now the
  Agent bridge block), "Everyday permissions" heading above "What Everyday can use", the second
  "Allowed does not mean active" note, the separate "Updates Homebody and this dashboard" line (folded
  into the update requirements disclosure).
- Nested `.card` styling inside Settings (advanced presence sub-cards, Everyday connection card are
  plain subsections now).

Moved, not removed: everything listed under control ownership above. Status readouts
(`companion-grid`, `companion-why`, Everyday connection status, selected voice engine) moved into
secondary disclosures.

## Deferred

- The MCP pending-approval list stays inside the closed "Agent access (MCP)" disclosure as it did
  before. Auto-opening it when a request is pending would need a main.js change beyond navigation glue.
- The "Speech recognition and spoken voices" disclosure is open by default so Pipeline-engine fields
  are never hidden; it does not react to engine changes.
- Visual verification (phone 320/390, desktop 1440, link paths, Save fixture) is owned by the parent.
