# Operations runbook

This runbook covers private Reachy Mini deployments using the Reachy daemon, a Hermes host, and the companion bridge.

## Service map

| Component | Default location | Port/service |
|---|---|---|
| Reachy daemon | Reachy/Pi | `8000`, `reachy-mini-daemon.service` |
| Reachy Hermes settings | Reachy app process | `8042` |
| Home Assistant ESPHome bridge | Reachy app process | `6053`, optional/trusted LAN only |
| Hermes API Server | Hermes host | `8642`, Hermes gateway |
| Reachy companion bridge | Hermes host | `8643`, `hermes-reachy-bridge.service` |

## Pre-deployment checks

On the development/Hermes host:

```bash
uv run ruff check .
uv run pytest
uv build --wheel
```

Confirm no production configuration, `.env`, captured audio, or provider key is included in the wheel or Git diff.

On Reachy:

```bash
systemctl is-active reachy-mini-daemon.service
curl -fsS http://127.0.0.1:8000/api/daemon/status
```

Stop any running app before replacing its installed package:

```bash
curl -X POST http://REACHY_HOST:8000/api/apps/stop-current-app
```

## Wheel deployment

Copy the wheel to Reachy, then install it into the same Python environment used by the Reachy daemon:

```bash
scp dist/reachy_mini_hermes-*.whl REACHY_HOST:/tmp/

uv pip install \
  --python /path/to/reachy_mini/.venv/bin/python \
  --reinstall \
  --no-deps \
  /tmp/reachy_mini_hermes-*.whl
```

`--no-deps` is appropriate only after verifying that the Reachy environment already satisfies the package requirements. In particular, Realtime mode needs `websockets>=15,<17`.

Start the app:

```bash
curl -X POST http://REACHY_HOST:8000/api/apps/start-app/reachy_mini_hermes
```

Wait for both conditions:

```bash
curl -fsS http://REACHY_HOST:8000/api/apps/current-app-status
curl -fsS http://REACHY_HOST:8042/api/status
```

The app manager may report `running` several seconds before the settings server and media pipeline are ready.

## Companion bridge deployment

The bridge normally runs directly from the checked-out repository. After bridge code or Hermes-host credentials change:

```bash
systemctl --user restart hermes-reachy-bridge.service
systemctl --user status hermes-reachy-bridge.service
```

Validate with the private bearer token:

```bash
curl -H "Authorization: Bearer $API_SERVER_KEY" \
  http://127.0.0.1:8643/health
```

For Realtime mode, require:

- `hermes_api: true`;
- `realtime_available: true`;
- `realtime_model: gpt-realtime-2.1`.

For Kids Mode, additionally require:

- `kids_chat_available: true`;
- `kids_ispy_available: true` before offering the I Spy activity;
- `kids_tts_streaming_available: true`;
- successful authenticated `/v1/kids/chat` moderation/chat and `/v1/kids/speech/stream` PCM probes.

For an I Spy release, additionally verify with an adult supervising the robot: explicit consent before capture; the visible camera-search indicator; exactly five bounded retained viewpoints at `0° / −60° / −120° / +60° / +120°`; no commanded base segment larger than 60°; camera revocation before the neutral return and guesses; English and Dutch clue/hint/reveal flows; reveal no later than the sixth guess; Stop during both search and provider wait; bridge target deletion; safe neutral/fold; and no retained frame bytes. Never run this physical acceptance unattended.

For Agent Mode, verify the authenticated manifest and keep every allowlist explicit:

```bash
curl -fsS -H "Authorization: Bearer ***" \
  http://127.0.0.1:8643/v1/agent/capabilities
```

Confirm that the manifest contains no T4, shell, arbitrary-file, or maintenance capability. Test one configured read, one reversible Home Assistant action plus undo, one timer callback, and one draft whose exact phone approval executes once and rejects replay/edits. Keep Home Assistant/provider credentials on the Hermes host. `REACHY_AGENT_REMINDER_CALLBACK_URL` must target Reachy's private settings endpoint and its callback token must match Reachy's bridge bearer. Stop Agent and starting Kids Mode must invalidate in-flight work and pending approvals before speech; `/v1/agent/activity` remains metadata-only.

### Agent 0.6 Proactive Presence signal

Goal 1 accepts only an identity-free, authenticated local signal. Keep Home Assistant and Reachy credentials on the Hermes host. Configure the companion bridge's private environment file:

```dotenv
HASS_URL=http://HOME_ASSISTANT_HOST:8123
HASS_TOKEN=REDACTED_LONG_LIVED_TOKEN
REACHY_PRESENCE_ENTITY_ID=binary_sensor.example_office_presence
REACHY_PRESENCE_URL=https://REACHY_PRIVATE_HOST/api/presence/signal
REACHY_PRESENCE_POLL_SECONDS=3
```

The bridge accepts only `on` and `off` from that exact entity, sends only changed occupancy state, disables redirects, and forwards a normalized `home_assistant` observation without entity attributes or names. `unavailable`, `unknown`, authentication failures, and HTTP errors fail closed and are retried without inventing an occupancy change. The Reachy endpoint still rejects arbitrary metadata, raw sensor data, invalid values, and unauthenticated requests.

Enabling Presence does not enable motors or change Reachy's power mode. In Standby it records sanitized state without movement. Perform Awake acknowledgement acceptance only with clear space and an owner supervising Reachy.

### Agent 0.6 Initiative Policy

Goal 2 is disabled by default and remains a silent eligibility gate. The trusted Agent workspace exposes Quiet, Balanced, and Engaged modes; optional local quiet hours; and hourly/day budgets. Decisions are limited to `remain_silent`, `physical_acknowledgement`, and `offer_candidate`. Goal 2 does not generate text, invoke TTS, start Agent runs, or execute an offer.

Runtime ownership and safety suppression run before policy timing or confidence checks. Kids Mode/lock, privacy, Meeting/Sleep, Standby, disabled motors, active voice or announcement playback, camera control, face tracking, explicit robot actions, and runtime transitions therefore always win. Topic cooldowns, duplicate windows, and dismissal backoff are process-local and contain only bounded machine labels—never transcripts or personal context.

For initial acceptance, enable the policy in **Balanced** mode while Reachy remains folded in Standby, submit one trusted presence signal, and verify the latest decision is `remain_silent` with reason `not_awake`, initiative counters remain zero, and no robot action or speech occurs. Awake physical acceptance still requires an owner and clear space.

### Agent 0.6 Contextual Offers

Goal 3 is separately disabled by default. Enable **Contextual offers** only with Agent profile active and Reachy already safely Awake. The authenticated `POST /api/initiative/offers` route accepts one structured candidate from the hard source allowlist (`calendar`, `reminder`, `timer`, `home_assistant`, `weather`, `project`), a bounded machine topic/fingerprint, confidence, one question of at most 180 characters, and read-only accepted text of at most 240 characters.

Eligible offers use Goal 2 quiet hours, confidence thresholds, budgets, cooldowns, duplicate suppression, and dismissal backoff. Runtime ownership is checked both when the offer is submitted and immediately before speech. Reachy never wakes itself for an offer. After speaking, it listens once for a bounded English/Dutch yes/no response; the unlocked phone also exposes single-use Yes/No controls. A Yes only queues the exact prepared read-only text. It does not invoke an Agent capability, execute Home Assistant control, write, message, or otherwise perform a consequential action.

For safe no-motion acceptance, keep Reachy folded in Standby and submit a valid high-confidence offer with the configured bearer. Verify `queued: false`, decision `remain_silent`, reason `not_awake`, unchanged initiative counters, no announcement, and no robot action. Spoken yes/no acceptance requires Tim present, clear space, confirmed Awake torque, and Stop immediately available.

### Agent 0.6 Shared Physical Context

Goal 4 is disabled by default and never runs in the background. The trusted phone must explicitly start `POST /api/presentation/start`; each window lasts 5–30 seconds and requires Agent profile, safely Awake torque, camera access, Goal 2 Initiative Policy, Goal 3 Contextual Offers, idle voice/announcement/camera-control/face-tracking/action ownership, and no Kids or privacy mode. `POST /api/presentation/stop` cancels immediately.

The gate decodes each bounded JPEG in memory, retains only small central grayscale feature arrays for the active window, requires three stable changed samples, and clears all features on detection, Stop, expiry, power/profile/privacy change, settings disable, or failure. It performs no OCR, object classification, face recognition, identity inference, child monitoring, or background recording. Polled status exposes only state, reason, time remaining, sample/detection counts, and the fixed facts `semantic_analysis: false` and `frames_retained: 0`.

A stable intentional presentation submits the internal `presentation` source to the existing Goal 3 policy. The resulting Yes response only says to wake Reachy and explicitly ask it to look. No image is sent to a cloud service during the presentation window; the later explicit request uses the existing bounded one-frame Realtime camera path.

For safe deployment acceptance, leave Shared Physical Context disabled with Reachy folded in Standby and verify start is rejected without camera capture, announcement, initiative-budget use, or robot action. Physical camera acceptance requires Tim present, clear space, camera opt-in, confirmed Awake state, and Stop immediately available.

## Health checks

### Reachy app

```bash
curl -fsS http://REACHY_HOST:8042/api/status
```

Important fields:

- `runtime.state`;
- `runtime.power_mode`;
- `runtime.last_error`;
- `runtime.audio_frames_processed`;
- `runtime.turns_completed`;
- `runtime.interruptions`;
- `config.conversation_mode`;
- `config.camera_enabled`, `runtime.camera_captures`, and `runtime.camera_last_error`.

Test one local camera frame without returning its image content:

```bash
curl -X POST http://REACHY_HOST:8042/api/camera/test \
  -H 'Content-Type: application/json' \
  -d '{"confirm":"camera"}'
```

### Camera-feed joystick acceptance

Camera movement controls are a separate opt-in from the live camera and remain off by default. Do not enable them until Reachy is Awake, motor torque is confirmed, the surrounding base/head sweep is clear, and an adult is supervising. The overlay appears only over an active local WebRTC feed.

Verify a candidate artifact in this order:

1. Keep **Camera movement controls** disabled. Start the feed and confirm no joystick or movement route is usable.
2. Enable the controls in Settings, select left/right thumb placement, save, and confirm the overlay appears only while the feed is live.
3. In normal, native fullscreen, and app-fallback fullscreen, verify Stop and Exit remain visible in portrait and landscape, including mobile safe areas.
4. Touch inside the dead zone and confirm no movement. Drag slowly in all four directions and confirm intuitive camera pan/tilt, bounded speed, smooth small steps, and base assistance only near the head-yaw edge.
5. Release, cancel the pointer, rotate the device, background the app, leave fullscreen, and stop the feed. Every case must spring the visual stick to center, hold the current measured view, and reject delayed packets—never surprise-center physically.
6. Press **Center head & base** only with clear base space. Confirm the cancellable neutral movement completes. Press the in-overlay **Stop movement** during head-only, base-assisted, and Center movement and confirm no late motion.
7. Verify direct/replayed/malformed requests, Kids active/locked, Meeting, Sleep, Standby, privacy, disabled settings, robot-busy state, feed loss, and network reconnect all fail closed server-side.
8. Finish with the camera and overlay off, safely fold into Standby, verify the measured folded pose, and only then confirm torque is disabled. Retain no private camera frames.

Source tests and browser simulation do not constitute physical acceptance. Record the exact commit and wheel SHA-256 used for any supervised run; do not deploy or enable this control by default after a failed or incomplete gate.

### Home Assistant ESPHome acceptance

1. Stop the old Reachy Home Assistant app so only one process can bind TCP `6053`.
2. Enable only **ESPHome device bridge**, restart Reachy Hermes, and keep Assist, camera, and robot controls disabled.
3. Confirm `runtime.home_assistant.ready=true`, `connected=true`, device name `Reachy Mini E79627`, and no bridge error. Verify `runtime.home_assistant.bind_address` is Reachy's RFC1918 LAN address and `ss -ltn` shows `LAN_IP:6053`, never `0.0.0.0:6053`, a public address or the Tailscale/CGNAT address. Restrict inbound TCP `6053` to Home Assistant with the host/network firewall.
4. Verify Home Assistant reconnects the existing device rather than creating a duplicate and that live daemon, pose and system entities update. IMU, gesture, face-detection and Look At entities must remain unavailable when their source or guarded implementation is absent—never fake zero.
5. With controls disabled, send number/select/switch commands and verify no robot movement. Then locally enable controls, put Reachy Awake with clear space and supervision, and test only one ≤10-unit relative pose change. Standby, Kids, Meeting, Sleep, busy motion, camera control and >10-unit jumps must fail closed.
6. Enable HA camera locally, request one image, visually inspect it, then verify Kids, Meeting and Sleep disable capture. Disable the camera again.
7. Enable Assist only for supervised acoustic testing. Verify local wake, HA listening/thinking/speaking states, 16 kHz audio, same-peer TTS playback, Stop/privacy interruption, disconnect recovery and optional follow-up. Hermes and HA must never own microphone turns simultaneously.
8. Return to Standby, verify safe fold and torque release, then leave only the intended bridge capabilities enabled. This compatibility endpoint is plaintext; keep the RFC1918 LAN trusted and the TCP `6053` firewall restriction in place.

In Standby, `audio_frames_processed` should increase while daemon motor mode remains `disabled`. In Meeting or Sleep, the frame count should stop increasing.

### Reachy daemon

```bash
curl -fsS http://REACHY_HOST:8000/api/daemon/status
systemctl show reachy-mini-daemon.service -p ActiveState -p NRestarts
```

Check:

- daemon state is `running`;
- motor mode matches the requested app power state;
- control-loop error count remains zero;
- restart count does not increase unexpectedly.

### Logs

```bash
journalctl -u reachy-mini-daemon.service -f
journalctl --user -u hermes-reachy-bridge.service -f
```

Expected startup milestones include:

- app process started;
- settings server listening on `8042`;
- Reachy Hermes audio input/output rates logged;
- motors disabled when entering Standby.

Treat tracebacks, `Reachy voice runtime failed`, repeated WebSocket closures, and increasing daemon restarts as failures. Hardware GPU-device discovery warnings from ONNX Runtime may be harmless on a Pi when CPU inference continues successfully.

## Bluetooth and controller checks

This procedure applies only to **Reachy Mini Wireless**. Reachy Mini Lite and wired-only installations do not expose this Bluetooth controller feature as supported hardware.

1. Verify the adapter and BlueZ service:

   ```bash
   systemctl is-active bluetooth
   bluetoothctl show
   sudo rfkill list bluetooth
   ```

2. Verify the Reachy app service account belongs to `input` and can run `bluetoothctl show` through the target image's BlueZ D-Bus/polkit policy. Do not assume a `bluetooth` Unix group exists. Restart the Reachy daemon after changing groups.
3. Put the controller in pairing mode and use **Robot → Bluetooth gamepad → Scan**. Confirm Pair, Trust, and Connect all succeed.
4. Confirm the kernel created `/dev/input/js0` (or another `js*` device) and the app reports **Controller ready**.
5. With clear space and controller movement enabled, test one input at a time: D-pad look, Cross center, Square Happy, Triangle Surprised, Circle Stop.
6. Enter Meeting, Sleep, and Kids Mode and confirm movement inputs are rejected. Disconnect the controller and confirm the UI returns to Waiting/Disconnected without moving Reachy.
7. Do not map power, shutdown, dance, camera, agent, smart-home, or raw joint operations to the controller.

Useful diagnostics:

```bash
bluetoothctl devices Paired
bluetoothctl devices Connected
jstest /dev/input/js0
journalctl -u bluetooth -n 100 --no-pager
```

## Physical GPIO buttons

Optional green/red momentary buttons wired between a BCM GPIO pin and ground. The app enables the Pi's internal pull-up, so no resistor is needed. The feature is off by default. Enable it in **Robot → Physical buttons** (pins, hold time, on/off; saving re-opens the lines without a restart), or in `~/.local/share/reachy_mini_hermes/config.json` followed by an app restart:

```json
"gpio_buttons_enabled": true,
"gpio_chip": "/dev/gpiochip0",
"gpio_green_pin": 17,
"gpio_red_pin": 27,
"gpio_long_press_seconds": 2.0
```

`gpio_red_pin` has no default: set it to the pin you actually wired. Set a pin to `null` to leave that button out. On a Raspberry Pi 5 the header lines are also on `/dev/gpiochip0` in current Raspberry Pi OS. Check with `gpioinfo`.

| Button | Short press | Long press (≥ `gpio_long_press_seconds`) |
| --- | --- | --- |
| Red | Stop everything: voice turn, Agent work, Kids Mode (safe fold), announcements, offers, movement | Stop, then Sleep |
| Green | From Sleep/Meeting: Awake. Otherwise: listen now, as if the wake word was heard | Standby |

Safety behaviour:

- Red short press is always honoured, also while the runtime is starting and while Kids Mode locks the phone UI. The other gestures wait until the runtime is ready.
- A button that is already held when the app starts is ignored until it is released once. A button held for 30 s is treated as stuck and ignored until it is released.
- Debounce is applied in the kernel (libgpiod, 30 ms) and again in the app.
- A missing `gpiod` library, a busy line, or missing permissions only disables the buttons. Reachy keeps running and the reason is logged.

Acceptance:

1. Confirm the app's service user can open the chip: `ls -l /dev/gpiochip0` (group `gpio`), and `groups` lists `gpio`. Restart the Reachy daemon after changing groups.
2. Confirm no other process owns the lines: `gpioinfo | grep -E "line +(17|27):"` shows them unused before the app starts and `consumer="reachy-mini-hermes"` afterwards.
3. In Standby, press green briefly: Reachy wakes and listens. Press red briefly during the answer: speech and movement stop.
4. Hold red for 2 s: Reachy stops and goes to Sleep. Press green briefly: Reachy returns to Awake.
5. Start Kids Mode and press red briefly: the session ends with a safe fold.
6. Hold a button while restarting the app: nothing happens until it is released and pressed again.

## Power controls

Use the settings UI where practical. API equivalents:

```bash
curl -X POST http://REACHY_HOST:8042/api/power \
  -H 'Content-Type: application/json' \
  -d '{"mode":"standby"}'

curl -X POST http://REACHY_HOST:8042/api/power \
  -H 'Content-Type: application/json' \
  -d '{"mode":"meeting","duration_minutes":60}'

curl -X POST http://REACHY_HOST:8042/api/power \
  -H 'Content-Type: application/json' \
  -d '{"mode":"sleep"}'
```

Stop only the voice app:

```bash
curl -X POST http://REACHY_HOST:8042/api/app-off \
  -H 'Content-Type: application/json' \
  -d '{"confirm":"off"}'
```

The Pi shutdown endpoint intentionally requires `{"confirm":"shutdown"}`. Do not call it as a routine health test.

## Cooling-maintenance acceptance checklist

After installing or changing a heatsink/fan:

1. Inspect that no cable, camera ribbon, speaker lead, or motor path is pinched.
2. Confirm the heatsink does not contact exposed components or obstruct Reachy's movement.
3. Power on the Pi and verify the fan physically spins under its configured trigger condition.
4. Check current temperature:

   ```bash
   vcgencmd measure_temp
   cat /sys/class/thermal/thermal_zone0/temp
   ```

5. Check Raspberry Pi throttling history:

   ```bash
   vcgencmd get_throttled
   ```

   A clean result is `throttled=0x0`. Nonzero values can include historical undervoltage or thermal events; decode them before concluding that the current state is bad.

6. Start the Reachy daemon and voice app.
7. Confirm daemon restart count, control-loop errors, motor mode, and app status.
8. Leave the robot operating long enough to observe steady-state temperature.
9. Perform the human audio acceptance sequence below.

## Human audio acceptance

1. Leave Reachy in Standby and confirm motors are relaxed.
2. Say **“Hey Hermes”** once at normal speaking volume and distance; repeat the initial-wake check with **“Okay Nabu”** and **“Hey Reachy.”**
3. Ask a simple social question; verify the native Realtime response begins promptly.
4. Interrupt Reachy naturally while it is speaking; verify playback clears and the new turn is heard. In pipeline mode, confirm each configured wake phrase can also interrupt playback.
5. Ask a non-consequential Hermes tool question, such as checking a sensor state.
6. With on-demand camera enabled, ask **“What do you see?”** and verify one camera capture is logged.
7. Start a supervised Kids Mode session, verify the parent lock, one moderated child turn, Flash PCM streaming, parent stop, safe fold, and continued transcript/status redaction.
8. Verify the final answers match the fresh image or Hermes tool result rather than an unverified claim.
9. Exercise Meeting, Standby, and Sleep from the UI.
10. Review logs for tracebacks and record temperature after the test.

## Soak test

Before declaring a deployment stable:

- issue at least 30 app-status requests;
- issue at least 30 daemon-status requests;
- ping Reachy at least 20 times and report packet loss;
- confirm zero new daemon or bridge restarts;
- confirm zero new runtime tracebacks;
- verify the final power state and motor mode.

## Rollback

Keep the previous known-good wheel until acceptance passes.

```bash
curl -X POST http://REACHY_HOST:8000/api/apps/stop-current-app
uv pip install --python /path/to/reachy_mini/.venv/bin/python \
  --reinstall --no-deps /path/to/previous/reachy_mini_hermes.whl
curl -X POST http://REACHY_HOST:8000/api/apps/start-app/reachy_mini_hermes
```

The user configuration is stored separately from the wheel, so rollback normally preserves settings. If a future release changes the configuration schema incompatibly, back up `~/.local/share/reachy_mini_hermes/config.json` before deployment.
