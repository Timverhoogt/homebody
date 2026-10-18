# Install your own standalone household MCP

This is the operator runbook and copyable AI-agent setup contract. Everyday needs **no
Hermes installation, profile, memory or agent subscription**. An AI assistant can help install
it, but is not a runtime dependency. This is not the existing “Connect your agent” pairing
wizard and must not be passed its one-time setup code.

## 1. Choose the integrated installation

**Released support since 0.5.35:** standalone household MCP is included in published Homebody.
Use matching reviewed versions on the household and robot hosts. Verify the exact source snapshot
contains `companion/household_server.py`, `household_smoke.py`, `homebody/household_client.py`
and this guide, or use a wheel built from that exact snapshot. Record source revision plus local
changes and the wheel SHA-256; a matching version string alone is insufficient. Verify installed
files against that artifact before replacing a production installation.

Roles:

- **Household host:** your computer running the HTTP MCP service and optional direct speech/model
  and HA integrations. It does not own robot hardware. Keep it awake during use.
- **Robot host:** the existing Reachy SDK/daemon and matching Homebody application. Its current
  adult runtime owns camera, movement, permissions and session scope. Do not start a second
  robot process. A source host alone cannot fabricate the device session required by tools.
- **MCP caller:** your chosen client using the separate caller token. It must support HTTP POST
  JSON-RPC, custom headers and per-request `_meta.homebody_context` for tools/list and tools/call.
  A client that only accepts a URL and bearer token cannot use the scoped catalogue as-is.

Python **3.11+**, pip and venv are required on the host. The minimal source host needs
`aiohttp>=3.10,<4`; stdlib handles the smoke client. Source commands below use a POSIX shell
(Linux/macOS); on Windows use `py -3.11 -m venv .venv-household` and
`.venv-household\Scripts\python.exe` in place of `.venv-household/bin/python`. Secret-file
permissions on Windows require an owner-only ACL, not just POSIX chmod. Windows/macOS live
service acceptance is not established by Linux offline tests. Robot OS, SDK/media and hardware
support are a separate requirement: consult the repository hardware and marketplace guides.
The full wheel includes robot dependencies (Reachy SDK, vision/audio and platform packages),
so a pure-Python wheel tag does not promise those dependencies install on every host.

### Independent household routing; preserve shared speech services

In the paired Owner dashboard, open **Household → Standalone household connection**.
Save the HTTPS host origin and **robot credential** there, not in the old bridge fields.
`household_url` and `household_api_key` are independent; when both are unset all existing
routes retain the original `bridge_url` / `api_key` defaults. Saved credentials never appear
in status or in this guide. The password input is write-only; blank retains the saved key.
Clear the URL to remove the independent pair and return to the shared bridge.

| Mode/call | Destination |
| --- | --- |
| Everyday Pipeline text/tools, looks, vision, capabilities | Household service |
| Everyday Realtime and GPT-Live voice WebSockets | Household service |
| External composed MCP robot callback | Household service, explicit Owner Connect |
| Pipeline health, STT/TTS; Kids chat, moderation, Show/Tell and exact speech | Existing shared speech bridge |
| Workspace agent/native execution and Workspace voice | Existing bridge |
| Device session generation/revocation | Both configured services; admission requires only that mode's service |

Keep the shared speech bridge running when using Kids, Pipeline, announcements or Workspace.
The standalone service does not implement those routes and does not import or execute a Hermes
agent runtime. Realtime/GPT-Live Everyday can use only the independent service. Pipeline still
requires its shared speech services; do not describe it as a speech-service replacement.
The legacy agent pairing wizard does not configure the household connection. On a fresh robot,
choose Everyday and Realtime or GPT-Live in Settings, save the independent connection, then use
the first-run **Check connection** and finish/restart through the existing Owner onboarding.
That check uses the household capabilities endpoint; Pipeline additionally checks the shared
speech bridge. These read-only checks never claim a real speech test or start listening.

## 2. Install on the household host (after approval)

Use a new directory/venv, not the robot daemon's Python, a global Python or an existing bridge
venv. Obtain the source from the project's repository using the reviewed snapshot supplied by
the maintainer; do not assume a public branch already contains it. In that checkout:

```sh
python3 --version
python3 -m venv .venv-household
.venv-household/bin/python -m pip install 'aiohttp>=3.10,<4'
.venv-household/bin/python companion/household_server.py --help
.venv-household/bin/python companion/household_smoke.py --help
```

Keep the **whole companion directory**, not just the server file: shared imports are required.
`--help` must exit successfully without binding ports, reading robot configuration or starting
hardware. No provider key is needed for direct clock/host tools. Optional integrations can
report unavailable without a model fallback.

Alternatively, build an explicitly reviewed local wheel in a separate build environment:

```sh
python3 -m venv .venv-build
.venv-build/bin/python -m pip install build
.venv-build/bin/python -m build --wheel
```

Select the exact wheel, not a wildcard that can select an older artifact. Set `WHEEL` to its
absolute path, then on a compatible target host:

```sh
python3 -c 'import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1],"rb"),"sha256").hexdigest())' "$WHEEL"
python3 -m venv .venv-household
.venv-household/bin/python -m pip install "${WHEEL}[household]"
.venv-household/bin/homebody-household --help
.venv-household/bin/python -m homebody.bridge.household_smoke --help
```

The `household` extra supplies aiohttp but **does not remove normal robot dependencies**. For
minimal host-only installation prefer source. Do not use `--no-deps` to call an incomplete
robot installation supported. Installing a matching robot artifact is a separate, approved
robot deployment using its documented install/update/rollback procedure, not these host steps.

## 3. Generate and hand off two secrets locally

Use a private operator terminal with tracing/session recording disabled. Never paste secrets,
provider keys, pairing codes, environment files or Owner cookies into agent chat. Use an
approved secret manager where available. This POSIX fallback creates a new owner-only file,
refuses overwrite and prints **no secret**; the service does not automatically load `.env`:

```sh
.venv-household/bin/python - <<'PY'
import os, secrets
from pathlib import Path
folder = Path.home() / '.config' / 'homebody-household-trial'
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
folder.chmod(0o700)
p = folder / 'tokens.env'
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('HOMEBODY_HOUSEHOLD_TOKEN=' + secrets.token_hex(32) + '\n')
    f.write('HOMEBODY_HOUSEHOLD_ROBOT_TOKEN=' + secrets.token_hex(32) + '\n')
PY
set +x
set -a
. "$HOME/.config/homebody-household-trial/tokens.env"
set +a
```

Use this only in an operator-controlled private home directory, outside version control and
shared backups. Protect any copy with the same permissions. The host receives both values;
the MCP caller receives **only HOMEBODY_HOUSEHOLD_TOKEN**, through its secret store. The trial
robot receives **only HOMEBODY_HOUSEHOLD_ROBOT_TOKEN**, through authenticated Owner settings
as its `household_api_key` (Standalone household connection → Robot credential).
Preserve the legacy `api_key` for shared speech, Kids and Workspace services.
Never put tokens in command arguments, URLs, screenshots or logs. Do not
source this two-token file in an untrusted caller/agent environment; have the operator provision
only its caller variable there. Separate tokens are mandatory and cannot be interchanged.

## 4. Launch locally, then check health

From the approved checkout and private host shell with the two variables loaded:

```sh
.venv-household/bin/python companion/household_server.py --host 127.0.0.1 --port 8644
```

Wheel alternative: `.venv-household/bin/homebody-household --host 127.0.0.1 --port 8644`.
This is a foreground process; Ctrl-C ends it. No daemon, login item, firewall rule, TLS proxy,
public tunnel or automatic restart is installed. Approve those separately if later needed.

In a second private shell with **only the caller token** provisioned in its environment:

```sh
.venv-household/bin/python companion/household_smoke.py --url http://127.0.0.1:8644
```

Wheel equivalent: `python -m homebody.bridge.household_smoke` using that venv Python.
This sends standard MCP `initialize` (2025-03-26), `notifications/initialized`, then `ping`.
Expected summary: `health: ok`, `scoped_tools: not_checked`. This proves authenticated protocol
reachability, **not robot attachment, tools, speech or physical safety**. `/health` returning
404 is expected; do not use legacy bridge health checks for this app.

### Remote robot or caller

127.0.0.1 means the machine making the request. A robot on another machine cannot reach the
host's loopback address directly. Use an **operator-managed private HTTPS origin** with a trusted
certificate, terminating to the loopback service. The proxy must preserve Authorization and
X-Reachy-Device-Id, support POST `/mcp` and outbound robot WebSocket upgrade at
`/v1/household/robot` (WSS), and pass required lifecycle/Everyday routes for the approved trial.
Use an origin without a path prefix for the supplied smoke check. Restrict ingress to intended
robot/caller networks; redact authorization and request bodies in proxy logs. Do not expose
this preview unauthenticated or turn off certificate checks. The robot explicitly rejects
non-loopback HTTP callbacks. A bare LAN HTTP URL is not a workaround. TLS, DNS, firewall,
certificate trust and proxy setup are operator work, not automatically implemented here.

## 5. Configure and attach through the Owner dashboard

If you have not paired an Owner browser, first provision the matching robot app through its
normal Reachy install/start flow. On the **robot host**, as the running app's user with its
effective HOME, run `python3 -m homebody.owner_auth pair --origin https://your-robot.example.net`
using the robot app's Python and replacing the example with your actual private trusted HTTPS
dashboard origin. Enter the locally displayed ten-minute code in that browser; do not send it
to an agent/chat. If the app uses a custom `HOMEBODY_OWNER_DB`, the pairing CLI must use that
same database/environment (or its `--db` option). Do not create a different user's database or
dump the existing one. This Owner code is distinct from the agent setup code. The local
dashboard normally uses port 8042; HTTPS proxy/owner provisioning is not supplied by the
household host installer. Have the operator resolve this prerequisite before continuing.

1. Confirm matching robot code and paired Owner browser. In **Household → Standalone household
   connection**, enter the HTTPS origin and robot credential, review the disclosure, choose
   whether to renew external connections, then **Save connection**. Existing bridge fields and
   permissions remain unchanged. **Validate service** performs a read-only capabilities check;
   it does not prove speech provider readiness. Do not use `/api/agent-setup/pair`.
2. Explicitly approve external tools (`mcp_enabled`) for this trial if wanted. Leave movement,
   power and camera OFF when already off. A read-only clock check does not require enabling
   them. Household speech camera consent is not external MCP camera consent; external camera
   additionally requires `mcp_vision_enabled`, camera access and local vision. No camera or
   motion acceptance is authorized by this guide.
3. Owner starts the approved **adult Everyday** runtime/session using its existing controls.
   Merely opening Settings or setting power Awake is not an active adult session. Starting
   voice may have microphone/speech/power effects; obtain separate approval and supervise it.
4. Press **Connect external tools** in the same card. Connection requires an eligible adult
   Everyday runtime and existing external-tool permission; it never wakes, captures or listens.
   For an MCP client's private scope handoff, the equivalent Owner API returns the exact
   scope below (disconnect first if already connected). In the paired dashboard console:

```js
const response = await window.fetch('/api/household/tools/connect', {method: 'POST'});
if (!response.ok) throw new Error(`Attachment refused (${response.status})`);
const householdScope = await response.json();
```

The loaded `owner.js` fetch wrapper supplies the same-origin Owner cookie and in-memory
`X-Homebody-CSRF` header. Do not copy cookies/CSRF values to a shell or disable the boundary.
This must run in the actual dashboard, not an arbitrary page or raw console fetch without its
wrapper. The POST has **no body**: request URLs, credentials or fabricated context are ignored.
It returns `connected: true`, `device_id`, exact `homebody_context`, `maximum_seconds: 300`
only after host acknowledgement. Connection itself does not capture, move or change settings.

Save that response locally as a private `household-scope.json` for the approved MCP caller
(e.g. browser DevTools `copy(JSON.stringify(householdScope))`, then paste into a local private
file, not chat; clear the clipboard afterward). Keep it outside version control, owner-readable
only. Context is short-lived authority metadata, not a replacement for the caller token.

## 6. List tools and make one read-only call

Within the current attachment window, from the caller shell:

```sh
.venv-household/bin/python companion/household_smoke.py \
  --url http://127.0.0.1:8644 --scope household-scope.json
```

Replace the origin with the operator-approved HTTPS origin for a remote host. The helper
never attaches, takes arbitrary tool names, calls a model, mutates HA, moves, speaks or captures.
It refuses remote cleartext, URL credentials and redirects, bypasses inherited HTTP proxies,
and does not print secrets, scope or response text. Success requires initialize/notification/ping,
a scoped `tools/list` containing `get_current_time`, and a successful `tools/call` readback.
Expected summary includes `scoped_tools: ok`, a measured `tool_count`, and `readback_ok: true`.

For another standard MCP client, configure URL `<approved origin>/mcp`, Authorization
`Bearer <caller token>` through its secret mechanism, and `X-Reachy-Device-Id` from the response.
Send `params._meta.homebody_context` unchanged on **both** tools/list and tools/call. For example
the read-only call params are `{ "name": "get_current_time", "arguments": {}, "_meta": {
"homebody_context": <exact response object> } }`. Never manufacture its session/generation.
Some clients cannot supply this metadata or refresh it; report that limitation rather than
claiming universal plug-and-play MCP compatibility. Approved transport renewal keeps the same
current scope; a changed generation requires a fresh Owner Connect response. Initialize/ping
do not require a device attachment; lists/calls do.

Host tools can remain listed when their optional integration is absent. `isError: true` or
`structuredContent.ok` not true means unavailable/failed, not successful validation. Do not
retry a timed-out action blindly: physical/HA actions may already have happened.

### Optional Home Assistant (not needed for the clock check)

Only after explicit owner approval, provision host-side `HASS_URL` and `HASS_TOKEN` via the
secret mechanism. Start with an empty allowlist. Choose actual owner-approved entity IDs;
this example illustrates exact JSON syntax, not entities to install blindly:

```sh
export REACHY_AGENT_HA_ALLOWLIST='{"light.desk":["friendly_name","brightness"]}'
export REACHY_EVERYDAY_ALIASES='{"light.desk":["desk lamp"]}'
```

The allowlist maps entity IDs to allowed attribute names; aliases map entity IDs to spoken
names. No wildcard, locks or arbitrary `call_service`. Supported eligible light/switch on/off
controls include readback, but are **not** part of onboarding verification. With explicit
permission to read that HA entity, use scoped `home_status` with `{"names":["desk lamp"]}` and
check the actual returned state/ok result. Empty names reads all configured devices, so prefer
the named scope. An allowlist does not itself create a read-only MCP credential: keep client
approval controls and do not give an autonomous agent blanket access to mutations.

### Optional speech

Direct tools need no language-model key. Voice/semantic selection requires the selected direct
provider's documented host-side `REACHY_LLM_PROVIDER`, `REACHY_EVERYDAY_PROVIDER`,
`REACHY_EVERYDAY_MODEL` and API-key configuration. Choose deliberately; do not inherit an agent's
credentials/profile. `openai-codex` subscription reasoning is rejected here. This guide does
not prescribe a provider or authorize paid calls. Realtime has native function tools;
**GPT-Live has no native MCP tool contract here**. Its delegation still uses the narrow Everyday
text model; speech-only semantic selection has not been achieved. Pipeline STT/TTS and Kids
routes are not provided by this standalone app.

## 7. Disconnect, troubleshoot, and roll back

From the paired dashboard console:

```js
const disconnected = await window.fetch('/api/household/tools/disconnect', {method: 'POST'});
if (!disconnected.ok || (await disconnected.json()).connected !== false) {
  throw new Error('Disconnect not confirmed');
}
```

Each transport lease lasts **at most 300 seconds**, with at most thirty robot calls and a
15-second host robot-call deadline. With the saved Owner renewal approval, healthy leases
renew automatically within the original authority. Transport failures get at most three retries
with bounded backoff; individual actions are never replayed. Use **Disconnect** to stop.
Stop, Kids, Workspace, privacy, permission changes or session replacement invalidate active
work and terminate renewal. After these events or app restart, press **Connect external tools**
again; saved renewal approval never recreates revoked consent or wakes/captures. Never turn off
privacy or re-enable a previously off setting merely to pass a check.

| Symptom | Check without widening permissions |
| --- | --- |
| Missing entrypoint/module/404 attach | Wrong artifact; installed 0.5.34 alone is insufficient. |
| Missing aiohttp/shared import | Correct venv and whole companion tree, or wheel household extra. |
| CLI refuses credentials | Both variables must exist and be different; never print them. |
| 401 host | Caller vs robot token separation; proxy must preserve Authorization. |
| 401/403 robot | Owner pairing, same-origin fetch and CSRF; do not bypass them. |
| 409 connect | Already attached, runtime not started, non-adult/privacy state, denied permission, TLS/WSS failure or missing matching host. Disconnect before intentional replacement. |
| MCP -32001 | Missing/stale scope or retired attachment; ask Owner to reconnect, don't fabricate context. |
| MCP -32602 | Use advertised name/schema, exact argument types; no generic URL tool. |
| Clock succeeds, HA unavailable | Optional integration/allowlist is not configured; clock is not HA proof. |
| Health succeeds, voice/Kids fails | Health is protocol-only; missing routes/provider readiness remain separate. |
| Remote cleartext/TLS failure | Use approved trusted HTTPS/WSS, not 0.0.0.0 plus disabled TLS checks. |

Rollback: disconnect and confirm `connected: false`; retire the adult trial session through
existing controls. Clear the independent household URL in the connection card to remove its
credential and restore legacy routing; verify unchanged bridge settings and off permissions. Stop only the trial host
process you created (Ctrl-C for the foreground launch), remove its caller registration/private
scope, and revoke/delete trial tokens through the secret mechanism. Remove trial venv/artifacts
only if approved, never another service or production environment. Revoking tokens may require
an approved host restart; no reload/rotation endpoint is provided. Re-run a scoped read after
disconnect expecting refusal, not success. Completed physical changes cannot be rolled back
by disconnect. Report failures without secret-bearing payloads.

## Copyable AI-agent instructions

> Help me set up **my own standalone Homebody household MCP server**, using this runbook,
> not Hermes as an Everyday dependency. First discover the approved source/wheel identity,
> target OS/Python, host vs robot vs caller roles, existing deployment and chosen private
> topology through non-secret inspection. Ask me for missing targets. Do not read credential
> stores, private configs or environment dumps for convenience. Explain that this is unreleased;
> use the independent household connection card and retain shared speech services for Kids,
> Pipeline and announcements. Do not claim a version number proves feature presence.
>
> Before installing dependencies, writing configuration, registering an MCP client, creating or
> starting services, changing robot settings, certificates, firewall/proxy/DNS or network exposure,
> give me the exact scoped plan, risks and rollback and wait for approval. Preserve existing
> endpoints, processes and every off permission. Never install a second hardware runtime.
> Use the integrated Owner settings; never replace the legacy bridge endpoint with this service.
>
> Generate distinct caller and robot secrets only through my approved local secret mechanism;
> never put them, pairing codes, Owner cookies or provider keys in chat, arguments, logs or
> screenshots. Have me hand off the robot token in Owner settings and the caller token in the
> client's secret store. Do not use the old agent pairing code/wizard for standalone setup.
> Use loopback first; remote use requires operator-managed trusted HTTPS/WSS, no TLS bypass.
>
> Validate only the standard MCP initialize/initialized/ping exchange, and after my explicit
> Owner attachment, tools/list plus get_current_time with the exact returned device/context.
> Use the packaged read-only smoke helper; do not invent URLs, metadata, scopes, schemas or
> success responses. Optional HA reads need separate explicit entity approval. No autonomous
> camera, movement, power, speech, HA mutation, provider calls or permission enablement.
> A passing ping is not a working robot, HA integration or voice session. GPT-Live delegation
> still uses a narrow text model; do not promise native GPT-Live MCP or speech-only tool selection.
>
> Disconnect on completion and verify refusal of stale scope. Report source/artifact identity,
> approved changes, actual redacted health/list/read-only results, preserved settings, rollback
> status and remaining untested live behavior. Mark each checklist item verified, blocked or
> not attempted, with evidence. Never reuse a prior snapshot's release gate after source changes.

### Reusable completion checklist

Copy this JSON into the private installation report and replace `not_attempted` with
`verified` or `blocked`, adding redacted evidence for each step. It is a report schema example,
**not service configuration**; do not place it in robot settings.

```json
{
  "artifact_identity": "not_attempted",
  "approval_and_topology": "not_attempted",
  "isolated_host_dependencies": "not_attempted",
  "separate_secret_handoff": "not_attempted",
  "protocol_health": "not_attempted",
  "owner_attachment": "not_attempted",
  "scoped_tool_list": "not_attempted",
  "read_only_clock_readback": "not_attempted",
  "optional_named_ha_read": "not_attempted",
  "disconnect_and_stale_scope_refusal": "not_attempted",
  "original_settings_preserved_or_restored": "not_attempted",
  "live_voice_hardware_acceptance": "not_attempted"
}
```
