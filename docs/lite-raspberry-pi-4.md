# Reachy Mini Lite + Raspberry Pi 4 companion host

> [!IMPORTANT]
> This is a community, self-built adaptation. It does **not** convert a Reachy Mini Lite into a Reachy Mini Wireless, and it does not claim electrical, mechanical, battery, IMU, radio, enclosure, or support equivalence. Pollen Robotics' official model distinction remains authoritative: Wireless has an onboard Raspberry Pi CM4, battery, and Wi-Fi; Lite is wall-powered and uses USB data to a computer.

## Evidence and support status

This guide deliberately labels what is known.

| Label | Meaning |
|---|---|
| **Official** | Stated in current Pollen Robotics documentation linked below. |
| **Reference-tested** | Exercised with Tim's Reachy Mini Lite and a Raspberry Pi 4 during this project's hardware acceptance work. It is not a general compatibility promise. |
| **Project-verified** | Covered by this repository's automated checks or runbooks, but still needs acceptance on each physical robot. |
| **TBD / untested** | Not proven by repository evidence or official documentation; do not infer a safe answer. |

**Reference build:** Reachy Mini Lite + Raspberry Pi 4 running the Reachy daemon and this app. The repository records daemon, app, camera, audio, network, safe-fold/torque and power-state acceptance on that reference deployment. The exact Pi model/RAM, OS image, power supply, mounting hardware, cable lengths and final physical arrangement have **not** been captured in public evidence and remain TBD pending Tim's confirmation.

![Conservative Reachy Mini Lite and Raspberry Pi 4 companion topology with separate power supplies, USB data, ventilation, strain relief and motor-clearance callouts](assets/lite-pi-overview.svg)

*Project-authored conservative topology. It deliberately shows off-robot Pi placement, separate supplies and USB data. It is not a photograph of Tim's final build and does not approve an attached mount or shared power.*

For official product imagery and exact reuse terms, see [Image credits and reuse notes](IMAGE_CREDITS.md).

## Before buying or mounting anything

Read the canonical sources first:

- [Official Reachy Mini Lite getting-started guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/get_started)
- [Official Lite hardware datasheet](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/hardware)
- [Official SDK installation guide](https://huggingface.co/docs/reachy_mini/SDK/installation)
- [Official SDK quickstart and daemon instructions](https://huggingface.co/docs/reachy_mini/SDK/quickstart)
- [Official troubleshooting and motor guidance](https://huggingface.co/docs/reachy_mini/troubleshooting)
- [This project's security model](../SECURITY.md) and [operations runbook](../OPERATIONS.md)

Stop if the official documentation for your hardware revision conflicts with this guide.

## Parts

### Required

- **Official:** assembled Reachy Mini Lite and its supplied robot power supply. The Lite must be powered from the wall; its computer-facing USB-C port does not power or charge the robot.
- **Reference-tested:** Raspberry Pi 4 used as the computer/companion host.
- A Pi power supply rated for the exact Raspberry Pi model and attached USB load.
- A known-good USB data cable from Reachy Mini Lite to the Pi.
- microSD or other supported Pi boot storage with enough space for the OS, SDK, app, model cache and logs.
- Network access for initial package/model downloads; a trusted LAN or VPN for remote management.
- Ventilated, electrically non-conductive placement or enclosure, plus strain relief for power and USB cables.

### Optional

- Ethernet for a more predictable management link.
- A separately powered USB hub **only if** the Pi cannot reliably supply the attached USB devices. Hub model and back-power behavior are **TBD / untested**.
- A small fan or heatsink suitable for the Pi. Keep it outside every Reachy motor/cable travel envelope.
- A removable external bracket or stand. No project-endorsed attached mount exists yet.

### Do not improvise

- Do not feed the robot from the Pi's 5 V rail, USB port, GPIO header, or an unverified splitter.
- Do not share a DC supply between Reachy and the Pi unless an electrically qualified design has established voltage, current, grounding, isolation, fusing and fault behavior. No such design is documented here.
- Do not expose conductors, let a metal mount contact either board, or use a hub/cable that can back-power the Pi.
- Do not drill, glue or clamp anything to Reachy where it can restrict the head, rotating base, antennas, ventilation or service access.

## Safe physical arrangement

1. Put Reachy in **Standby**, confirm the head is folded and motor torque is released, then stop the app.
2. Shut down the Pi cleanly and remove power from both devices.
3. Place the Pi beside Reachy on a stable, non-conductive, ventilated base. This off-robot arrangement is the safest default until a mount has been physically reviewed.
4. Route the USB data cable with a relaxed service loop. It must not cross the rotating base, head linkages, antenna paths, vents or sharp edges.
5. Add strain relief to the stationary support—not to a moving Reachy part. A light pull on either end must not move the robot or Pi.
6. Keep both power cables separate and identifiable. Verify there are no exposed conductors.
7. Manually inspect the full likely movement envelope while everything is unpowered. Never attach, remove, reroute or tighten hardware while Reachy is moving or has motor torque enabled.

**Mounted-on-robot option: TBD.** The project does not yet have confirmed bracket dimensions, fasteners, center-of-gravity assessment, thermal measurements or motor-clearance photographs. Keep the Pi off the robot until Tim provides and approves those facts.

## Power sequence

The conservative reference topology uses two supplies:

```text
Reachy supplied PSU ──> Reachy Mini Lite power input
Pi-rated PSU         ──> Raspberry Pi 4 power input
Reachy USB data port <──> Raspberry Pi 4 USB data port
```

1. With movement space clear, power Reachy using its supplied PSU.
2. Power the Pi from its own rated PSU.
3. After the Pi boots, check for under-voltage or thermal warnings before starting motors or the app.
4. For shutdown, put Reachy in Standby and confirm safe fold/torque release. Stop the app and daemon, shut down the Pi cleanly, wait for storage activity to stop, then remove Pi power. Follow Pollen's official power guidance for Reachy.

A shared supply, battery integration, GPIO-fed fan, UPS HAT and power-button wiring are all **TBD / untested**.

## Network and OS preparation

The reference deployment is Linux-based. The exact public reference OS image is **TBD**. Raspberry Pi OS 64-bit is a reasonable candidate, not a confirmed requirement for this project.

1. Install a currently supported 64-bit Linux image for Raspberry Pi 4.
2. Create a non-default user, apply OS updates, and enable SSH only if needed.
3. Join a trusted LAN. Prefer DHCP reservation or mDNS rather than hard-coding an address in this repository.
4. Do not expose ports `8000`, `8042`, `8642` or `8643` directly to the internet. Use a trusted LAN/VPN; use TLS and authentication across less-trusted boundaries.
5. Install `git`, Git LFS, `uv`, GStreamer and the USB permission rules exactly as described in the official SDK installation guide. Log out/reboot after group or udev changes.

Never publish hostnames, addresses, Wi-Fi credentials, API tokens, serial numbers or screenshots of them.

## Connect Lite and start the Reachy daemon

Pollen's official Lite topology is wall power plus USB data to a computer. Here, the Pi is that computer.

1. With both devices safely positioned, connect Reachy's USB data port to the Pi.
2. Confirm the expected USB, camera and audio devices appear. Do not change udev permissions to world-writable beyond Pollen's current documented rules.
3. In the SDK virtual environment, start the official Lite daemon:

   ```bash
   reachy-mini-daemon
   ```

4. Keep that process running. In a second terminal, verify the daemon API:

   ```bash
   curl -fsS http://127.0.0.1:8000/api/daemon/status
   ```

   You can also open `http://127.0.0.1:8000/docs` locally, as described by Pollen.

5. Do **not** use the official quickstart antenna wiggle as the first physical test for this adaptation. First verify read-only status, clear the movement area, and use the app's guarded wake/fold flow.

A production `systemd` unit for the Lite daemon on a generic Pi OS install is **TBD**; service names and paths vary by installation. Do not copy the Wireless image's service assumptions blindly.

## Install and start this app

The app requires Python 3.11+ and Reachy Mini SDK 1.9+. From a checkout on the Pi:

```bash
uv sync --group dev
uv run pytest
uv build --wheel
uv pip install --reinstall --no-deps dist/reachy_mini_homebody-*.whl
```

Use `--no-deps` only after confirming the target environment already satisfies `pyproject.toml`. For development, `uv pip install -e .` is also supported.

If the Reachy app manager is installed and owns the environment, follow [OPERATIONS.md](../OPERATIONS.md). On a plain Lite/Pi host, exact app-manager integration and boot service wiring are **TBD**. The CLI entry point is:

```bash
homebody
```

Keep the settings UI on the trusted network. Its default app port is `8042`.

### Optional Hermes connection

Hermes is not needed to understand the local control/privacy surface, but the current voice, Realtime, Kids speech and advanced agent paths use the private companion bridge on a separate Hermes host. Follow the repository [README](../README.md#add-hermes-for-voice-memory-and-tools) and [companion guide](../companion/README.md). Provider credentials stay on that host; Reachy stores only the bridge URL and bearer token.

## Acceptance sequence

Do this with one adult at the controls and clear space around Reachy.

1. **Read-only health:** daemon status responds; app status responds; no new traceback or daemon restart appears.
2. **Privacy:** enter Meeting, confirm microphone capture and wake detection stop; enter Sleep and confirm they remain stopped.
3. **Safe wake:** from folded Standby, request the app's guarded wake. Stop immediately if cables pull, the Pi shifts, a mount flexes, or anything enters the motor envelope.
4. **Safe fold:** return to Standby. Confirm Reachy folds before torque is released. If folding fails, torque must remain enabled rather than letting the head drop.
5. **Camera:** keep both camera paths off by default. Enable local live camera only from the trusted UI, then confirm it disconnects when leaving the Robot tab or entering Meeting/Sleep. Enable one-frame cloud sharing only for an explicit visual test.
6. **Audio:** after local-only checks pass, perform the spoken wake/barge-in sequence in [OPERATIONS.md](../OPERATIONS.md#human-audio-acceptance).
7. **Thermal/power:** inspect Pi temperature, under-voltage/throttling history, USB stability and cable temperature under sustained operation.
8. **Final state:** stop the app and leave Reachy safely folded with torque released.

Record the Pi model, OS release, SDK/app versions, power supplies, cable/hub models and pass/fail result locally. Sanitize that record before sharing it.

## Start on boot (only after acceptance)

Do not auto-start motors or the app until manual acceptance passes. If you add services:

- order the app after networking and the Reachy daemon;
- run as an unprivileged dedicated account with only required `dialout`/`input` access;
- use restart limits rather than an infinite crash loop;
- keep credentials in mode-`0600` configuration or the Hermes-host environment;
- make the app enter folded Standby on startup;
- test power loss and an unclean reboot before relying on unattended startup.

Exact unit files for the self-built Lite/Pi topology remain **TBD** pending confirmation of the reference image, paths and service account.

## Rollback and recovery

1. Put Reachy in Standby and confirm fold/torque release.
2. Stop this app; leave the official daemon available for diagnosis.
3. Reinstall the previous known-good wheel as shown in [OPERATIONS.md](../OPERATIONS.md#rollback), or uninstall this app from its virtual environment.
4. If the Pi no longer boots reliably, disconnect it from Reachy and return to Pollen's supported Lite setup: supplied robot PSU + USB data to a normal computer running Reachy Mini Control or the official SDK.
5. If a motor, cable, power or electrical-shock error appears, remove power and follow Pollen's troubleshooting guidance. Do not repeatedly restart into a fault.

## Troubleshooting

| Symptom | Check | Safe response |
|---|---|---|
| Daemon cannot find Reachy | USB data cable, Pi USB enumeration, official udev rules, `dialout` membership | Stop; do not loosen live connectors. Power down before reseating. |
| Camera or audio missing | The SDK must run on the computer physically connected to Lite; inspect USB devices and GStreamer/audio setup | Keep camera sharing off; fix official SDK media setup first. |
| Pi under-voltage / USB resets | Pi PSU rating, cable quality, attached USB current, hub behavior | Stop app; use a correctly rated separate supply or a verified powered hub. |
| Head does not fold | Obstruction, cable strain, daemon/motor fault | Keep torque enabled; clear the obstruction only after stopping movement and making the system safe. |
| Pi or enclosure is hot | Vent blockage, fan/heatsink placement, sustained load | Stop workload; improve off-robot ventilation before retrying. |
| Remote UI unavailable | local app status, firewall, trusted LAN/VPN routing | Diagnose locally; do not open the management port to the public internet. |
| Repeated service restarts | app/daemon logs and restart limits | Disable auto-restart and return to manual startup. |

## Facts Tim still needs to confirm

Before this guide can claim a reproducible reference build, capture and approve:

- exact Raspberry Pi 4 RAM/model revision and OS image/release;
- Pi PSU make/rating, robot PSU revision and whether any powered hub is used;
- USB cable type/length and the physical USB ports used;
- actual physical placement/mount and clearances, with sanitized photographs;
- cooling parts and measured idle/load temperature plus throttling result;
- daemon installation/startup method and any service unit names;
- app launch/service account and boot behavior;
- final DualShock 4 / DualSense pairing and mapping acceptance on this external-Pi Lite adaptation (BlueZ and `input` group access are verified).
