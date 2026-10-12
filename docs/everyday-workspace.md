# Everyday, Workspace and voice engines

Homebody separates **what a voice turn may do** (assistant mode) from **how speech travels**
(voice engine, under Settings → Advanced). Kids Mode keeps its own dedicated route and is
checked before either.

| | Everyday | Workspace |
|---|---|---|
| Entered by | Default; Agent tab → Everyday | Agent tab → Workspace (adult, paired owner) |
| Backend | Host Everyday tools + fast text model | Bound native Hermes session (if enabled) or the restricted broker |
| Can see | Current turn, allowlisted HA state, configured weather, host clock | Selected project roadmap, native session history |
| Cannot | Hermes agent, memory, notes, projects, coding, camera via Live, any fallback | Run work without exact phone approval |

## Everyday (host side)

Configured on the Hermes host in the bridge environment. Nothing below reaches the robot or browser
except names/labels in `/v1/everyday/capabilities`.

| Variable | Meaning |
|---|---|
| `REACHY_EVERYDAY_PROVIDER` | `openai` (default, or `REACHY_LLM_PROVIDER`), `openrouter`, `cortecs`, `llmrouter`, `custom` |
| `REACHY_EVERYDAY_MODEL` | Defaults: `gpt-4.1-mini` (OpenAI), `openai/gpt-4.1-mini` (OpenRouter). Other routers must set it |
| `REACHY_EVERYDAY_URL` | Optional OpenAI-compatible `/v1` URL override |
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | Resolved on the host only, by preset key name |
| `REACHY_AGENT_HA_ALLOWLIST` / `REACHY_AGENT_HA_ACTION_ALLOWLIST` | Existing allowlists, reused unchanged. Everyday controls only `light.*`/`switch.*` `turn_on`/`turn_off` and verifies the new state; locks, covers, alarms and climate are refused |
| `REACHY_EVERYDAY_ALIASES` | `{"light.desk": ["desk light"]}` spoken names for allowlisted entities only; ambiguous or unknown names are returned for the model to ask about |
| `REACHY_EVERYDAY_WEATHER_ENTITY` | Preferred: a Home Assistant `weather.*` entity (state + `weather.get_forecasts`). Freshness is reported; data older than 3 h is marked stale |
| `REACHY_EVERYDAY_WEATHER_PROVIDER=open-meteo` + `REACHY_EVERYDAY_WEATHER_LATLON=lat,lon` | Optional public provider; the rounded coordinates are sent to api.open-meteo.com and the UI discloses that. No auto-geolocation |
| `REACHY_EVERYDAY_TIMEZONE` | IANA zone for the host-clock time tool (default: host local time) |

Budgets per user request: three tool calls, one device change, call ids executed at most once,
HA 4 s (+2 s verification), weather 3 s, text turn 20 s. Kids Mode blocks every tool.

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

## Voice engines

- **Pipeline**: STT → text → TTS. Everyday turns use `/v1/everyday/turn`.
- **Realtime**: `gpt-realtime-2.1` or `-mini`; Everyday sessions get only Everyday, power and robot
  tools (no `ask_hermes`); tool calls run on the host.
- **GPT-Live preview**: `/v1/live` relays to `wss://api.openai.com/v1/live/sessions` with
  `store:false` and client delegation. The bridge rebuilds each delegated request from transcript
  deltas (corrections, late delivery grace, task revisions), answers with
  `session.commentary.append` (≤480 UTF-8 bytes) and records superseded results as
  `session.thinking.append`. No camera, movement or memory tools. **Echo cancellation and physical
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
