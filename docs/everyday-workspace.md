# Everyday, Workspace and voice engines

Homebody separates **what a voice turn may do** (assistant mode) from **how speech travels**
(voice engine, under Settings → Advanced). Kids Mode keeps its own dedicated route and is
checked before either.

| | Everyday | Workspace |
|---|---|---|
| Entered by | Default; Agent tab → Everyday | Agent tab → Workspace (adult, paired owner) |
| Backend | Host Everyday tools + fast text model | Bound native Hermes session (if enabled) or the restricted broker |
| Can see | Current turn, allowlisted HA state, configured weather, host clock | Selected project roadmap, native session history |
| Cannot | Hermes agent, private memory, notes, projects, coding or unrestricted fallback | Run work without exact phone approval |

## Everyday (host side)

Configured on the Hermes host in the bridge environment. Nothing below reaches the robot or browser
except names/labels in `/v1/everyday/capabilities`.

| Variable | Meaning |
|---|---|
| `REACHY_EVERYDAY_PROVIDER` | `openai` (default, or `REACHY_LLM_PROVIDER`), `openrouter`, `cortecs`, `llmrouter`, `ollama`, `custom` |
| `REACHY_EVERYDAY_MODEL` | Defaults: `gpt-4.1-mini` (OpenAI), `openai/gpt-4.1-mini` (OpenRouter). Other routers must set it |
| `REACHY_EVERYDAY_URL` | Optional OpenAI-compatible `/v1` URL override |
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | Resolved on the host only, by preset key name |
| `OLLAMA_API_KEY` | Optional only for an authenticated Ollama proxy; ordinary local Ollama needs no key. Default URL: `http://127.0.0.1:11434/v1`; explicitly choose an installed `REACHY_EVERYDAY_MODEL` |
| `REACHY_AGENT_HA_ALLOWLIST` / `REACHY_AGENT_HA_ACTION_ALLOWLIST` | Existing allowlists, reused unchanged. On/off for lights and switches; brightness for permitted lights; scenes only with an exact existing `scene.*` → `turn_on` entry. Locks, covers, alarms and climate stay refused. |
| `REACHY_EVERYDAY_ALIASES` | `{"light.desk": ["desk light"]}` spoken names for allowlisted entities only; ambiguous or unknown names are returned for the model to ask about |
| `REACHY_EVERYDAY_TEMPERATURE_ENTITIES` | JSON list of up to 16 unique indoor `sensor.*` IDs, also present in the HA read allowlist; no auto-discovery or added control authority |
| `REACHY_EVERYDAY_WEATHER_ENTITY` | Preferred: a Home Assistant `weather.*` entity (state + `weather.get_forecasts`). Freshness is reported; data older than 3 h is marked stale |
| `REACHY_EVERYDAY_WEATHER_PROVIDER=open-meteo` + `REACHY_EVERYDAY_WEATHER_LATLON=lat,lon` | Optional public provider; the rounded coordinates are sent to api.open-meteo.com and the UI discloses that. No auto-geolocation |
| `REACHY_EVERYDAY_TIMEZONE` | IANA zone for the host-clock time tool (default: host local time) |
| `REACHY_AGENT_REMINDER_CALLBACK_URL` / `REACHY_AGENT_REMINDER_CALLBACK_TOKEN` | Existing authenticated robot reminder-delivery callback; scheduling is unavailable without it. No callbacks are provisioned by this implementation. |
| `REACHY_EVERYDAY_SEARCH_PROVIDER=brave` / `REACHY_EVERYDAY_SEARCH_API_KEY` | Optional public snippet search at the fixed Brave HTTPS endpoint. Queries leave the host. No redirects, arbitrary URL fetches, private retrieval or automatic credential copying. |

Budgets per user request: three tool calls, one device change, call ids executed at most once,
HA 4 s (+2 s verification), weather 3 s, text turn 20 s. Kids Mode blocks every tool.

For Windows/Mac/Linux Ollama setup, caller-relative loopback versus LAN URLs, firewall scope and Pipeline versus cloud speech boundaries, see [Ollama setup](ollama.md). Model download, availability and tool competence are separate from configuration readiness.

Timers run for up to one day, reminders up to 30 days. They are scoped to the adult device
in host RAM, so another wake or voice engine can inspect/cancel them, but another device
cannot. Fresh original-context authority is still required for every admitted operation.
Host restart clears schedules; Meeting/Sleep/privacy/Kids may block spoken delivery.
Creation verifies scheduling, not audible completion. Scene verification confirms a new
activation timestamp, not the resulting state of every member. Brightness/scenes have no
automatic undo. Search returns bounded source URLs, snippets and retrieval timestamps;
provider configuration is not proof of live provider access.

### Indoor temperatures

`get_home_temperature` reads only `REACHY_EVERYDAY_TEMPERATURE_ENTITIES`, resolves exact
aliases from `REACHY_EVERYDAY_ALIASES`, and computes an equal-weight current sensor mean
in Celsius. An empty `names` list selects that configured set; explicit names select a
subset, without duplicates. Choose one representative sensor per room; do not include
outdoor, appliance, battery or NAS temperatures in an indoor average.

For each sensor, allow `device_class`, `unit_of_measurement`, and `last_updated` in
`REACHY_AGENT_HA_ALLOWLIST`. The last field opts into its HA observation timestamp.
Only finite temperature-class readings in °C/°F with a timezone-bearing observation
no older than six hours are included. Missing/invalid/future timestamps and unavailable
readings are excluded, with source names, included/requested counts and partial coverage.
No usable readings returns no average, never zero. This is neither a historical average
nor a claim to measure the entire home. No action allowlist changes are needed.

## Expanded Everyday robot tools (development candidate)

Everyday now has one canonical local tool schema in `companion/everyday_manifest.py` and a
single robot executor in `homebody/everyday_runtime.py`. Pipeline, Realtime and GPT-Live
use the same power, status, volume, camera and semantic-movement handlers. This source
candidate is not a deployed release or physical voice/camera acceptance.

- **Power:** Standby, Awake, timed Meeting and Sleep use the existing serialized safe-fold
  handler. Standby/Meeting/Sleep end the conversation; Sleep cannot be woken by voice.
- **Status and speaker:** categorical microphone-recording, motor and power status; verified
  playback-only volume/mute, never microphone mixer changes or a test sound.
- **Look once:** one fresh frame, a specific question, then discard. Local vision is preferred
  when configured; otherwise the explicitly requested frame goes through the host's narrow
  Everyday vision endpoint. Images and visible text are untrusted data, not instructions.
- **Camera permission:** one Owner-controlled **Camera access for AI** (`camera_enabled`,
  default false) covers explicit looks and short sessions in every voice engine. There is no
  separate voice-camera switch or repeated Owner approval. Signing in never enables or starts it.
  Legacy temporary-only consent never silently enables this broader permission.
- **Camera session:** an explicit spoken Start validates a fresh frame, then opens an app capture window
  for 5–120 seconds. There is no automatic capture loop, cloud upload, tracking or recording.
  Camera off, Stop, permission revocation and lifecycle changes invalidate pending frames.
  This is app capture access, **not physical power control of the daemon-owned shared camera**.
  Saved general-camera, viewer, tracking and movement settings are not changed by a voice tool.
- **Movement:** existing bounded head direction, recorded emotion and dance actions, with
  original voice-session cancellation checked by the action worker before moving.

The Everyday card stays compact: selected voice engine and a short capability summary,
one **Manage permissions** disclosure with grouped readiness/activity, and technical provider
and entity details inside that disclosure. It does not render a switch for every tool.
Camera consent reuses Settings and stays off until the paired owner explicitly saves it.

Robot callbacks carry one-shot call IDs and the original stop/profile generation. Local
camera/network/action work does not block the Live/Realtime microphone pump. Expired,
superseded, replayed, private, Kids and Workspace calls fail closed; there is no shell,
raw motor API, credential access or full-Hermes fallback.

## Workspace native discussion

A Workspace discussion turn is a real run in the **bound** native Hermes session
(`POST /v1/runs {session_id, input, instructions}` then `GET /v1/runs/{id}` until terminal).
The answer is accepted only when the run reports the same `session_id` and `run_id`; its
provenance (`native_hermes`, session, run) is shown in the conversation view.

A native discussion turn runs with that worker's configured tools; the "discuss only"
instruction is a request, not enforcement. It is therefore **off unless the host owner opts in**
per target:

```json
REACHY_NATIVE_TARGETS_JSON='{"photo": {"backend": "hermes", "url": "http://127.0.0.1:8642",
  "token_env": "PHOTO_HERMES_KEY", "project_id": "photo", "session_id": "<existing session id>",
  "discussion_enabled": true}}'
```

Setup recipe (host owner, not done by this change):
1. Create or choose a Hermes API-server profile whose toolset is read-only for the project, and an
   existing session for it. Register the project in `REACHY_AGENT_PROJECTS_JSON`.
2. Add the target above with the session id and a host-only token variable; restart the bridge.
3. In Agent → Workspace, select that project/session. Execution additionally needs
   `execution_enabled`, fixed `verification_commands` and `artifacts` (see `native-workspace.md`).

Without a registered target Workspace shows the honest unavailable state; without
`discussion_enabled` answers come from the restricted broker and are labelled as such.

## Stop and mode switches

| Action | Voice | Native work |
|---|---|---|
| Stop speaking / barge-in | stops now; late results discarded | continues |
| Switch Workspace → Everyday | new generation | parked: keeps running, display hidden, badge only |
| Switch back to Workspace | — | un-parked; Show to view again |
| Stop project work (Workspace card) | — | stopped, binding kept |
| Stop all Agent work, Kids, privacy, Meeting/Sleep | stops | revoked/stopped (unchanged) |
| Clear & disconnect (confirmed) | display cleared | stopped and disconnected; Show + Open project again |

In the Agent tab, Everyday shows only its home/voice card and Companion. The project picker, request
forms, Advanced home actions and session details appear only once status confirms Workspace. In
Everyday, Stop all Agent work is shown while the status-only badge reports running work or cannot be
checked, and hidden only when it confirms none. Private Agent polls pause during a mode switch and
late replies from the previous mode are dropped; the server's 423 checks are unchanged.

## Voice engines

- **Pipeline**: STT → text → TTS. New Everyday turns use authenticated
  `/v1/everyday/session` with bounded local callback/result events. `/v1/everyday/turn`
  remains the text-only compatibility endpoint for older clients.
- **Realtime**: `gpt-realtime-2.1` or `-mini`; Everyday sessions get only Everyday, power and robot
  tools (no `ask_hermes`); tool calls run on the host.
- **GPT-Live preview**: `/v1/live` relays to `wss://api.openai.com/v1/live/sessions` with
  `store:false` and client delegation. The bridge rebuilds each delegated request from transcript
  deltas (corrections, late delivery grace, task revisions), answers with
  `session.commentary.append` (≤480 UTF-8 bytes) and records superseded results as
  `session.thinking.append`. Everyday robot callbacks stay on the robot↔bridge socket and
  are never forwarded as provider protocol events. No private memory tools. **Echo cancellation and physical
  barge-in are unverified**; with barge-in off the microphone is not streamed while Reachy speaks.

## Timing

`GET /api/voice/timings` (paired owner, no-store) returns the last 50 turns as text-free offsets:
`session_ready`, `local_speech_end`, `provider_speech_end`, `tool_requested`, `tool_result`,
`first_provider_audio`, `player_enqueued` (SDK enqueue, not "heard"), `playback_estimated`,
`turn_end`, with engine, mode and cold/warm. Summarize a saved copy with
`python tools/voice_timing_report.py timings.json`.

## Deployment and rollback

1. Bridge first (additive; old robots that send no `assistant_mode` keep legacy routing): back up
   `~/.hermes/homebody-bridge`, copy the new `companion/` files, add Everyday env vars, restart the
   bridge, check `/health` and `/v1/everyday/capabilities`. Roll back by restoring the backup.
2. Robot wheel through the managed updater into a new slot; roll back to the previous slot. Older
   wheels ignore the new config keys.
3. After the robot update, ordinary voice uses Everyday. Choose Realtime or GPT-Live in Advanced
   only after supervised tests.
