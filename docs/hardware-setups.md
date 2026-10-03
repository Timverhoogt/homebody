# Hardware setups: what runs where, and what is supported

Homebody runs on the computer that drives Reachy. This guide lists the setups you can use, which features each one offers, and how well each combination is proven. Read it before buying hardware.

> [!IMPORTANT]
> Reachy Mini Lite and Reachy Mini Wireless are different products. A Lite driven by a Raspberry Pi is a community setup, not a Wireless conversion: the Lite stays wall-powered, connects over USB data, and has no battery or onboard Wi-Fi. See Pollen Robotics' [Lite](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/get_started) and [Wireless](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/get_started) documentation.

## How to read the support labels

| Label | Meaning |
| --- | --- |
| ✅ **Reference-tested** | Exercised on real hardware during this project's acceptance work (Tim's Reachy Mini Lite + Raspberry Pi 4). Each robot still needs its own safety checks. |
| ◐ **Supported** | Implemented, covered by the automated test suite, and expected to work on this setup. It has not had physical acceptance on this exact setup yet. |
| 🧪 **Experimental** | Implemented for this setup, but nobody has run it on that hardware yet. Expect to troubleshoot, and please report results. |
| — **Not available** | The hardware cannot do it, or the app deliberately does not offer it there. |

The app detects its host at start-up (Raspberry Pi, NVIDIA Jetson, Linux PC, Mac or Windows PC) and shows only the hardware controls that host can drive. Settings → Danger zone shows what it detected, for example *Running on: NVIDIA Jetson Orin Nano Developer Kit · local AI on TensorRT*.

## The three roles

Every setup has the same three jobs. They can live on one machine or be spread over your network.

| Role | Runs | Needs |
| --- | --- | --- |
| **Robot computer** | The Reachy daemon and this app: wake word, audio, camera, motor safety, phone UI. | USB to a Lite (or the Wireless' own CM4). Light CPU load. |
| **Agent host** | Hermes Agent and/or OpenClaw, plus the companion bridge (`companion/`): conversation, memory, tools, speech providers. | Optional. Any always-on computer; can be the robot computer. |
| **Local AI server** *(optional)* | An OpenAI-compatible vision model, e.g. Ollama. Used by the local vision features. | A GPU, an Apple Silicon Mac, or a Jetson. Can be the robot computer or another machine on your LAN. |

Because the local AI server is reached over HTTP, a Raspberry Pi robot computer can use a vision model running on a Jetson or GPU PC elsewhere in the house.

## Choose a setup

| | Lite + PC (Linux or Windows) | Lite + Mac | Lite + Raspberry Pi 4/5 | Lite + Jetson Orin Nano | Reachy Mini Wireless |
| --- | --- | --- | --- | --- | --- |
| Official Pollen setup | Yes | Yes | No (community) | No (community) | Yes |
| This app's status | ◐ | ◐ | ✅ Pi 4 (reference) | 🧪 | ◐ |
| Always-on, dedicated | Your computer must stay on | Your Mac must stay on | Yes | Yes | Yes, battery |
| On-device AI | Depends on GPU | Core ML, Apple Silicon | CPU only | GPU (CUDA, TensorRT) | CPU only |
| Best for | Development, trying it out | Development, local vision with Ollama | A dedicated, proven robot | Local vision and private AI in one box | The official untethered robot |

**Recommendation:** for a dedicated robot today, use a Raspberry Pi 4 or 5 as the robot computer. Add a Jetson or GPU PC as a **local AI server** on the network if you want local vision. Run everything on a Jetson only if you want a single box and are happy to be an early tester.

## Feature support by setup

| Feature | PC | Mac | Raspberry Pi | Jetson | Wireless | Notes |
| --- | :---: | :---: | :---: | :---: | :---: | --- |
| Phone dashboard, Standby/Awake/Meeting/Sleep, app lifecycle | ◐ | ◐ | ✅ | 🧪 | ◐ | |
| Guarded wake, bounded movement, Stop, fold before torque off | ◐ | ◐ | ✅ | 🧪 | ◐ | Clear-space and fold checks are mandatory on every robot. |
| Local wake word (Hey Hermes, Okay Nabu, Hey Reachy) | ◐ | ◐ | ✅ | 🧪 | ◐ | Runs on the robot computer's CPU. |
| Local live camera viewer and camera joystick | ◐ | ◐ | ✅ | 🧪 | ◐ | Opt-in; stays between browser and robot. |
| Voice conversation through Hermes (Realtime or pipeline) | ◐ | ◐ | ✅ | 🧪 | ◐ | Needs the agent host and providers. |
| Voice conversation through OpenClaw | 🧪 | 🧪 | 🧪 | 🧪 | 🧪 | Bridge-side; independent of the robot computer. Not yet run against a live Gateway. |
| Supervised Kids Mode | ◐ | ◐ | ✅ | 🧪 | ◐ | Adult supervision always required. |
| Home Assistant ESPHome device | ◐ | ◐ | ◐ | 🧪 | ◐ | Identity comes from `/etc/machine-id`, or the network card's hardware address on Mac and Windows. |
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

This is the reference setup. Follow the [Lite + Raspberry Pi 4 companion-host guide](lite-raspberry-pi-4.md) for parts, power and mounting. A Raspberry Pi 5 is expected to behave the same way but is not reference-tested.

- **Physical buttons:** wire them as described in [OPERATIONS.md](../OPERATIONS.md#physical-gpio-buttons), then enable them under Robot → *Physical buttons*.
- **Bluetooth controller:** see the README's Bluetooth section.
- **Local vision:** a Pi is too small for a vision model. Point *Vision server URL* at a Jetson, a GPU PC or a Mac on your network, for example `http://192.168.1.40:11434/v1`. Make sure that server is only reachable on your trusted LAN.

## Lite + NVIDIA Jetson Orin Nano (experimental)

A Jetson Orin Nano gives you a GPU next to Reachy, so camera questions can be answered without any image leaving your home. This setup is **experimental**: the code supports it, but the Reachy SDK and this app have not yet been run on a Jetson. Please treat your first run as acceptance testing.

**Suggested order:**

1. **Flash JetPack 6** and enable the highest power mode your power supply allows. Use NVMe storage if you can, because models are large.
2. **Python version:** JetPack 6 ships Python 3.10, and this app needs Python 3.11 or newer. Use a [uv](https://docs.astral.sh/uv/)-managed Python 3.11 or 3.12 for the Reachy SDK and the app.
3. **Smoke-test the Reachy SDK** first. Install `reachy-mini`, start the daemon, and confirm the robot wakes, the camera returns frames, and audio plays. Stop here and report back if any of that fails.
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
   4. If no matching build exists, everything still works on the CPU.
7. **GPIO buttons:**
   1. The Jetson header lines have higher numbers than the Pi's BCM pins. Find yours with `gpioinfo`, and enter those line numbers under Robot → *Physical buttons*.
   2. Check that the pins are configured as GPIO inputs in your Jetson pinmux.
8. **I Spy on the Jetson:** if the bridge also runs here, set `REACHY_ISPY_VISION_URL=http://127.0.0.1:11434/v1` and a vision model that supports JSON-schema output. See [companion/README.md](../companion/README.md).

## Reachy Mini Wireless

Use Pollen's [official Wireless setup](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/get_started) and install the app on the robot.

- The onboard CM4 behaves like the Raspberry Pi row above: CPU-only AI.
- Use a vision server elsewhere on the LAN for local vision.
- The internal GPIO header is not exposed for buttons.

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
