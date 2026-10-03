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

Hosted agents run in their vendor's cloud, so they need a public HTTPS address and sign in with OAuth instead of a bearer token. Homebody acts as its own small OAuth 2.1 server, so you need no extra account. You approve each agent once with a one-time code.

**1. Point an HTTPS tunnel at the sign-in listener, never at the dashboard.** While sign-in is on, Homebody runs a second, separate listener (default `127.0.0.1:8043`) that serves only:

- `/mcp`, which accepts OAuth access tokens only, never the static token;
- `/oauth/` (register, authorize, token, revoke);
- `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`.

Every other path there answers 404. The dashboard on port `8042` serves none of these routes, so the tunnel can only reach what is meant to be public, whatever `Host` header your proxy sends. Use a tunnel that terminates TLS and point it at the listener, for example:

- Cloudflare Tunnel: `service: http://127.0.0.1:8043`
- Tailscale Funnel: `tailscale funnel --bg 8043`
- a reverse proxy on a VPS: `proxy_pass http://<reachy-address>:8043;` (then set **Sign-in listener address** to `0.0.0.0`, or to the address the proxy reaches, and firewall the port to the proxy)

Never point a tunnel at port `8042`. As a backstop, the dashboard answers 404 to any request carrying the public host name, but a proxy that rewrites `Host` would bypass that check.

**2. Turn on sign-in.** In **Settings → Agent access (MCP)**:

1. Enter the tunnel's address in **Public HTTPS address**, for example `https://reachy.example.com`. Use no path.
1. Leave **Sign-in listener address** and **port** at `127.0.0.1` and `8043` unless your tunnel runs on another machine. Settings shows whether the listener is running.
2. Turn on **Let hosted agents sign in (OAuth)** and save. If a bridge API key is set, enter it in *Current API key* first.

**3. Connect the agent.** In the agent's connector or MCP settings, add `https://reachy.example.com/mcp`. The agent then:

1. discovers the sign-in server;
2. registers itself;
3. opens Homebody's consent page in your browser, which shows the agent's name, where it will return to, and what it may do.

**4. Approve it.** Press **Create approval code** in Settings and type the code on the consent page.

- The code works once, for 10 minutes, and five wrong guesses burn it.
- **Deny** sends the agent away without access.

**Afterwards.**

- Settings lists connected agents.
- **Disconnect all hosted agents** signs every one out; they must be approved again.
- Changing the public address also signs them out, because tokens are bound to it.

How it works, for reviewers:

- **Standards:** OAuth 2.1 with Protected Resource Metadata (RFC 9728), Authorization Server Metadata (RFC 8414) and Dynamic Client Registration (RFC 7591).
- **Clients:** public clients only. PKCE S256 is required, and redirect URIs must match exactly.
- **Tokens:** bound to `<public address>/mcp` with resource indicators (RFC 8707). Access tokens last one hour. Refresh tokens rotate, and replaying an old one ends that agent's access.
- **Storage:** only hashes are kept, in `mcp-oauth.json` next to the config, with mode 0600.

For your own devices away from home, a private network such as Tailscale (tailnet only, no Funnel) with the bearer token is simpler.

## Security notes

- Off by default. The endpoint returns 404 until you turn it on and create a token (or turn on agent sign-in).
- OAuth sign-in and its public address need the current bridge API key to change, when one is set.
- The token is a random 256-bit secret, compared in constant time; only its SHA-256 is stored.
- Creating or revoking a token needs the current bridge API key, when one is set.
- Requests carrying a browser `Origin` from another site are refused, which blocks DNS-rebinding tricks from web pages.
- Requests are limited to 64 KB, and JSON-RPC batches are not accepted.
- Kids Mode wins. While a child session is active or the parent lock is held, every action is refused.
- Each call is logged with the tool name and outcome only, never the message text.
