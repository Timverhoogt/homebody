# Standalone household tools and voice service

**New installation:** read the [complete operator and AI-agent runbook](../homebody/agent_guides/household.md)
first: your own host, source/wheel dependencies, separate secrets, TLS, Owner attachment,
read-only smoke checks and rollback. The matching app serves it at `/agent-setup/household.md`.
This feature is unreleased; a published 0.5.34 install is not enough.

This is a development artifact, not an installed service. Production and saved robot settings
are not changed by building it. The standalone service needs no Hermes agent, profile, memory,
private-file runtime or subscription-backed Hermes model process. Workspace remains on its
separate existing bridge; this service deliberately has no Workspace execution routes.

## Start the household-only host

Use Python 3.11+ and `aiohttp` on the host. From this checkout:

```sh
python companion/household_server.py --host 127.0.0.1 --port 8644
```

The wheel also provides `homebody-household --host 127.0.0.1 --port 8644`; its `household`
optional dependency supplies `aiohttp`. The wheel otherwise retains the robot application's
normal dependencies. A minimal host can run the source companion directly without installing
robot dependencies. The packaged launcher handles the companion's direct-script import layout.

Before launching, the operator must supply **two distinct** environment credentials:

- `HOMEBODY_HOUSEHOLD_TOKEN`: only for household MCP callers (`POST /mcp`).
- `HOMEBODY_HOUSEHOLD_ROBOT_TOKEN`: only for the trusted robot's voice, session and outbound
  callback attachment. Never distribute it to an MCP caller.

Neither has a default. Keep credentials in the deployment's secret mechanism, not command-line
arguments or chat. Bind loopback by default; a remote robot needs an operator-managed HTTPS/WSS
endpoint. No TLS proxy, tunnel, daemon or service is created here. Source `--help` and the wheel
entrypoint can be inspected without starting a server.

Direct host tools do not need a language-model key. Voice and semantic selection require the
chosen direct provider's host-side configuration (`REACHY_LLM_PROVIDER`,
`REACHY_EVERYDAY_PROVIDER`, `REACHY_EVERYDAY_MODEL` and its API-key environment variable).
`openai-codex` subscription reasoning is explicitly rejected by this standalone factory.
Provider secrets are never taken from a Hermes profile in this mode.

Existing household integration settings are reused, not broadened: `HASS_URL`, `HASS_TOKEN`,
`REACHY_AGENT_HA_ALLOWLIST`, `REACHY_EVERYDAY_ALIASES`, weather/timezone settings and the existing
optional host integrations. The legacy `REACHY_AGENT_` prefix does not enable agent tools.
HA control remains limited to the existing eligible, allowlisted lights/switches and supported
on/off actions, with readback; locks and arbitrary `call_service` are not exposed.

## Attach the real robot (explicit Owner operation)

The matching robot artifact includes `homebody.household_client`. It uses the current runtime;
it does not launch another robot process or invent a voice transcript.

1. In the paired Owner dashboard open **Household → Standalone household connection**.
   Save `household_url` and the robot `household_api_key` using that card; leave the existing
   `bridge_url` and `api_key` untouched. Validate service there. Saving requires Owner/CSRF and
   explicit acknowledgement. Blank password retains the saved secret; clearing the URL removes
   the independent pair. Saved secrets are never returned in status. Keep the shared bridge
   for Kids, Pipeline health/STT/TTS, announcements and Workspace.
2. Retain the existing explicit `mcp_enabled` approval for external tool callers. Power and
   movement use their existing per-capability choices. External camera additionally requires
   `mcp_vision_enabled`, Camera access and local vision. Ordinary household speech camera
   approval is **not** external-client permission.
3. While an adult Everyday session is current, an authenticated same-origin Owner client posts
   `POST /api/household/tools/connect` with the existing Owner cookie and CSRF header. This route
   uses only saved host settings and the runtime's actual context; request-body URLs, credentials
   and context cannot replace them. It connects, but never captures, moves or changes settings.
4. The response confirms attachment only after the host acknowledgement and returns `device_id`,
   `homebody_context` and `maximum_seconds: 300`. Give the MCP caller that scope plus its separate
   caller credential. The dashboard provides Connect and Disconnect. With saved Owner renewal
   approval, five-minute/30-call leases renew within the original adult authority; transport
   failures get at most three bounded retries. Stop/privacy/Kids/permission changes terminate
   renewal. Reconnect explicitly after authority changes or app restart; never restore consent.

The robot opens `/v1/household/robot` over the authenticated outbound WebSocket. It advertises
canonical tool **names**, never schemas or remote URLs. The host uses the existing
`LocalToolTransport` and `everyday.tool_call` / `everyday.tool_result` correlation protocol.
Each call has a 15-second host deadline, messages are bounded, duplicates are refused and the
robot attachment allows at most thirty robot calls in five minutes. These are external-tool
calls, not microphone commands; no voice permission is borrowed.

## One MCP endpoint

Send requests to `POST /mcp` with:

- `Authorization: Bearer <caller credential>`
- `X-Reachy-Device-Id: <device_id returned by Owner connect>`
- `params._meta.homebody_context`: the exact current context returned by Owner connect.

For example, `tools/list` has params containing only that `_meta`. `tools/call` adds `name`
and `arguments`, such as `get_current_time` with `{}`. Use only read-only calls for onboarding;
listing power/camera/home-control tools does not authorize executing them.
No transcript, target URL or generic remote-call argument exists.

One `tools/list` contains the canonical host catalogue plus that robot's approved canonical
local tools. Host names remain advertised even if their optional integration is unconfigured;
execution then reports unavailable, never silently falls back. One `tools/call` route dispatches
host tools to `EverydayTools` and robot tools to the bound callback transport. The separate legacy
robot `/mcp` is retained for compatibility but is not needed by a client using this composed surface.

The host refuses lists and calls without a matching live adult attachment. Robot and MCP tokens
are not interchangeable. Device/session generation is checked before dispatch and before accepting
results. Disconnect, replacement, a changed permission, Stop, Kids, privacy or Workspace invalidates
the attachment, cancels pending composed work and prevents stale results being reported as success.
The robot also checks its original runtime cancellation fence at execution and during capture.
Camera/permission revocation after capture prevents subsequent image analysis. An already-completed
physical/HA action cannot be rolled back by cancellation; a retired power session may therefore
end without a success acknowledgement. Never retry a timed-out mutation blindly.

## Voice and API limitations

Speech transports consume the same catalogue but retain their existing native callback lifetimes;
voice does not need the external MCP attachment or its separate external camera permission.
Everyday is independent of the Hermes **agent**, not necessarily independent of all text-model
reasoning. Realtime supports native function tools. GPT-Live's supported upstream contract exposes
`session.delegation.created` and the actual `session.input_transcript.delta` stream, not arbitrary
function calls or a fabricated final-utterance event. Its delegation uses the existing narrow
Everyday text model to select approved tools. **Speech-only semantic tool selection is API-dependent
and has not been achieved for GPT-Live.** No invented provider event or fabricated transcript is used.

This is an adult household service, not a replacement child-service endpoint. Kids retires adult
authority and continues to use its existing separate guarded flow. Physical wake, audio quality,
real provider delegation, real HA readback and physical safe-fold/camera behavior still need
operator-approved live acceptance. All verification here uses offline fixtures.
