# OpenClaw support for the project voice workflow

## Verdict

**The installed OpenClaw has the required native building blocks, but the complete Homebody workflow is not wired to it.** Existing conversation routing is not equivalent to source-grounded project discussion, authorized project execution, shared desktop identity, or verified ready-to-test reporting.

The target remains: discuss the actual roadmap → select the next item → explicitly authorize work → display real run/tool progress → report a verified result and ask for testing. Homebody currently implements the read-only roadmap slice, not coding execution, for either backend.

## Verified against the owner installation

Read-only inspection found OpenClaw **2026.9.8**, an active Gateway, a successful Gateway health RPC and a successful `sessions.list` RPC. The configured agent is `main`; no dedicated Reachy agent was listed. The Gateway's OpenAI-compatible chat-completions and Responses endpoints are disabled.

The installed CLI includes:

- `openclaw tui <target>` for continuing a Gateway-owned session.
- `openclaw attach <target>` for a session-scoped Claude Code/MCP attachment.
- `openclaw sessions tail` for human-readable trajectory progress.

Installed package source contains session-bound `chat.send`, `chat.history`, `chat.abort`, `sessions.resolve`, `sessions.subscribe`, session visibility/scope checks and `x-openclaw-session-key` routing. This is source/CLI capability evidence, not a completed Homebody integration or an executed coding run.

No OpenClaw configuration, agent policy, token, pairing, coding harness or running job was changed by the scan.

## What Homebody already supports

[`companion/agent_backends.py`](../companion/agent_backends.py) provides an `OpenClawBackend`; [`tests/test_openclaw_bridge.py`](../tests/test_openclaw_bridge.py) covers allowlisted-agent routing and mixed-backend operation with explicit upstream fixtures. The adapter uses the Gateway's HTTP chat-completions endpoint, selects an allowlisted agent, supplies a bridge-owned `user` identity, forces `stream: false`, and does not forward caller-supplied tools or arbitrary routing credentials.

The primary/default agent is rejected unless explicitly opted in. This is a Homebody engineering boundary, not an OpenClaw limitation. A dedicated agent is the safe default for unattended robot access.

The deployed host bridge is configured for **Hermes only**. Its live model listing does not expose an OpenClaw model. Simply selecting an OpenClaw-looking model name does not activate a missing backend.

The new roadmap flow in [`companion/hermes_reachy_bridge.py`](../companion/hermes_reachy_bridge.py) uses a restricted bridge-owned reasoning loop and the host catalog. It does **not** delegate that reasoning to an OpenClaw-native session. Project follow-ups are bounded to the already-selected roadmap; there is no project launcher or coding authority.

## What the native OpenClaw platform provides

OpenClaw documents Gateway-owned session state shared across its Control UI, mobile clients, ACP and terminal, plus session-scoped coding-harness attachment. These are suitable foundations for making Reachy another surface on an existing session rather than a second independent assistant.[2]

Its WebSocket protocol exposes chat, agents, sessions, approvals and event families, with role/scope checks. That is the appropriate integration surface for durable identity, live progress and cancellation.[3]

The HTTP chat-completions interface is useful for basic conversation, but agent selection and a stable `user` or session-key override do not themselves implement Homebody's project policy, approval UI or progress contract. API credential holders are trusted principals; broad gateway credentials must remain on the host, never on Reachy.[1]

## Recommended next increment

Build **one backend-neutral project/session adapter**, with a Hermes implementation and an OpenClaw Gateway implementation:

1. Bind an owner-selected project to an exact native backend session; expose the session identity and history in the existing Workspace. Do not silently attach Reachy to the owner's unrestricted primary session.
2. Add a separately authorized host-side execution contract. Keep roadmap reading and execution permissions distinct; scope project root, operations and approval to the exact request.
3. Map native run/tool/approval/cancellation events into the existing truthful Workspace timeline. Emit ready-to-test only from a verified artifact/test receipt, never from an assistant's prose or a run-start acknowledgement.

Preserve Homebody's owner pairing, Stop/privacy/Kids invalidation, microphone/camera/movement consent and text-only bounded transcript handling. Never render hidden reasoning or secrets from generic upstream events.

This is an integration gap, not a reason to install a second OpenClaw or Hermes on the Jetson. Basic OpenClaw conversation would additionally require an explicit agent/policy choice, host-held authentication and enabling the desired endpoint; those changes were not made during this inspection.

## Sources

[1] https://docs.openclaw.ai/gateway/openai-http-api
[2] https://docs.openclaw.ai/concepts/session-attachment
[3] https://docs.openclaw.ai/gateway/protocol
