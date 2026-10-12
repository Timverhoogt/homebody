# Subscription text models, reasoning off, native Workspace

Homebody can separate API-funded speech from **subscription-funded text/vision inference**. This is optional trusted-host configuration, not the default for every installation. It uses the installed Hermes provider router and Codex Responses adapter with supported saved ChatGPT authentication. OAuth tokens never go to the robot or dashboard. Subscription inference is still cloud processing, subject to account availability and quotas; it is not offline or unlimited.

See [release and acceptance status](release-status.md): this host support landed on main **after robot release v0.5.2**. The one-message setup bundle comes from the installed robot version and does not automatically provision these settings or a native session. Back up and update the host bridge separately from the robot and phone.

## 1. Prerequisites and authentication

- A trusted bridge host with a compatible installed [Hermes Agent runtime](https://hermes-agent.nousresearch.com/docs/integrations/providers) and its supported ChatGPT/Codex authentication already working under the service account.
- Use Hermes' normal supported sign-in; do not export browser cookies or treat subscription credentials as generic API keys. Authenticate/refresh on the host, not through voice or a dashboard text field.
- Current companion source modules, including `subscription_reasoning.py` and `subscription_bridge_entry.py`. Do not assume the bridge bundled inside an older robot wheel contains them.
- Existing bridge bearer authentication and Kids moderation/speech credentials remain required for their respective paths. The bridge bearer token is not a model-provider credential.
- Models available to that account which genuinely accept `reasoning.effort=none`. Verify account acceptance, not just a model name in a dropdown.

## 2. Narrow Everyday, Kids and I Spy

Example host environment file; these are **validated reference choices as of 12 October 2026**, not universal defaults:

```dotenv
HOMEBODY_HERMES_SOURCE=/absolute/path/to/installed/hermes-agent
REACHY_EVERYDAY_PROVIDER=openai-codex
REACHY_EVERYDAY_MODEL=gpt-6-sol
REACHY_EVERYDAY_URL=
REACHY_LLM_PROVIDER=openai-codex
REACHY_LLM_MODEL=gpt-6-sol
REACHY_LLM_URL=
REACHY_AGENT_MODEL=gpt-6-sol
REACHY_KIDS_MODEL=gpt-6-luna
REACHY_ISPY_MODEL=gpt-6-luna
```

Keep the file private (`0600`), owned by the bridge service account. Replace the absolute path with the real installed Hermes source; placeholders are not runnable configuration. Preserve existing bridge key, household entity allowlists, weather source, speech settings and camera consent. In the reference setup household writes are explicitly lights-only—not switches, locks or climate. Optional `REACHY_ISPY_VISION_URL` still selects a separately configured local vision server; clear it only if intentionally moving those frames to subscription cloud vision.

Launch through the bootstrap entry rather than importing Hermes for the first time inside a request:

```bash
/path/to/hermes/python /path/to/homebody/companion/subscription_bridge_entry.py \
  --profile reachy --hermes-url http://127.0.0.1:8652 --host 127.0.0.1 --port 8643
```

Set the environment through the service's protected environment file before launching. The entry bootstraps the managed Hermes runtime and preserves bridge CLI arguments through a possible re-exec. Keep the existing restricted Reachy profile for ordinary room conversation; do not enable its terminal or file tools to make subscription inference work. LAN/VPN binding is a separate explicit choice. See [companion operations](../companion/README.md).

The adapter sends `reasoning.enabled=false` / `effort=none` and checks that the generated Responses payload contains `reasoning.effort=none` **before submission**. At this snapshot `gpt-6-sol` and `gpt-6-luna` accepted none; `gpt-6.1-sol` has no genuine none route. Low/minimal is not off. Missing authentication, unsupported model/off capability, a custom inference URL, quota or inference failure is an explicit error—not permission to fall back to a paid text API or full agent.

`SubscriptionHttp` intercepts only the registered subscription text endpoint. Moderation, speech, Home Assistant and native-session HTTP keep their real transport. Existing Kids input/output moderation, strict I Spy target/JSON validation, privacy, generation checks and game rules remain mandatory. The Codex chat shim may ignore `response_format`: explicit structured-output instructions and strict host validation remain necessary; do not claim provider-enforced schema guarantees.

Everyday and Kids use direct, narrow prompts. They do **not** instantiate a full Hermes agent or inherit Workspace memory, repositories or shell tools. Selecting subscription funding does not change that authority boundary.

## 3. Dedicated native Workspace (owner configuration)

Native Workspace intentionally uses a real Hermes session, history, tools and normal approvals. Its configured working directory is **context, not a filesystem sandbox**. Native discussion runs can use the worker's tools; “discuss only” is an instruction, not an enforced read-only boundary. Choose an appropriately restricted worker/toolset if read-only discussion is required. Planned work still goes through Homebody's exact phone approval, fixed checks and receipt flow.

The dedicated `subscription_workspace_entry.py` starts the official Hermes session/run API on authenticated loopback. It pins one exact registered session, Codex provider/model and reasoning off at the real agent factory, including the confirmed runtime-lock contract that disables the native fallback chain. It does not modify the primary gateway's model or ongoing sessions. It shares the selected Hermes home's native session database; use that same service identity and home for session creation and observation.

Example additional **host-only** environment:

```dotenv
HERMES_HOME=/absolute/path/to/owner/hermes-home
HOMEBODY_HERMES_SOURCE=/absolute/path/to/installed/hermes-agent
HOMEBODY_WORKSPACE_ROOT=/absolute/path/to/registered/project-repository
HOMEBODY_WORKSPACE_SESSION=<exact-owner-created-session-id>
HOMEBODY_WORKSPACE_MODEL=gpt-6-sol
HOMEBODY_WORKSPACE_PORT=8662
HOMEBODY_NATIVE_API_KEY=<new-private-listener-bearer-token>
```

Choose an unused port, a real Git repository and a new high-entropy listener token, keeping it only in private host configuration. The session ID must exactly match the session created below; do not substitute an alias or an unverified resumed child. Launch as an owner-managed host service:

```bash
/path/to/hermes/python /path/to/homebody/companion/subscription_workspace_entry.py
```

Before asking it to run, create the exact session through that listener's authenticated `POST /api/sessions`. Example JSON body (replace placeholders):

```json
{
  "id": "<exact-owner-created-session-id>",
  "title": "Homebody project Workspace",
  "source": "homebody",
  "model": "gpt-6-sol",
  "provider": "openai-codex",
  "model_options": {"reasoning": {"enabled": false, "effort": "none"}},
  "require_model_lock": true
}
```

Read back that exact session and its lock. Keep the session's project instructions explicit but do not mistake instructions for enforcement. Register the repository/roadmap in host-owned `REACHY_AGENT_PROJECTS_JSON` using the [native project catalog contract](native-workspace.md), then register its target in the bridge environment:

```dotenv
REACHY_NATIVE_TARGETS_JSON='{"project-hermes":{"backend":"hermes","url":"http://127.0.0.1:8662","token_env":"HOMEBODY_NATIVE_API_KEY","project_id":"<registered-project-id>","session_id":"<exact-owner-created-session-id>","discussion_enabled":true,"execution_enabled":false}}'
```

Start with execution disabled. Before enabling it, explicitly configure fixed `verification_commands` and `artifacts` against the **same actual repository** used by the native worker, and retain normal tool/command approvals. See [native Workspace](native-workspace.md) and [Everyday/Workspace controls](everyday-workspace.md). The key referenced by `token_env` must also be available to the bridge service; never send it to the phone.

Restart only the affected host services after their configuration is complete. Select Workspace and the registered project/session from the phone. Registration alone does not select a project, approve work or prove the full physical/phone flow. Without native binding, the roadmap reader remains explicitly restricted—not “your Hermes session.” Existing OpenClaw conversation compatibility does not imply equivalent subscription or native execution support.

## Local speech runtime compatibility

Kids always use STT before the moderated text route, even when the adult engine is GPT-Live or Realtime. A successful subscription text request does not verify transcription. If bootstrapping moves the bridge to a managed Python without faster-whisper, do not assume an older environment's installed package is visible there, and do not silently move child audio to a cloud provider.

When a separate trusted Python already has faster-whisper and the `base` model cached, the host can use the supported command backend:

```dotenv
HERMES_LOCAL_STT_COMMAND='/absolute/path/to/whisper/python /absolute/path/to/companion/local_stt_command.py --input {input_path} --output-dir {output_dir}'
```

Set this only in the bridge's protected service environment, then restart that idle host bridge—not the primary gateway or robot. The helper uses cached CPU/int8 base inference with two threads, language auto-detection, VAD and a conservative silence-hallucination gate. It does not download models or fall back to cloud STT. Verify actual generated English and Dutch audio through `/v1/audio/transcriptions` using the same configured selection as the robot; then check a moderated Kids reply and approved speech generation without playing audio physically.

Voice scopes are separate: Marin/Cedar apply to new adult Live/Realtime sessions, dedicated Kids replies use the host's fixed-policy ElevenLabs voice, and Pipeline/greeting/approved-fallback voice selection is another setting. Changing an adult voice must not alter child moderation or speech approvals.

## 4. Verify without activating the robot

1. Back up bridge modules and private configuration. Preserve the current speech selection and main gateway.
2. Read actual robot state: safely folded motor-off Standby. Do not fabricate an Awake context or trigger speech/movement for a backend probe.
3. Read authenticated `/health` and `/v1/everyday/capabilities`; verify provider, purpose models, subscription billing/no fallback and `reasoning_effort=none` against effective service configuration.
4. Exercise harmless text, moderated Kids input/output, structured guess matching and a **synthetic** image. End synthetic Kids sessions and clear their state/capabilities afterward. Synthetic image success is not physical I Spy acceptance.
5. Exercise a harmless authenticated native `POST /v1/runs` against the exact session, then read the admitted run ID to terminal success and verify stored history. Inspect `runtime.provider/model`, not just a virtual top-level model alias. A cold first run can warm compute dependencies; after a timeout inspect the already admitted run, do not resubmit blindly.
6. Verify exact model/lock persistence, session/run identities and bridge authentication to that history. Full phone binding/approval/execution and physical speech need their own acceptance; backend success is not that result.
7. Keep negative tests for wrong endpoint/model/session, missing auth, inability to encode none, denied tools and stale callbacks.

OpenAI Realtime/GPT-Live speech and `omni-moderation-latest` screening remain API services. Do not remove moderation to claim an API-free setup. No Homebody-specific setting here changes the owner's main Telegram/Desktop model.
