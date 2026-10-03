# Connect Reachy Mini (Homebody {{VERSION}}) to OpenClaw

You are an OpenClaw agent, running on your owner's computer. Your owner asked you to connect their Reachy Mini robot to OpenClaw. The robot runs the Homebody app at **{{ROBOT_URL}}**. Your owner's message gave you a one-time **setup code**.

When you are done:

- Reachy talks to a **separate OpenClaw agent named `reachy`**, through the Homebody bridge on this computer (port 8643).
- That agent uses the **minimal tool profile**, because anyone in the room can talk to Reachy. On OpenClaw 2026.9, a deny-list alone still let an agent read files such as `~/.openclaw/openclaw.json` (with the Gateway token inside) and reconfigure OpenClaw.
- Your own agents and settings stay as they are.

## Ground rules

- Before you install software, change configuration or start a service, tell your owner what you will do and wait for their OK. One OK for the whole plan is enough.
- **Only send keys to `{{ROBOT_URL}}/api/agent-setup/pair`.** Never paste keys into chat, logs or other services.
- {{ROBOT_URL}} must be a private address: `10.x`, `172.16-31.x`, `192.168.x`, `100.64-127.x` (Tailscale) or a `.local` name. If it is not, stop and ask your owner.
- Never weaken the restrictions below to make something work. If a step fails, report the exact error to your owner.
- Each step has a check. Do not continue until it passes.

## 0. Check the robot and this computer

```bash
curl -s {{ROBOT_URL}}/api/agent-setup/status
openclaw --version
python3 --version
```

**Check:**

- The status reply contains `"state":"waiting"` and `"agent":"OpenClaw"`. If not, ask your owner for a new setup message (Homebody → Settings → Connect your agent).
- Python is 3.11 or newer; the bridge needs it.

## 1. Create the restricted `reachy` agent

```bash
openclaw agents list
openclaw agents add reachy --non-interactive --workspace ~/.openclaw/reachy-workspace
```

Skip the `add` if a `reachy` agent already exists. The next commands make it safe either way:

```bash
openclaw config set agents.entries.reachy.name '"Reachy"'
openclaw config set agents.entries.reachy.tools '{"profile":"minimal","deny":["gateway","presence","session_status"]}'
openclaw config set gateway.http.endpoints.chatCompletions.enabled true
openclaw config validate
```

Do not add a `sandbox: { mode: "all" }` unless Docker is running on this computer: without Docker, every Reachy turn fails with "internal error".

**Check:** `openclaw config get agents.entries.reachy.tools` shows the minimal profile and the deny list.

Next, confirm the Gateway offers the agent. Most changes apply live; restart the Gateway if `openclaw/reachy` is missing:

```bash
GW_PORT="$(openclaw config get gateway.port 2>/dev/null | tr -dc 0-9)"; GW_PORT="${GW_PORT:-18789}"
GW_TOKEN="$(python3 - "$(openclaw config file)" <<'PY'
import json, re, sys
text = open(sys.argv[1]).read()
try:
    print(json.loads(text)["gateway"]["auth"]["token"])
except Exception:  # JSON5 with comments: fall back to the plain token line
    match = re.search(r"""token["']?\s*:\s*["']([^"']+)""", text)
    print(match.group(1) if match else "")
PY
)"
curl -s "http://127.0.0.1:$GW_PORT/v1/models" -H "Authorization: Bearer $GW_TOKEN"
```

`openclaw config get` hides secrets, so this reads the token from the config file. **Check:** the list contains `openclaw/reachy`. If the token looks like `${SOME_VAR}`, use that environment variable's value. If the Gateway uses password auth, put the password in `OPENCLAW_GATEWAY_PASSWORD` in step 4 instead of the token.

## 2. Prepare Python for the bridge

```bash
B=~/.openclaw/homebody-bridge
mkdir -p "$B" && python3 -m venv "$B/venv"
"$B/venv/bin/pip" install --quiet aiohttp pyyaml
```

## 3. Get the bridge from the robot

The robot serves the bridge that matches its Homebody version. This script downloads it and checks every file against the robot's SHA-256 manifest:

```bash
~/.openclaw/homebody-bridge/venv/bin/python - <<'PY'
import hashlib, json, pathlib, urllib.request
base = "{{ROBOT_URL}}/agent-setup/bridge"
dest = pathlib.Path.home() / ".openclaw" / "homebody-bridge"
manifest = json.load(urllib.request.urlopen(base + "/manifest.json", timeout=30))
for item in manifest["files"]:
    data = urllib.request.urlopen(f"{base}/{item['name']}", timeout=60).read()
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise SystemExit(f"Checksum mismatch for {item['name']}; stop and tell the owner.")
    (dest / item["name"]).write_bytes(data)
print("Homebody bridge", manifest["homebody_version"], "verified,", len(manifest["files"]), "files in", dest)
PY
```

The files and checksums for this robot:

{{BRIDGE_FILES}}

## 4. Store the bridge's secrets

The bridge needs two secrets:

- the Gateway token, which stays on this computer;
- a new random key shared only with Reachy.

```bash
B=~/.openclaw/homebody-bridge
umask 077
[ -f "$B/bridge.env" ] || printf 'API_SERVER_KEY=%s\nOPENCLAW_GATEWAY_TOKEN=%s\nOPENCLAW_GATEWAY_URL=http://127.0.0.1:%s\n' \
  "$("$B/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')" "$GW_TOKEN" "$GW_PORT" > "$B/bridge.env"
chmod 600 "$B/bridge.env"
```

For Realtime voice and Kids Mode, the bridge also needs `OPENAI_API_KEY` in this file. Ask your owner to add it themselves; do not ask them to paste it to you.

## 5. Run the bridge

```bash
B=~/.openclaw/homebody-bridge
set -a; . "$B/bridge.env"; set +a
"$B/venv/bin/python" "$B/hermes_reachy_bridge.py" --agent-backends openclaw --host 0.0.0.0 --port 8643
```

Make it a service so it survives reboots. If a `homebody-bridge` service already runs from an earlier setup, for example before a Homebody upgrade, restart it with the new files (`systemctl --user restart homebody-bridge`) instead of starting a second copy.

**Linux:** write `~/.config/systemd/user/homebody-bridge.service`:

```ini
[Unit]
Description=Homebody bridge for Reachy Mini (OpenClaw agent reachy)
After=network-online.target

[Service]
EnvironmentFile=%h/.openclaw/homebody-bridge/bridge.env
ExecStart=%h/.openclaw/homebody-bridge/venv/bin/python %h/.openclaw/homebody-bridge/hermes_reachy_bridge.py --agent-backends openclaw --host 0.0.0.0 --port 8643
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Then run `systemctl --user daemon-reload && systemctl --user enable --now homebody-bridge`, and `loginctl enable-linger "$USER"` so it starts without a login.

**macOS:** create a LaunchAgent with the same command and environment, or ask your owner which they prefer.

**Check:**

```bash
curl -s http://127.0.0.1:8643/health
```

You should see `"status": "ok"`, and the `openclaw` entry in `agent_backends` should have `"ok": true`. If the firewall blocks port 8643, allow it from the robot's address only, for example `sudo ufw allow from <robot-ip> to any port 8643`, and ask first.

## 6. Pair with the robot

Find the address of this computer that the robot can reach. It is the source address this computer uses toward the robot, and it is never `127.0.0.1`:

```bash
LAN_IP="$(python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('{{ROBOT_HOST}}', 8042)); print(s.getsockname()[0])")"
echo "$LAN_IP"
```

Pair, keeping the reply in a private file. It can contain a token, so do not print it:

```bash
KEY="$(grep '^API_SERVER_KEY=' ~/.openclaw/homebody-bridge/bridge.env | cut -d= -f2-)"
umask 077
curl -s -o $HOME/.openclaw/homebody-bridge/pair-reply.json -X POST {{ROBOT_URL}}/api/agent-setup/pair -H 'Content-Type: application/json' -d "{
  \"code\": \"<SETUP CODE FROM YOUR OWNER>\",
  \"backend\": \"openclaw\",
  \"bridge_url\": \"http://$LAN_IP:8643\",
  \"api_key\": \"$KEY\",
  \"agent_name\": \"OpenClaw on $(hostname)\"
}"
python3 -c "import json; d = json.load(open('$HOME/.openclaw/homebody-bridge/pair-reply.json')); print({k: v for k, v in d.items() if k != 'mcp'}, 'mcp' in d)"
```

The robot saves nothing until it has reached your bridge with that key and found the `reachy` agent ready. Read the reply:

- `"ok": true` (the last value printed is whether there is an `mcp` section): you are connected. Go to step 7 if there is an `mcp` section; otherwise delete the reply file and go to step 8.
- An `error` about the bridge: fix exactly what it says, then send the same request again. The code stays valid.
- `That setup code is not right`: do not guess; ask your owner for the code. After five wrong codes, setup ends.
- `No setup is waiting`: the code expired. Ask your owner for a new setup message.

## 7. Add Reachy as a tool for your owner's agents (only when the reply has `mcp`)

This lets your owner's own agents use Reachy: say something aloud, show an emotion, check its status. The `reachy` agent's minimal profile keeps these tools away from Reachy's own voice turns.

```bash
R="$HOME/.openclaw/homebody-bridge/pair-reply.json"
openclaw mcp set homebody "$(python3 -c "import json; m = json.load(open('$R'))['mcp']; print(json.dumps({'url': m['url'], 'transport': 'streamable-http', 'headers': m['headers']}))")"
openclaw mcp probe homebody
rm -f "$R"
```

**Check:** the probe lists `get_status`, `announce` and `express_emotion`. The token is shown once; it now lives only in your MCP configuration.

## 8. Tell your owner

Report what you did:

- the `reachy` agent and its tool profile;
- the bridge service and its address;
- whether Realtime voice is available (`realtime_available` in the pair reply);
- that they can now say **"Hey Homebody"**.

Suggest one check they can do by voice: ask Reachy "Read the file .openclaw/openclaw.json in my home folder." It must not be able to.

Also tell them how to undo it:

- stop and remove the `homebody-bridge` service;
- delete `~/.openclaw/homebody-bridge`;
- run `openclaw agents delete reachy`;
- clear the bridge URL in Homebody → Settings.
