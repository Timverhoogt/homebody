# Homebody: marketplace quick start and first-run status

> Development candidate 0.5.15. This guide describes the first onboarding slice, not a published or fully accepted marketplace release.

## If you found Homebody in Reachy Mini Control

1. Connect your Lite over USB or connect to your Wireless robot using the official Reachy setup.
2. In Reachy Mini Control, choose **Applications → Discover Apps**, find **Homebody**, install it and choose **Start**.
3. Open Homebody's local UI using the launcher-supported app link. If needed, open `http://REACHY_HOST:8042` on the trusted management network. The Control app computer and the robot host are not necessarily the same machine.
4. Pair an owner browser using the existing [owner pairing procedure](agent-setup.md#pair-your-owner-browser). Initial owner provisioning still requires trusted local administration in this slice; the terminal-free marketplace owner bootstrap remains open work. Do not publish the UI publicly to work around pairing.
5. Choose **Continue setup**. Confirm the detected host and select **Reachy Mini Control marketplace**. This is descriptive progress only; it does not grant permissions or change how the app is installed.
6. Save English/Dutch language and use **Configure backend in Settings**. Reuse the current restricted Hermes/OpenClaw setup or the manual companion guide. A standalone non-agent provider installer is not included in this slice.
7. Return through **Settings → Review setup / repair connection**. Choose **Check saved connection silently**. It checks bridge health and an authenticated model-catalog request without microphone, speaker, camera, movement or model inference. It does not prove provider model access or acoustic quality.
8. Review privacy/cost boundaries, explicitly acknowledge next-Start listening and choose **Save setup for next Start**. In Reachy Mini Control, **Stop** then **Start** Homebody when you are ready to supervise wake-word listening and a first speech test. No browser response starts media automatically.

A genuinely fresh install (no saved Homebody config) stays in `setup_required`, without app-owned recording/playback, wake-model downloads, actions workers, GPIO or hosted-agent listener startup. Fresh motion and robot-tool defaults are off. An existing saved installation keeps its startup behavior and permissions; it is not forced through first run by an upgrade.

## Resume, repair and privacy

- Step and entry-route progress live with the existing app configuration, not browser storage. Refresh or another paired owner device can resume.
- A successful check is valid for ten minutes and bound to saved backend credentials, language and selected speech settings. Configuration drift, a newer check or a failed check invalidates it.
- All setup APIs use existing Owner/CSRF and Kids management locks. No raw provider errors, keys or connection-proof digest are returned by setup.
- Browser owner loss and Kids lock close/reset the wizard and discard late private replies.
- Camera, motion, Kids, home controls, Workspace and local AI packs remain separate opt-ins. “Connection checked” never means speech, child safety or complete offline operation has been accepted.
- Marketplace and Homebody managed updating remain separate. This wizard does not migrate installations or create a second daemon/updater.

## Platform scope

The wizard reports the actual Homebody host (Pi, Jetson, Linux, Windows or Mac), architecture, Python and installed SDK information and gives platform-specific starting advice. This is detection, not whole-platform certification. Windows's unconditional Unix-locking import blocker has a regression fix; native Windows/macOS install, media, service and Control-launcher acceptance still need separate evidence. Custom managed updating remains Linux/Python3.11/3.12 only.

## Not completed by this slice

- Terminal-free secure initial owner provisioning from the actual marketplace launch path.
- Standalone basic provider/companion installer, secure native per-OS credential setup and silent real-account access checks.
- Complete platform preflight/repair/service recipes and fresh native install evidence.
- Local AI pack provisioning/activation and Nemotron streaming integration.
- Physical microphone/speaker/camera/movement acceptance and actual Control Install/Start/Stop/UI handoff.

Official Applications flow: [Lite](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini_lite/usage), [Wireless](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/usage). App listing: [Homebody](https://huggingface.co/spaces/Timbo89/reachy_mini_homebody).
