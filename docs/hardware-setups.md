# Hardware setups: what runs where, and what is supported

Homebody runs on the computer that drives Reachy. This guide lists the setups you can use, which features each one offers, and how well each combination is proven. Read it before buying hardware.

> [!IMPORTANT]
> Reachy Mini Lite and Reachy Mini Wireless are different products. A Lite driven by a Raspberry Pi is a community setup, not a Wireless conversion: the Lite stays wall-powered, connects over USB data, and has no battery or onboard Wi-Fi. See Pollen Robotics' [Lite](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/get_started) and [Wireless](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/get_started) documentation.

## How to read the support labels

| Label | Meaning |
| --- | --- |
| ✅ **Reference-tested** | The named capability has reference hardware evidence. Original Lite + Pi 4 acceptance and current Lite + Jetson deployment are tracked separately; this is not whole-platform certification. Each robot still needs its own safety checks. |
| ◐ **Supported** | Implemented, covered by the automated test suite, and expected to work on this setup. It has not had physical acceptance on this exact setup yet. |
| 🧪 **Experimental** | Implemented capability without hardware acceptance on that setup. Other capabilities may already be deployed there. Expect to troubleshoot and report results. |
| — **Not available** | The hardware cannot do it, or the app deliberately does not offer it there. |

The app detects its host at start-up (Raspberry Pi, NVIDIA Jetson, Linux PC, Mac or Windows PC) and shows only the hardware controls that host can drive. Settings → Danger zone shows what it detected, for example *Running on: NVIDIA Jetson Orin Nano Developer Kit · local AI on TensorRT*.

## Reference evidence and limits

The Lite + Raspberry Pi 4 remains the original reference build and mounting/power history. A Lite + Jetson host now has reference deployment of the app, camera, Home Assistant integration, power lifecycle, silent speaker volume/mute and managed updates. The stale “never run on Jetson” description no longer applies.

This does **not** verify every Jetson GPU vision model, TensorRT/CUDA gesture path, GPIO button, Bluetooth mapping or live acoustic echo/barge-in behaviour. New camera startup/lifecycle repairs and safe joystick/Center controls still require supervised acceptance. The camera's private HTTPS frame fallback runs only after explicit **Start**, at a lower frame rate than WebRTC; a signalling connection is not proof of decoded video.

See [speaker volume](speaker-volume.md), [managed updates](managed-updates.md) and [release status](release-status.md). Updates replace Reachy software/dashboard assets, not an Android APK. Robot release **0.5.2** and a separately updated host bridge must not be treated as one deployment.

## The three hosting roles

Every setup has the same three jobs. They can live on one machine or be spread over your network.

| Role | Runs | Needs |
| --- | --- | --- |
| **Robot computer** | The Reachy daemon and this app: wake word, audio, camera, motor safety, phone UI. | USB to a Lite (or the Wireless' own CM4). Light CPU load. |
| **Bridge / agent host** | Narrow Everyday and Kids backends, speech providers, optional native Hermes Workspace, or legacy Hermes/OpenClaw conversation adapters. | Optional trusted host. [Agent-led setup](agent-setup.md) connects the basic adapter; [subscription/native setup](subscription-reasoning.md) is explicit and separate. Credentials stay here. |
| **Local AI server** *(optional)* | An OpenAI-compatible vision model, e.g. Ollama. Used by the local vision features. | A GPU, an Apple Silicon Mac, or a Jetson. Can be the robot computer or another machine on your LAN. |

Because the local AI server is reached over HTTP, a Raspberry Pi robot computer can use a vision model running on a Jetson or GPU PC elsewhere in the house.

## Choose a setup

| | Lite + PC (Linux or Windows) | Lite + Mac | Lite + Raspberry Pi 4/5 | Lite + Jetson Orin Nano | Reachy Mini Wireless |
| --- | --- | --- | --- | --- | --- |
| Official Pollen setup | Yes | Yes | No (community) | No (community) | Yes |
| This app's status | ◐ | ◐ | ✅ Pi 4 (original reference) | Reference-deployed core; per-feature acceptance below | ◐ |
| Always-on, dedicated | Your computer must stay on | Your Mac must stay on | Yes | Yes | Yes, battery |
| On-device AI | Depends on GPU | Core ML, Apple Silicon | CPU only | GPU (CUDA, TensorRT) | CPU only |
| Best for | Development, trying it out | Development, local vision with Ollama | A dedicated, proven robot | Local vision and private AI in one box | The official untethered robot |

**Choose by evidence, not GPU promises:** Pi 4 has the original reference guide; Pi 5 is expected, not reference-tested. Jetson is now a deployed community robot host, but its optional accelerator/vision/GPIO/controller features still need their own acceptance. A separate trusted local AI server is optional on either setup. Keep separate approved supplies, ventilation, cable strain relief and clear motor space; neither is an official Wireless conversion.

## Feature support by setup

| Feature | PC | Mac | Raspberry Pi | Jetson | Wireless | Notes |
| --- | :---: | :---: | :---: | :---: | :---: | --- |
| Phone dashboard, Standby/Awake/Meeting/Sleep, app lifecycle | ◐ | ◐ | ✅ | ✅ core reference | ◐ | Per-installation safety checks remain required. |
| Guarded wake, bounded movement, Stop, fold before torque off | ◐ | ◐ | ✅ | ✅ power reference | ◐ | Clear-space/fold checks mandatory; not every motion preset is physically accepted. |
| Local wake word (Hey Homebody, Hey Hermes, Okay Nabu, Hey Reachy) | ◐ | ◐ | ✅ | 🧪 | ◐ | Runs on the robot computer's CPU. |
| Local live camera viewer | ◐ | ◐ | ✅ | ✅ camera reference | ◐ | Explicit Start; WebRTC or lower-rate private HTTPS fallback. |
| Camera joystick / newer safe controls | ◐ | ◐ | ◐ | ◐ | ◐ | Separate movement opt-in; supervised acceptance remains required. |
| Voice engines: Pipeline / Realtime / GPT-Live preview | ◐ | ◐ | ◐ | ◐ | ◐ | Purpose routing is separate. Prior Pi speech evidence does not validate GPT-Live echo/barge-in. |
| Voice conversation through OpenClaw | ◐ | ◐ | ◐ | ◐ | ◐ | Bridge-side; independent of the robot computer. Verified against a live OpenClaw 2026.9.8 Gateway; not yet spoken to on a robot. |
| Supervised Kids Mode | ◐ | ◐ | ✅ | 🧪 | ◐ | Adult supervision always required. |
| Home Assistant ESPHome device | ◐ | ◐ | ◐ | ✅ integration reference | ◐ | Controls/camera/Assist are separate opt-ins; spoken Assist acceptance remains pending. |
| Silent speaker volume/mute; managed updates | ◐ | ◐ | ◐ | ✅ reference | ◐ | Named playback backend and managed installation prerequisites apply. |
| On-device hand gestures (CPU) | ◐ | ◐ | ◐ | 🧪 | ◐ | |
| Gesture detection on an accelerator | 🧪 CUDA, DirectML | 🧪 Core ML | — | 🧪 TensorRT, CUDA | — | Needs an onnxruntime build with that provider; falls back to the CPU. |
| Local vision model for camera questions | 🧪 with a GPU | 🧪 Apple Silicon | ◐ via a server on the LAN | 🧪 on the device | ◐ via a server on the LAN | Any OpenAI-compatible vision server. |
| Kids I Spy with a local vision model | 🧪 | 🧪 | 🧪 | 🧪 | 🧪 | Configured on the bridge host; moderation stays on OpenAI. |
| Physical green/red GPIO buttons | — | — | ◐ (GPIO17 verified electrically) | 🧪 | — | Needs a GPIO header and `gpiod`. |
| Bluetooth DualShock 4 / DualSense controller | ◐ Linux only | — | ◐ (BlueZ verified) | 🧪 | ◐ | Needs BlueZ and evdev, so Linux only. |
| Power off the robot computer from the phone | — | — | ◐ | 🧪 | ◐ | Deliberately never offered on a desktop or laptop. |
| Offline speech (no cloud STT/TTS) | Hermes host | Hermes host | Hermes host | Hermes host | Hermes host | Set by the Hermes host's own speech configuration, not by this app. |

## Lite + PC or Mac

Follow Pollen's [official Lite setup](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/get_started), then [install the app](../README.md#install-the-reachy-app).

- The app installs on macOS and Windows: the Linux-only `evdev` and `gpiod` packages are skipped automatically.
- GPIO buttons and the Bluetooth controller are hidden; Bluetooth control needs a Linux host. A Linux PC with Bluetooth can use the controller.
- **Power off** is not offered, so the phone can never shut down your computer.
- On a Mac, the standard onnxruntime build includes Core ML, so *Automatic* acceleration uses it for gesture detection.
- On Windows, install `onnxruntime-directml` instead of `onnxruntime` for DirectML. Windows support is best-effort: it is covered by code paths and tests, not by a Windows robot.
- **Local vision on a Mac or GPU PC:** install [Ollama](https://ollama.com), run `ollama pull qwen2.5vl:3b`, then set Settings → *Local vision and AI acceleration* → *Vision server URL* to `http://127.0.0.1:11434/v1`.

## Lite + Raspberry Pi

This is the original reference setup. Follow the [Lite + Raspberry Pi 4 companion-host guide](lite-raspberry-pi-4.md) for parts, power and mounting. A Raspberry Pi 5 is expected to behave the same way but is not reference-tested.

- **Physical buttons:** wire them as described in [OPERATIONS.md](../OPERATIONS.md#physical-gpio-buttons), then enable them under Robot → *Physical buttons*.
- **Bluetooth controller:** see [Bluetooth setup](#bluetooth-controller-setup).
- **Local vision:** a Pi is too small for a vision model. Point *Vision server URL* at a Jetson, a GPU PC or a Mac on your network, for example `http://VISION_HOST:11434/v1`. Make sure that server is only reachable on your trusted LAN.

## Lite + NVIDIA Jetson Orin Nano

The app and Reachy SDK have now run on the Jetson reference host. App/camera/HA/power/speaker/managed-update deployment is established; local GPU vision, accelerated gestures, GPIO, controller and physical live echo/barge-in acceptance are **not** implied. Treat your own setup and optional features as acceptance work. A configured trusted local vision server can keep image inference at home; merely choosing Jetson does not make cloud speech or vision local.

**Suggested order:**

1. **Choose a compatible JetPack image** and a power mode within the board, supply and cooling limits. JetPack 6 is a starting point, not a universal compatibility guarantee. NVMe helps with large optional models.
2. **Python version:** JetPack 6 ships Python 3.10, and this app needs Python 3.11 or newer. Use a [uv](https://docs.astral.sh/uv/)-managed Python 3.11 or 3.12 for the Reachy SDK and the app.
3. **Smoke-test the Reachy SDK** first using [operations](../OPERATIONS.md): start with folded/motor-off checks; perform wake, camera and audible tests only with owner supervision and explicit consent. Stop if any check fails.
4. **Install and start the app.** Settings → Danger zone should say *Running on: NVIDIA Jetson …*.
5. **Local vision:**
   1. Install Ollama, whose installer supports Jetson.
   2. Run `ollama pull qwen2.5vl:3b`. `gemma3:4b` is an alternative; `moondream` is smaller and faster but less precise.
   3. Enable *Use a local vision model*, keep the URL at `http://127.0.0.1:11434/v1`, and press **Test vision server**.
   4. Ask a question from the camera card on the Robot tab.
   5. The Orin Nano's 8 GB is shared between CPU and GPU, so avoid running a large chat model on the same board.
6. **Gesture acceleration (optional):**
   1. Install an `onnxruntime-gpu` build that matches your JetPack and Python version. NVIDIA's Jetson AI Lab package index provides them.
   2. Leave *On-device AI accelerator* on *Automatic*. Danger zone then shows *local AI on TensorRT* or *CUDA*.
   3. The first start builds TensorRT engines into `~/.cache/homebody/onnx`.
   4. If no matching build exists, gesture inference can fall back to CPU; verify throughput and robot responsiveness rather than assuming equivalence.
7. **GPIO buttons:**
   1. The Jetson header lines have higher numbers than the Pi's BCM pins. Find yours with `gpioinfo`, and enter those line numbers under Robot → *Physical buttons*.
   2. Check that the pins are configured as GPIO inputs in your Jetson pinmux.
8. **I Spy on the Jetson:** if the bridge also runs here, set `REACHY_ISPY_VISION_URL=http://127.0.0.1:11434/v1` and a vision model that supports JSON-schema output. See [companion/README.md](../companion/README.md).

## Bluetooth controller setup

Linux with a Bluetooth radio is required; controls are hidden on Mac/Windows. Install BlueZ and joystick support, give the **app service account** access to input devices, and verify its BlueZ D-Bus/polkit permissions. Do not grant blanket passwordless sudo to the dashboard.

Only DualShock 4 / DualSense identities with implemented mappings are accepted; other controllers need separate validation. Use Share + PS (DS4) or Create + PS (DualSense), then Robot → Scan → Pair & connect → Enable controller movement. Input remains opt-in and bounded through the existing action queue; Kids/privacy/power gates remain authoritative. Real mapping acceptance is still pending, including Jetson. DS4 gyro/touchpad/rumble extensions are experimental. See [operations](../OPERATIONS.md) for deployment checks.

## Reachy Mini Wireless

Use Pollen's [official Wireless setup](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/get_started) and install the app on the robot.

- The onboard CM4 behaves like the Raspberry Pi row above: CPU-only AI.
- Use a vision server elsewhere on the LAN for local vision.
- The internal GPIO header is not exposed for buttons.

## Purpose modes are not hardware tiers

Everyday / Workspace / Kids describe authority; Pipeline / Realtime / GPT-Live preview describe speech. Everyday never inherits full agent memory/tools or falls back to them. Native Workspace requires explicit registered project/session selection and real Hermes identity/history. Exact phone approvals gate the planned execution flow, not every native discussion tool call. Discussion uses the worker's configured tools; “discuss only” is an instruction, not enforcement. Use owner-scoped read-only tools when that guarantee is needed; CWD is not a sandbox. Kids remains isolated from adult memory/tools with unchanged moderation, camera consent and lifecycle.

Optional [subscription text/vision](subscription-reasoning.md) uses saved host Hermes Codex auth with no paid text fallback and genuine reasoning `none`. Validated reference models are `gpt-6-sol` for Everyday/planning/native Workspace and `gpt-6-luna` for Kids/I Spy, not universal defaults; `gpt-6.1-sol` currently lacks `none`. Speech API costs and Kids moderation remain. OpenClaw conversation compatibility is not native Workspace/subscription parity. [Agent-led connection](agent-setup.md) does not provision these host settings or sessions automatically.

## Privacy notes for local AI

- With *Use a local vision model* on, Realtime camera requests are answered by your vision server. OpenAI receives only the text description.
- Turning that setting off returns to sending the single frame to the Realtime model, as before.
- The vision server URL can only be changed with the current API key, because camera frames are sent there.
- A "local" server is only private if it stays on your trusted network. Do not point the URL at a public host.
- Kids Mode keeps OpenAI moderation even when I Spy uses a local vision model.

## Help us move a row to ✅

If you run one of the 🧪 or ◐ setups, please open an issue with:
- the hardware and OS version
- the Danger zone *Running on* line
- which checks in [OPERATIONS.md](../OPERATIONS.md) passed
- anything that failed

That evidence is what moves a setup from experimental to supported.
