# Agent access (MCP): let your agents use Reachy

Homebody can serve a [Model Context Protocol](https://modelcontextprotocol.io) endpoint. MCP-capable agents can then use Reachy as a gentle physical presence: Hermes Agent, OpenClaw, Claude Code, and others. "Remind me in Reachy's voice", "show that you're happy", "what's on the desk?".

It runs inside Homebody instead of as a separate Reachy app. The Reachy daemon runs one app at a time, and agent requests should queue alongside wake-word conversations and announcements rather than replace the companion.

## What agents can do

| Tool | What it does | Safety rules |
| --- | --- | --- |
| `get_status` | Reports whether Reachy is Awake, in Standby, Meeting or Sleep, whether someone seems present, and which tools work right now. | Read-only. Presence is reported only when presence sensing is on. |
| `announce` | Speaks a short message (up to 500 characters) after any conversation in progress. | Refused in Meeting, Sleep, privacy mode and child sessions. At most 6 messages per 10 minutes. |
| `express_emotion` | Plays one bounded emotion, for example `happy` or `surprised`. | Only while Reachy is already Awake with motors confirmed. It never wakes Reachy, and it is refused in child sessions and during camera control. |
| `look_and_describe` | Answers a question about the current camera view. | Off unless you allow it separately. Uses your local vision model, so the agent gets text, never an image. Refused in Meeting, Sleep and child sessions. |

There is no raw motor or joint control, no image download, no microphone access, and no way to change power modes, Kids Mode or settings. Every agent is limited to 30 requests a minute. When a rule blocks a request, the agent gets a plain-language reason it can relay, for example "Reachy is in Sleep mode".

## Turn it on

1. Open Homebody → **Settings → Agent access (MCP)**.
2. Turn on **Allow agent access** and save settings.
3. Optionally turn on **Let agents ask what Reachy sees**. This needs the local vision model and On-demand camera; see [hardware setups](hardware-setups.md).
4. Press **Create new token**. If a bridge API key is set, enter it in *Current API key* first. Copy the token: it is shown once and only a hash is stored. **Revoke token** cuts every agent off.

The endpoint is `http://<reachy-address>:8042/mcp`, shown in the same section.

## Connect an agent

Homebody speaks MCP over **Streamable HTTP** and answers with plain JSON, which is stateless. Configure it as a remote or HTTP MCP server with this header:

```text
Authorization: Bearer <your token>
```

**Claude Code**

```bash
claude mcp add --transport http homebody http://<reachy-address>:8042/mcp \
  --header "Authorization: Bearer <your token>"
```

**Hermes Agent, OpenClaw and other agents.** Add a remote HTTP MCP server with the endpoint URL and the `Authorization` header above, following that agent's MCP documentation. Give the agent a short instruction such as "Use the homebody tools to speak reminders aloud at home."

**Clients that only start local (stdio) servers.** Use a small stdio-to-HTTP adapter that can add headers, such as `mcp-remote`.

## Hosted agents (ChatGPT dots, Grok Bot)

Hosted agents run in their vendor's cloud. To reach Reachy they need a public HTTPS address, and usually an OAuth login rather than a bearer token. Homebody does not provide OAuth yet, so **do not** publish the endpoint to the internet with a bearer token alone. That is planned as a follow-up.

For your own devices away from home, use a private network such as Tailscale (tailnet only, never Funnel).

## Security notes

- Off by default. The endpoint returns 404 until you turn it on and create a token.
- The token is a random 256-bit secret, compared in constant time; only its SHA-256 is stored.
- Creating or revoking a token needs the current bridge API key, when one is set.
- Requests carrying a browser `Origin` from another site are refused, which blocks DNS-rebinding tricks from web pages.
- Requests are limited to 64 KB, and JSON-RPC batches are not accepted.
- Kids Mode wins. While a child session is active or the parent lock is held, every action is refused.
- Each call is logged with the tool name and outcome only, never the message text.
