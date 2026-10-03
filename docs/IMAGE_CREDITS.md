# Image credits and reuse notes

The public images below come from Pollen Robotics' official [`pollen-robotics/reachy_mini`](https://github.com/pollen-robotics/reachy_mini) repository at immutable commit [`1e8a628cce504e49c86a982dd1a59f311b03cefc`](https://github.com/pollen-robotics/reachy_mini/tree/1e8a628cce504e49c86a982dd1a59f311b03cefc).

Copyright © 2025 Pollen Robotics. Licensed under the [Apache License 2.0](https://github.com/pollen-robotics/reachy_mini/blob/1e8a628cce504e49c86a982dd1a59f311b03cefc/LICENSE). The upstream repository has no `NOTICE` file at that revision. These assets are used to describe Reachy Mini and their origin; no Pollen Robotics endorsement is claimed. Apache-2.0 does not grant trademark rights.

## Derived web assets

All four files were converted to static WebP, resized only where listed, compressed for the web and saved without EXIF, GPS, device or editing-history metadata. The derived files are modified from their upstream source files.

| Public file | Immutable upstream source | Upstream SHA-256 | Derived size | Use and modification |
|---|---|---|---:|---|
| `docs/assets/hero-reachy.webp` | [`docs/assets/reachy_mini_hello.gif`](https://github.com/pollen-robotics/reachy_mini/blob/1e8a628cce504e49c86a982dd1a59f311b03cefc/docs/assets/reachy_mini_hello.gif) | `eeaa08cd92c11329cec8e72937eaecf4ad41ca8adc56572272cdfb8d2d65020a` | 800×450 | First frame exported as static WebP; no crop. Actual official Reachy Mini desk scene. |
| `docs/assets/reachy-components.webp` | [`docs/assets/reachy_mini_components.png`](https://github.com/pollen-robotics/reachy_mini/blob/1e8a628cce504e49c86a982dd1a59f311b03cefc/docs/assets/reachy_mini_components.png) | `e4f18b75047ba917392c5ea06d980bff923a800240438d3f4f0c3234ef0d55b0` | 1400×990 | Proportionally resized; official Lite/Wireless component comparison. |
| `docs/assets/lite-assembly.webp` | [`docs/assets/digital_assembly_guide_preview_mini_lite.jpg`](https://github.com/pollen-robotics/reachy_mini/blob/1e8a628cce504e49c86a982dd1a59f311b03cefc/docs/assets/digital_assembly_guide_preview_mini_lite.jpg) | `ecc493c4190e3828d5637a6b34424da69862ab380cb96bd33a563a6dc8b0cff4` | 1183×720 | Static WebP conversion; official Lite assembly preview. |
| `docs/assets/mic-camera.webp` | [`docs/assets/mic_and_camera.png`](https://github.com/pollen-robotics/reachy_mini/blob/1e8a628cce504e49c86a982dd1a59f311b03cefc/docs/assets/mic_and_camera.png) | `326dc451e1284b66054489ca08718be2b84c7387d0b33ab25aea34240699bc4a` | 1400×990 | Proportionally resized; official microphone/camera callout. |

The project-authored `docs/assets/architecture.svg` and `docs/assets/lite-pi-overview.svg` contain no third-party raster imagery. The Lite + Pi diagram is an explanatory conservative topology, not evidence of an official conversion, final attached mount or shared-power design.

`docs/assets/ui-dashboard.webp` and `docs/assets/ui-robot.webp` are project-authored interface captures rendered from this repository's real static UI at 1440×900 by [`docs/tools/capture_ui_screenshots.py`](tools/capture_ui_screenshots.py). Every request was answered locally during capture: status and transcript fields come from explicit synthetic, non-private demo text, the robot controls list comes from the app's own allowlists, and all other requests, including camera, were refused. They were last retaken for the dashboard redesign. They contain no browser chrome, hostnames, addresses, tokens, controller identifiers, PINs, faces or camera frames. The captions state that they are not evidence of a live robot connection.

## Privacy and verification

The selected official frames contain no readable credentials, network details, notifications, addresses, serial numbers or private screens. The hero source includes an office background and partial people; no clearly identifiable face is presented in the static first-frame export. Final publication review must still inspect the rendered desktop/mobile crops and captions.
