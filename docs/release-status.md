# Release and acceptance snapshot

**Snapshot: 12 October 2026. Homebody remains early alpha.** This separates source availability, packaged releases, one reference deployment and physical acceptance. It is not a compatibility or production-readiness promise. Check [GitHub releases](https://github.com/Timverhoogt/homebody/releases) and [the changelog](../CHANGELOG.md) for later changes.

## What is available where?

| Component | Snapshot | What that does—and does not—mean |
|---|---|---|
| Published robot/dashboard package | [v0.5.2](https://github.com/Timverhoogt/homebody/releases/tag/v0.5.2) | Includes Everyday/Workspace routing, GPT-Live preview, silent measured speaker volume/mute, managed updater compatibility and clearer update messaging. |
| Source on GitHub `main` and Hugging Face | Includes [Everyday subscription support (#77)](https://github.com/Timverhoogt/homebody/pull/77) and [Kids/I Spy/native Workspace support (#78)](https://github.com/Timverhoogt/homebody/pull/78) | These host changes landed **after** v0.5.2. Mirroring main does not retroactively replace that immutable release wheel. |
| Reference trusted-host bridge | Separately updated with subscription routing and a dedicated native Hermes Workspace listener | Requires saved supported authentication, explicit model configuration, a registered project/session and host service configuration. Not a universal fresh-install default. |
| Android companion / APK | Independent of the robot package | A Reachy update changes robot software and served dashboard assets. It does not install or version-check an APK on the phone. |
| Agent-led setup bundle | Served by the installed robot version | Its checksums identify that bundle, not the latest GitHub source. Existing one-message setup establishes a restricted connection; it does not automatically provision subscription routing or a full native Workspace. |

The Hugging Face Space is a static product page and source mirror, not a cloud-hosted robot demo. Its successful build does not prove a particular robot is installed or physically accepted.

## Tested reference configuration—not defaults

| Text/vision purpose | Tested subscription model | Reasoning |
|---|---|---|
| Everyday questions and narrow tools | `gpt-6-sol` | `enabled=false`, `effort=none` |
| Restricted planning/roadmap discussion | `gpt-6-sol` | `enabled=false`, `effort=none` |
| Native Homebody project Workspace | `gpt-6-sol` | Confirmed provider/model lock; `enabled=false`, `effort=none` |
| Kids chat and activities | `gpt-6-luna` | `enabled=false`, `effort=none` |
| I Spy vision and guess judging | `gpt-6-luna` | `enabled=false`, `effort=none` |

Models and supported effort levels depend on the account and installed Hermes runtime. At this snapshot `gpt-6.1-sol` cannot encode genuine `none`; low/minimal is not off. Validate the emitted wire payload and actual account acceptance. See [subscription configuration](subscription-reasoning.md).

The reference saved speech engine is GPT-Live (`gpt-live-1`). Realtime (`gpt-realtime-2.1`) and Pipeline remain selectable. OpenAI speech is still an API service; Kids safety screening still uses `omni-moderation-latest`. Subscription funding does not make cloud inference local or eliminate speech charges. Other configured Pipeline providers have their own terms and costs. None of these Homebody-specific settings changes the owner's main Telegram/Desktop model.

## Evidence and remaining acceptance

| Area | Exercised evidence | Still required / not claimed |
|---|---|---|
| Lite + Raspberry Pi 4 | Historical reference dashboard, power/motion/camera and connected-flow checks | Per-install acceptance; not an official Wireless conversion or a battery/mount/electrical equivalence claim. |
| Lite + Jetson host | Deployed app, real camera preview, HA telemetry/lifecycle, folded motor-off state, silent speaker controls and managed updates | No blanket claim for local GPU vision, accelerated gestures, GPIO, Bluetooth mapping or every supported host. |
| Everyday subscription | Real general-answer, configured clock/weather and provider readback checks; narrow tool routing tests | Microphone-to-speaker latency and newly configured home-action writes require separate supervised acceptance. The reference household write grant is lights only, not switches/locks/climate. |
| Kids / I Spy subscription | Real moderated Kids reply; structured matching and synthetic-image vision with accepted none; existing validation/lifecycle regression coverage | Synthetic images do not prove physical I Spy camera/search acceptance. Keep parent supervision, fresh camera consent and stop/expiry checks on each setup. |
| Native Workspace | Real admitted/completed Hermes run, effective subscription runtime, stored session/history and reasoning lock; bridge credential/history readback | Host registration is not project-work approval or full phone-to-execution acceptance. Native discussion uses the worker’s configured tools; discuss-only instructions are not enforcement. Registered CWD is context, not a filesystem sandbox. |
| GPT-Live preview | Real silent session start/close with the configured account | Physical echo, acoustic barge-in, speaker playback and end-to-end conversational latency remain unverified for this preview. |
| Private phone camera fallback | Explicit-start authenticated HTTPS frame fallback and decoded-frame/visual checks; generation-bound playback and cancellation regressions | Reduced frame rate; never infer media success from HTTPS or signalling alone. No implicit camera opt-in. |

Do not replace these distinctions with one global green checkmark or a fixed historic test count. Automated coverage, network/provider evidence, actual deployment and supervised physical acceptance are different evidence classes.

## Update the appropriate component

- **Managed robot:** use the documented [Check → Install update flow](managed-updates.md). Preserve the updater/daemon topology; do not pip-install over a live managed slot.
- **Host bridge:** back up its source/configuration, update it separately, bootstrap the supported Hermes runtime and read back effective provider/model flags. Installing a robot wheel alone does not restart or update the host bridge.
- **Phone:** update its APK independently if needed; the PWA/served dashboard is a different layer.
- **After maintenance:** follow [Operations](../OPERATIONS.md), confirm preserved settings and folded motor-off state, then perform only explicitly opted-in supervised camera, speech and movement checks.
