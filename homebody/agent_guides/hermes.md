# Connect Reachy Mini (Homebody {{VERSION}}) to Hermes Agent

You are Hermes Agent, running on your owner's computer. Your owner asked you to connect their Reachy Mini robot to you. The robot runs the Homebody app at **{{ROBOT_URL}}**. Your owner's message gave you a one-time **setup code**.

When you are done:

- Reachy talks to a **separate Hermes profile named `reachy`**, through the Homebody bridge on this computer (port 8643).
- That profile has **no terminal, file, code, browser, delegation, cron or skill-writing tools**, because anyone in the room can talk to Reachy.
- Your own profile, tools and settings stay as they are.

## Ground rules

- Before you install software, change configuration or start a service, tell your owner what you will do and wait for their OK. One OK for the whole plan is enough.
- **Only send keys to `{{ROBOT_URL}}/api/agent-setup/pair`.** Never paste keys into chat, logs or other services.
- {{ROBOT_URL}} must be a private address: `10.x`, `172.16-31.x`, `192.168.x`, `100.64-127.x` (Tailscale) or a `.local` name. If it is not, stop and ask your owner.
- Never weaken the restrictions below to make something work. If a step fails, report the exact error to your owner.
- Each step has a check. Do not continue until it passes.

## 0. Check the robot

```bash
curl -s {{ROBOT_URL}}/api/agent-setup/status
```

**Check:** the reply contains `"state":"waiting"` and `"agent":"Hermes Agent"`. If it says `expired`, `idle` or `cancelled`, ask your owner to create a new setup message in Homebody → Settings → Connect your agent.

Find the Python that belongs to Hermes. The bridge must run in it:

```bash
HERMES_PY="$(head -1 "$(command -v hermes)" | sed 's/^#!//')"
"$HERMES_PY" -c "import hermes_cli, sys; print(sys.version)"
```

**Check:** it prints Python 3.11 or newer. If `hermes` is a wrapper script, use the `python` inside Hermes' own `venv/bin/` instead, usually `~/.hermes/hermes-agent/venv/bin/python`.

## 1. Create the `reachy` profile

```bash
hermes profile list
hermes profile create reachy --clone --description "Voice profile for Reachy Mini: no host tools"
```

If a `reachy` profile already exists, keep it and continue; steps 2 and 3 make it safe again.

`--clone` copies your model settings and keys. Open `~/.hermes/profiles/reachy/.env` and **delete every messaging-platform credential**, such as `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_*`, `WHATSAPP_*`, `SIGNAL_*` and `MATRIX_*`. That way this profile's gateway runs only the API server and never answers your chats a second time. Keep model and speech provider keys, such as `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` and `ELEVENLABS_API_KEY`.

## 2. Remove broad tools from the profile's API server

```bash
hermes -p reachy tools disable --platform api_server \
  terminal file code_execution browser delegation cronjob skills computer_use todo image_gen vision
hermes -p reachy tools list --platform api_server
```

**Check:** only harmless toolsets are enabled, such as `web`, `memory` and `session_search`. Never enable `terminal`, `file`, `code_execution`, `browser`, `delegation`, `cronjob`, `skills` or `computer_use` for this profile. Add others, such as `homeassistant` or `spotify`, only if your owner asks.

## 3. Turn on the profile's API server

Generate a key and append the settings to the profile's `.env`. Do not use `hermes config set` for these: it stores them in `config.yaml` in plain text.

```bash
P=~/.hermes/profiles/reachy
grep -q '^API_SERVER_KEY=' "$P/.env" 2>/dev/null || {
  KEY="$("$HERMES_PY" -c 'import secrets; print(secrets.token_hex(32))')"
  printf 'API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=8652\nAPI_SERVER_KEY=%s\n' "$KEY" >> "$P/.env"
}
chmod 600 "$P/.env"
```

Port 8652 keeps it apart from your own API server on 8642. The API server and the bridge both need `aiohttp`:

```bash
"$HERMES_PY" -c "import aiohttp, yaml" 2>/dev/null || "$HERMES_PY" -m pip install aiohttp pyyaml
```

If that venv has no `pip`, use `uv pip install --python "$HERMES_PY" aiohttp pyyaml`.

Start the profile's gateway as a background service:

```bash
hermes -p reachy gateway install && hermes -p reachy gateway start
```

Without systemd or launchd, run `nohup hermes -p reachy gateway run > ~/.hermes/profiles/reachy/gateway.log 2>&1 &` instead.

**Check:**

```bash
KEY="$(grep '^API_SERVER_KEY=' ~/.hermes/profiles/reachy/.env | cut -d= -f2-)"
curl -s http://127.0.0.1:8652/health
curl -s http://127.0.0.1:8652/v1/toolsets -H "Authorization: Bearer $KEY" | "$HERMES_PY" -c "
import json, sys
d = json.load(sys.stdin); d = d.get('data', d)
print(sorted(t['name'] for t in d if t.get('enabled')))"
curl -s http://127.0.0.1:8652/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"model":"reachy","messages":[{"role":"user","content":"Say OK"}]}'
```

You should see:

- `"status": "ok"` from `/health`;
- only the toolsets from step 2;
- a normal answer to "Say OK".

If the chat fails with an authentication or provider error, the profile has no working model credentials. Run `hermes -p reachy model`, or the same login you use for your own profile, and ask your owner to finish any browser login.

## 4. Get the bridge from the robot

The robot serves the bridge that matches its Homebody version. This script downloads it and checks every file against the robot's SHA-256 manifest:

```bash
"$HERMES_PY" - <<'PY'
import hashlib, json, pathlib, urllib.request
base = "{{ROBOT_URL}}/agent-setup/bridge"
dest = pathlib.Path.home() / ".hermes" / "homebody-bridge"
dest.mkdir(parents=True, exist_ok=True)
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

## 5. Run the bridge

The bridge uses the `reachy` profile's `API_SERVER_KEY` as the key between Reachy and the bridge. It listens on all interfaces so the robot can reach it:

```bash
"$HERMES_PY" ~/.hermes/homebody-bridge/hermes_reachy_bridge.py \
  --profile reachy --hermes-url http://127.0.0.1:8652 --host 0.0.0.0 --port 8643
```

Make it a service so it survives reboots. If a `homebody-bridge` service already runs from an earlier setup, for example before a Homebody upgrade, restart it with the new files (`systemctl --user restart homebody-bridge`) instead of starting a second copy.

**Linux:** write `~/.config/systemd/user/homebody-bridge.service`:

```ini
[Unit]
Description=Homebody bridge for Reachy Mini (Hermes profile reachy)
After=network-online.target

[Service]
ExecStart=<HERMES_PY> %h/.hermes/homebody-bridge/hermes_reachy_bridge.py --profile reachy --hermes-url http://127.0.0.1:8652 --host 0.0.0.0 --port 8643
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Replace `<HERMES_PY>` with the full path, then run `systemctl --user daemon-reload && systemctl --user enable --now homebody-bridge`. Run `loginctl enable-linger "$USER"` so it starts without a login.

**macOS:** create a LaunchAgent with the same command, or ask your owner which they prefer.

**Check:**

```bash
curl -s http://127.0.0.1:8643/health
```

You should see `"status": "ok"`, and the `hermes` entry in `agent_backends` should have `"ok": true`. If it says Hermes exposes broad host tools, go back to step 2. If the firewall blocks port 8643, allow it from the robot's address only, for example `sudo ufw allow from <robot-ip> to any port 8643`, and ask first.

## 6. Pair with the robot

Find the address of this computer that the robot can reach. It is the source address this computer uses toward the robot, and it is never `127.0.0.1`:

```bash
LAN_IP="$(python3 -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('{{ROBOT_HOST}}', 8042)); print(s.getsockname()[0])")"
echo "$LAN_IP"
```

Pair, keeping the reply in a private file. It can contain a token, so do not print it:

```bash
KEY="$(grep '^API_SERVER_KEY=' ~/.hermes/profiles/reachy/.env | cut -d= -f2-)"
umask 077
curl -s -o $HOME/.hermes/homebody-bridge/pair-reply.json -X POST {{ROBOT_URL}}/api/agent-setup/pair -H 'Content-Type: application/json' -d "{
  \"code\": \"<SETUP CODE FROM YOUR OWNER>\",
  \"backend\": \"hermes\",
  \"bridge_url\": \"http://$LAN_IP:8643\",
  \"api_key\": \"$KEY\",
  \"agent_name\": \"Hermes Agent on $(hostname)\"
}"
python3 -c "import json; d = json.load(open('$HOME/.hermes/homebody-bridge/pair-reply.json')); print({k: v for k, v in d.items() if k != 'mcp'}, 'mcp' in d)"
```

The robot saves nothing until it has reached your bridge with that key and found a ready, restricted profile. Read the reply:

- `"ok": true` (the last value printed is whether there is an `mcp` section): you are connected. Go to step 7 if there is an `mcp` section; otherwise delete the reply file and go to step 8.
- An `error` about the bridge: fix exactly what it says, then send the same request again. The code stays valid.
- `That setup code is not right`: do not guess; ask your owner for the code. After five wrong codes, setup ends.
- `No setup is waiting`: the code expired. Ask your owner for a new setup message.

## 7. Add Reachy as a tool for yourself (only when the reply has `mcp`)

This lets **your own** profile, not `reachy`, use Reachy: say something aloud, show an emotion, check its status. The token goes into your `.env`; the config only refers to it. Hermes' MCP client needs the `mcp` package from the 1.x line:

```bash
"$HERMES_PY" -c "import mcp.client.streamable_http as m; m.streamablehttp_client" 2>/dev/null || "$HERMES_PY" -m pip install "mcp<2"
R="$HOME/.hermes/homebody-bridge/pair-reply.json"
umask 077
"$HERMES_PY" - "$R" <<'PY'
import json, pathlib, sys
env = pathlib.Path.home() / ".hermes" / ".env"
token = json.load(open(sys.argv[1]))["mcp"]["headers"]["Authorization"].split(" ", 1)[1]
lines = [line for line in env.read_text().splitlines() if not line.startswith("HOMEBODY_MCP_TOKEN=")] if env.exists() else []
env.write_text("\n".join(lines + [f"HOMEBODY_MCP_TOKEN={token}"]) + "\n")
env.chmod(0o600)
PY
hermes config set mcp_servers.homebody.url "$("$HERMES_PY" -c "import json; print(json.load(open('$R'))['mcp']['url'])")"
hermes config set mcp_servers.homebody.headers.Authorization 'Bearer ${HOMEBODY_MCP_TOKEN}'
hermes mcp test homebody
rm -f "$R"
```

These commands target your default profile. If your owner talks to you through another profile, add `-p <that profile>` to each `hermes` command and use that profile's `.env`.

**Check:** `hermes mcp test homebody` connects and lists `get_status`, `announce` and `express_emotion`. The token is shown once; it now lives only in your `.env`.

## 8. Tell your owner

Report what you did:

- the `reachy` profile and the toolsets it has;
- the bridge service and its address;
- whether Realtime voice is available (`realtime_available` in the pair reply; it needs an `OPENAI_API_KEY` in the `reachy` profile's `.env`);
- that they can now say **"Hey Homebody"**.

Also tell them how to undo it:

- stop and remove the `homebody-bridge` service;
- run `hermes -p reachy gateway uninstall` and `hermes profile delete reachy`;
- clear the bridge URL in Homebody → Settings.
