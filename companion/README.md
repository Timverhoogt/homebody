# Hermes Reachy companion bridge

The companion bridge gives Reachy one authenticated endpoint for:

- Hermes API Server chat, OpenClaw Gateway chat, or both (see [Use OpenClaw](#use-openclaw-instead-of-or-besides-hermes));
- model and voice discovery;
- configured or explicitly selected STT/TTS providers;
- a private OpenAI Realtime WebSocket;
- `ask_hermes` delegation from Realtime back into the user's Hermes agent.
- the fixed owner-scoped Agent broker: read tools, reversible home actions, timers/reminders, media, drafts, exact approvals, cancellation, and a sanitized timeline.
- local Realtime power-mode calls for Standby, Awake, timed Meeting, and Sleep.
- pass-through of on-demand Reachy camera frames to Realtime image input.

Provider credentials stay on the Hermes host. Reachy stores only the bridge URL and the private `API_SERVER_KEY` bearer token.

## Prerequisites

```bash
hermes config set API_SERVER_ENABLED true
hermes config set API_SERVER_KEY 'use-a-long-random-secret'
hermes gateway restart
```

Verify Hermes itself:

```bash
curl http://127.0.0.1:8642/health
```

For Realtime mode, add a direct OpenAI project key to the active profile's `.env`:

```bash
OPENAI_API_KEY=your-openai-project-key
```

The OpenAI account must have API billing and access to `gpt-realtime-2.1`. A ChatGPT subscription alone is not an API credential.

Agent Mode is fail-closed: every read/action allowlist and reminder callback defaults empty. Configure only the resources Reachy may use in the bridge service environment:

```bash
HASS_URL=http://homeassistant.internal:8123
HASS_TOKEN=host-only-long-lived-token
REACHY_AGENT_HA_ALLOWLIST='{"sensor.living_room":["friendly_name","unit_of_measurement"]}'
REACHY_AGENT_HA_ACTION_ALLOWLIST='{"light.desk":["turn_on","turn_off"]}'
REACHY_AGENT_MEDIA_ALLOWLIST='["media_player.living_room"]'
REACHY_AGENT_CALENDAR_ALLOWLIST='["calendar.personal"]'
REACHY_AGENT_NOTIFY_ALLOWLIST='{"mobile_app":["tim"]}'
REACHY_AGENT_NOTE_ROOTS='{"notes":"/absolute/path/to/reachy-notes"}'
REACHY_AGENT_PERSONAL_ROOTS='{"memory":"/absolute/path/to/scoped-memory"}'
REACHY_AGENT_HISTORY_ROOTS='{"history":"/absolute/path/to/scoped-history"}'
REACHY_AGENT_SEARCH_URL=http://127.0.0.1:8888/search
REACHY_AGENT_MODEL=gpt-5-mini
REACHY_AGENT_ASK_TIMEOUT_SECONDS=80
REACHY_AGENT_REMINDER_CALLBACK_URL=https://reachy-private-host
REACHY_AGENT_REMINDER_CALLBACK_TOKEN=the-same-private-bridge-bearer-token
```

Roots must be absolute. Note reads and approved appends reject traversal, symlinks, hardlinks, non-regular files, unsupported extensions, and oversized files. Home Assistant returns or controls only explicit allowlists; its token never enters a result. Timers/reminders are accepted only when the authenticated callback is configured. Media and calendar/message/note writes pause in a five-minute exact-action sheet on the trusted phone UI; approval is one-shot and content edits require a new draft.

## Run the bridge

Use the Python environment that belongs to Hermes Agent:

```bash
cd ~/.hermes/hermes-agent
venv/bin/python /path/to/homebody/companion/hermes_reachy_bridge.py \
  --host 0.0.0.0 \
  --port 8643
```

The bridge resolves secrets from the selected Hermes profile's environment, `.env`, and configuration. For another profile:

```bash
venv/bin/python /path/to/hermes_reachy_bridge.py \
  --profile my-profile \
  --host 0.0.0.0
```

Configure Reachy with:

```text
Bridge URL: http://<hermes-host-LAN-IP>:8643
API key:    the same API_SERVER_KEY
```

## Warm Hermes agents (optional)

Hermes' API server builds a new agent for every request. Each spoken turn therefore pays again for provider resolution, tool discovery, memory start-up and a freshly assembled system prompt. The bridge can instead keep one warm Hermes agent per conversation, the way Hermes' own messaging gateway does, for the two routes someone is waiting on:

- `pipeline`: the Hermes pipeline mode's `/v1/chat/completions` turns;
- `realtime`: Realtime `ask_hermes` delegations.

It is off by default. To turn it on, add this to the bridge's environment and restart it:

```bash
REACHY_HERMES_WARM_AGENTS=1
```

Warm agents run inside the bridge, so the bridge must run from Hermes Agent's own virtualenv, as shown above. They use the same configuration, toolsets, session store and memory scope as the API server. The API server stays the fallback: when Hermes cannot be imported, an agent cannot be built, or a request is anything other than the plain Reachy shape (the default model alias, system messages and one user message, plus a session id), the request goes over HTTP as before. A turn that has already started is never retried over HTTP, because it may have used tools.

| Setting | Default | Meaning |
|---|---|---|
| `REACHY_HERMES_WARM_ROUTES` | `pipeline,realtime` | Which routes may use warm agents. |
| `REACHY_HERMES_WARM_MODELS` | `hermes-agent` | Model aliases that mean "the configured Hermes agent". Other model ids keep the API server's routing. |
| `REACHY_HERMES_WARM_IDLE_SECONDS` | `600` | Close an agent after this long without a turn (30–3600). |
| `REACHY_HERMES_WARM_MAX_AGE_SECONDS` | `3600` | Rebuild an agent after this long (300–86400). |
| `REACHY_HERMES_WARM_MAX_TURNS` | `40` | Rebuild after this many turns (1–200). |
| `REACHY_HERMES_WARM_MAX_AGENTS` | `4` | Most warm agents at once; the least recently used idle one goes first (1–16). |
| `REACHY_HERMES_WARM_QUEUE_SECONDS` | `30` | How long a second turn for the same conversation waits before HTTP 429 (1–120). |

How it stays safe:

- **One turn at a time per conversation.** Turns for the same session queue; different sessions and routes never share an agent.
- **Cache signatures.** Before every turn the bridge fingerprints the model, provider, a hash of the credential, the enabled toolsets, the system prompt, the memory scope and the Hermes `config.yaml`, `.env` and `SOUL.md`. Any change rebuilds the agent first.
- **The tool boundary still applies.** The API server's `/v1/toolsets` check runs before each turn, and every new agent's actual tool list is checked too. An agent with `terminal`, `read_file` or another broad tool is closed unused.
- **Lifecycle.** A failed, timed-out or cancelled turn interrupts and retires its agent. Starting a Kids session closes every warm agent. Shutdown closes them all, and an agent mid-turn is closed only when its turn has finished.
- **Default profile only.** With `--profile` the bridge keeps the per-request API server path, because in-process Hermes reads its home directory once at import.

Owner status and controls:

```bash
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8643/v1/warm-agents
curl -s -X DELETE -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8643/v1/warm-agents
```

The status shows per-route turns, warm hits, cold builds, rebuild and eviction reasons, fallbacks, build and turn seconds, and token use. Sessions appear only as short hashes, with no prompts, transcripts or credentials. Pipeline responses also carry `X-Reachy-Warm-Agent: hit`, or the reason the agent was built (`cold`, `signature`, `idle`, `age`, `turns`).

Warm agents use internal Hermes Agent interfaces that can change between releases. After a Hermes update, check `/v1/warm-agents` reports `"available": true`, and time a few pipeline and `ask_hermes` turns against the plain API server before relying on it.

## Use OpenClaw instead of, or besides, Hermes

The bridge can send Reachy's conversations to an [OpenClaw](https://docs.openclaw.ai) agent through the Gateway's OpenAI-compatible endpoint. Use it on its own, or next to Hermes Agent and switch between them in Reachy's Settings.

### Safety model

**An OpenClaw Gateway token is an owner/operator credential.** Anyone who can speak to Reachy is talking to the agent behind it. For Hermes, the bridge checks the agent's tool inventory before every request. OpenClaw offers no equivalent check over HTTP, so the bridge fails closed in other ways:

- Reachy only reaches agents you list in `REACHY_OPENCLAW_AGENTS`, `reachy` by default. Whatever model id the app sends, the request goes to an allowlisted agent.
- The owner's `main` or `default` agent is refused unless you set `REACHY_OPENCLAW_ALLOW_PRIMARY_AGENT=1`. Don't, unless that agent's tools are already safe for a room full of voices.
- `x-openclaw-*` override headers, client tools and streaming are never forwarded. The Gateway token stays on the bridge host and Reachy never receives it.

### 1. Prepare OpenClaw

Create a dedicated agent for Reachy and restrict its tools and sandbox:

```bash
openclaw agents add reachy
```

Then edit `~/.openclaw/openclaw.json` (JSON5). Keep your other entries as they are, enable the chat endpoint, and restrict the `reachy` agent:

```json5
{
  gateway: { http: { endpoints: { chatCompletions: { enabled: true } } } },
  agents: {
    entries: {
      reachy: {
        name: "Reachy",
        sandbox: { mode: "all", scope: "agent" },
        tools: { deny: ["exec", "process", "write", "edit", "apply_patch", "browser", "gateway"] },
      },
    },
  },
}
```

Exact field names can differ between OpenClaw versions; check with `openclaw agents list` and the OpenClaw [multi-agent sandbox and tools](https://docs.openclaw.ai/tools/multi-agent-sandbox-tools) guide. Restart the Gateway, then confirm the agent is offered:

```bash
curl -s http://127.0.0.1:18789/v1/models -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN"
# lists openclaw/reachy
```

Keep the Gateway on loopback or a private network, as OpenClaw recommends.

### 2. Run the bridge

**OpenClaw only.** The bridge needs Python 3.11+ and `aiohttp` (`pyyaml` is optional). Hermes Agent is not required:

```bash
python3 -m venv ~/.venvs/reachy-bridge
~/.venvs/reachy-bridge/bin/pip install aiohttp pyyaml

export OPENCLAW_GATEWAY_TOKEN='your-gateway-token'      # stays on this host
export API_SERVER_KEY="$(openssl rand -hex 32)"          # Reachy <-> bridge key; enter it in Reachy
export OPENAI_API_KEY='your-openai-project-key'          # only for Realtime mode and Kids Mode
~/.venvs/reachy-bridge/bin/python /path/to/companion/hermes_reachy_bridge.py \
  --agent-backends openclaw --host 0.0.0.0 --port 8643
```

**Hermes and OpenClaw together.** Use Hermes' environment as above and add the backend:

```bash
OPENCLAW_GATEWAY_TOKEN='your-gateway-token' venv/bin/python /path/to/hermes_reachy_bridge.py \
  --agent-backends hermes,openclaw --host 0.0.0.0
```

In Reachy → Settings → *Agent model*, choose *Hermes default model* or *OpenClaw agent · reachy*. Models named `openclaw/<agent>` go to OpenClaw; every other model goes to Hermes. With OpenClaw only, any model goes to the first allowlisted agent.

| Variable / flag | Meaning |
| --- | --- |
| `--agent-backends` / `REACHY_AGENT_BACKENDS` | `hermes` (default), `openclaw`, or `hermes,openclaw`. |
| `--openclaw-url` / `OPENCLAW_GATEWAY_URL` | Gateway base URL, default `http://127.0.0.1:18789`. A trailing `/v1` is accepted. |
| `OPENCLAW_GATEWAY_TOKEN` or `OPENCLAW_GATEWAY_PASSWORD` | Gateway credential. Read from the environment or the Hermes profile `.env`, never from the command line. |
| `--openclaw-agents` / `REACHY_OPENCLAW_AGENTS` | Comma-separated agent ids Reachy may use, default `reachy`. |
| `REACHY_OPENCLAW_ALLOW_PRIMARY_AGENT=1` | Allow `main`/`default`. Not recommended. |

### What works with OpenClaw

| Feature | With OpenClaw |
| --- | --- |
| Pipeline conversation (wake word, STT, agent, TTS) | ✅ The agent answers; choose **ElevenLabs** for speech. |
| Realtime conversation | ✅ The Realtime model delegates through an `ask_openclaw` tool. Needs `OPENAI_API_KEY`. |
| Conversation memory | ✅ Each Reachy conversation maps to one OpenClaw session through the OpenAI `user` field. |
| Kids Mode, Agent Mode broker, I Spy | ✅ These never used Hermes; they work the same. |
| "Configured" or local Whisper speech | ❌ They run Hermes Agent's own speech tools. The bridge answers HTTP 409 and the Settings list hides them. |
| Tool-inventory check before each request | ❌ Hermes-only. Rely on the dedicated, restricted `reachy` agent. |

## European model providers (Cortecs, LLMrouter.eu)

The bridge calls a text model itself for Agent Mode (planning and the bounded tool loop), Kids Mode chat and I Spy (picking the object, judging guesses, guessing on the child's turn). By default those calls go to OpenAI in the US. To keep them with European providers, point them at an EU router:

| `REACHY_LLM_PROVIDER` | Endpoint | API key variable | Notes |
| --- | --- | --- | --- |
| `openai` (default) | `https://api.openai.com/v1` | `OPENAI_API_KEY` | Existing behaviour and default models. |
| `cortecs` | `https://api.cortecs.ai/v1` (Vienna) | `CORTECS_API_KEY` | Sends `eu_native: true`, so Cortecs only routes to providers based and regulated in the EU. Set `REACHY_CORTECS_EU_NATIVE=0` to allow all its GDPR-compliant providers. |
| `llmrouter` | `https://proxy.llmrouter.eu/v1` (Germany) | `LLMROUTER_API_KEY` | |
| `custom` | `REACHY_LLM_URL` | `REACHY_LLM_API_KEY` | Any OpenAI-compatible `/v1` endpoint. |

Model names differ per router, so the bridge never guesses one. Set `REACHY_LLM_MODEL` for all three uses, or override per use with `REACHY_AGENT_MODEL`, `REACHY_KIDS_MODEL` and `REACHY_ISPY_MODEL`. The bridge refuses to start when one is missing. Pick models that support:

- **Agent Mode:** tool calling and JSON-schema output.
- **I Spy:** JSON-schema output, and image input for choosing the object. Alternatively, keep the I Spy frames fully local with `REACHY_ISPY_VISION_URL` (see the Realtime trust boundary section).
- **Kids chat:** a capable instruction model; replies are still moderated and length-limited by the bridge.

```bash
REACHY_LLM_PROVIDER=cortecs
CORTECS_API_KEY=your-cortecs-key
REACHY_LLM_MODEL=mistral-medium-2508      # example; use a model your router lists
```

Two things deliberately stay with OpenAI:

- **Realtime voice** (`gpt-realtime-2.1`). Neither router offers a realtime speech endpoint. Use the pipeline conversation mode to avoid it.
- **Kids Mode moderation** (`omni-moderation-latest`). It is the hard safety boundary for child sessions, so Kids Mode still requires `OPENAI_API_KEY`. Only the moderation text is sent to OpenAI, not the chat itself.

`/health` reports the active provider and its region as `text_provider`, plus `agent_model_available`.

Hermes Agent and OpenClaw choose their own models. To run them on an EU router as well, add Cortecs or LLMrouter.eu there as an OpenAI-compatible custom provider (same base URL and key as above).

## API surface

All `/v1/*` routes require:

```http
Authorization: Bearer <API_SERVER_KEY>
```

| Route | Purpose |
|---|---|
| `GET /health` | Hermes, provider, and Realtime availability |
| `GET /v1/models` | Reachy-compatible Hermes model routes |
| `GET /v1/voice-options` | STT/TTS models and account voices |
| `POST /v1/chat/completions` | Authenticated proxy to Hermes API Server |
| `POST /v1/kids/session` | Reachy reports a Kids session as `active` or `ended` for the bridge-side Kids latch |
| `POST /v1/kids/chat` | Bounded, pre/post-moderated child chat without Hermes memory/tools |
| `POST /v1/kids/speech/stream` | Fixed-policy ElevenLabs Flash v2.5 24 kHz PCM stream for approved child text |
| `POST /v1/audio/transcriptions` | Configured/local/ElevenLabs STT |
| `POST /v1/audio/speech` | Configured/Edge/ElevenLabs TTS |
| `GET /v1/realtime` | Authenticated WebSocket proxy to OpenAI Realtime |
|| `GET /v1/agent/capabilities` | Live typed bounded capability manifest |
| `POST /v1/agent/session` | Establish or invalidate the runtime-authoritative device generation |
| `POST /v1/agent/activity` | Recent sanitized device/generation-scoped broker activity |
|| `POST /v1/agent/execute` | Execute or stage one generation-bound typed capability |
|| `POST /v1/agent/pending-approval` | Read the current device/session-scoped exact draft |
|| `POST /v1/agent/approve-pending` | One-shot execution of the unchanged pending draft |
|| `POST /v1/agent/ask` | Bounded model loop using only broker tools |
| `POST /v1/agent/cancel/{request_id}` | Cancel an in-flight broker/Agent request |
| `GET /v1/warm-agents` | Sanitized warm-agent state and per-route usage |
| `DELETE /v1/warm-agents` | Close every warm agent now |

The Realtime client sends an initial `session.start` envelope containing model, voice, reasoning effort, Hermes agent route, stable memory scope, system prompt, and the camera/robot-tool feature flags. The bridge then creates the OpenAI GA Realtime session and exposes `ask_hermes`, the always-available local `set_reachy_power_mode` tool, and only the enabled camera/motion tools. Sleep and Meeting are applied on Reachy itself; no privileged credential is sent to the robot.

### Realtime trust boundary

OpenAI may answer ordinary conversation directly. The session instructions require `ask_hermes` for:

- personal or persistent memory;
- current information;
- Home Assistant or other connected devices;
- local files and system state;
- consequential actions.

In Conversation profile, the bridge executes `ask_hermes` through the authenticated local Hermes API Server. In Agent profile, the same single Realtime tool enters a bounded model loop with fixed T0–T3 schemas and no shell, arbitrary filesystem, maintenance, purchase, lock, alarm, garage, climate-safety, or security-system authority. T1 private reads require matching present-turn intent. T3 writes and all media actions stage exact device/session-scoped drafts for trusted-phone approval. OpenAI does not receive the Hermes bearer token or provider/home credentials, and Reachy does not receive the OpenAI or Home Assistant keys.

When Reachy enables camera support, the bridge advertises `capture_reachy_camera`. The tool call is forwarded to Reachy, which captures one bounded JPEG and sends it as an `input_image` conversation item. The bridge never polls or continuously streams the camera.

When **Use a local vision model** is on in Reachy's settings, Reachy answers `capture_reachy_camera` itself: it sends the frame to the configured OpenAI-compatible vision server (for example Ollama on a Jetson Orin Nano) and returns only the text description as the tool result. No image reaches OpenAI.

Kids I Spy can pick its object with a local vision model too. Set these on the bridge host:

| Variable | Meaning |
| --- | --- |
| `REACHY_ISPY_VISION_URL` | OpenAI-compatible base URL, e.g. `http://127.0.0.1:11434/v1`. When unset, I Spy uses OpenAI (`REACHY_ISPY_MODEL`). |
| `REACHY_ISPY_VISION_MODEL` | Vision model on that server, default `qwen2.5vl:3b`. It must support JSON-schema output. |
| `REACHY_ISPY_VISION_API_KEY` | Optional bearer key for that server. The OpenAI key is never sent to it. |

With a local server the five I Spy frames stay on your network. The bridge still validates the target strictly, and the target text still passes OpenAI moderation, so `OPENAI_API_KEY` remains required for Kids Mode.

When Reachy enables robot tools, the bridge advertises `move_reachy_head`, `express_reachy_emotion`, and `dance_reachy`. The bridge never executes these physical actions itself: completed calls are forwarded to the robot, where an allow-listed local worker performs them. Knowledge, Home Assistant, files, memory, and consequential actions continue to route through `ask_hermes`.

### Kids Mode trust boundary

While any Kids session is live, the bridge itself refuses adult capabilities with HTTP 423: `/v1/chat/completions`, `/v1/realtime`, and the `/v1/agent/*` routes, except `/v1/agent/session` and the cancel/pause routes Reachy needs to wind adult work down. Generic `/v1/audio/speech` stays available for fixed system notices such as the five-minute warning, but its text is moderated first. Reachy reports session start and end through `/v1/kids/session`; any Kids chat or I Spy request also marks its session live. If the end notification is lost, the latch expires 65 minutes after the session started. A start notification that arrives after its end is ignored.

`/v1/kids/chat` is a separate, bounded OpenAI chat route. It does not forward Hermes session headers or normal agent history, and it applies moderation before and after generation. The bridge accepts only age-band/activity/language enums, constructs the child policy itself, and owns bounded ephemeral history keyed by the random child session ID; caller-supplied system prompts and history are rejected. Camera, robot, agent/delegation, Home Assistant, file, messaging, purchase, and power capabilities are absent during child conversation.

I Spy uses a separate consented boundary: `/v1/kids/ispy/select` accepts exactly five bounded JPEGs, requests a strict structured target, revalidates stability, visibility count, confidence, frame index, normalized bounding box, colour, forbidden vocabulary and hints, then moderates the target text before storing only target metadata. The camera is revoked before guessing. `/v1/kids/chat` keeps an explicit Reachy-picker/player-picker state machine: it judges the child's guesses with a boolean-only schema, then lets the child supply bounded clues while a strict schema produces up to six non-repeating safe household-object guesses. A confirmed guess or reveal returns a constrained next-round action to the robot; `/v1/kids/ispy/clue` approves only the bridge-owned colour clue for speech after the next base-and-head camera search. Stop/expiry deletes the complete alternating-round state.

The Kids `/v1/kids/speech/stream` and `/v1/kids/speech/fallback` paths accept only bounded text carrying their own short-lived, single-use bridge approval tied to the exact child session and normalized post-moderated text digest. The streaming provider, model, output format, and default voice are bridge-controlled: ElevenLabs `eleven_flash_v2_5`, 24 kHz PCM, and the configured `ELEVENLABS_KIDS_VOICE_ID` (or bundled child-voice default); fallback ignores caller provider/model/voice fields and invokes the Hermes host's configured TTS. Missing, expired, altered, or replayed approvals are rejected; the route does not accept arbitrary provider/model selection or unmoderated model tokens.

## Run at boot with systemd

The included example assumes this repository is cloned to `~/homebody`:

```bash
mkdir -p ~/.config/systemd/user
cp companion/hermes-reachy-bridge.service.example \
  ~/.config/systemd/user/hermes-reachy-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-reachy-bridge.service
```

Verify with:

```bash
systemctl --user status hermes-reachy-bridge.service
curl -H "Authorization: Bearer $API_SERVER_KEY" \
  http://127.0.0.1:8643/health
```

Expected Realtime health fields:

```json
{
  "realtime_available": true,
  "kids_chat_available": true,
  "kids_tts_streaming_available": true,
  "realtime_model": "gpt-realtime-2.1"
}
```

## Logs

```bash
journalctl --user -u hermes-reachy-bridge.service -f
```

The bridge must not log bearer tokens, OpenAI keys, ElevenLabs keys, or response audio. Keep Hermes secret redaction enabled.

## Security

- The default bind address is `127.0.0.1`.
- Bind to `0.0.0.0` only on a trusted LAN or VPN.
- Every chat/audio/discovery/Realtime route uses constant-time bearer-token authentication.
- Provider keys remain on the Hermes host. An OpenClaw Gateway token likewise stays on the bridge host and is never sent to Reachy.
- OpenClaw requests only reach allowlisted agents (`REACHY_OPENCLAW_AGENTS`), never the owner's `main` agent by default.
- The bearer token can invoke a tool-capable agent; treat it as an administrative credential.
- For remote access, use TLS and an authenticated reverse proxy. Never expose raw port `8643` publicly.
- Rotate both the bearer token and any provider credential after suspected disclosure.
