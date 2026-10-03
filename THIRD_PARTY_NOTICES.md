# Third-party notices

The Home Assistant ESPHome wire framing and stable entity-key compatibility were
implemented with reference to the Reachy Mini Home Assistant app:

- Project: https://huggingface.co/spaces/djhui5710/reachy_mini_home_assistant
- Reference commit: `c5fd1f522ab44e8e9feb2897d4018027a8afb063`
- License: Apache License 2.0

The Hermes implementation is rewritten around its existing serialized robot
action controller, local opt-in gates, truthful unavailable states, same-peer
media validation, and mutually exclusive Hermes/Home Assistant voice ownership.

The on-device HaGRID gesture preprocessing/class map and the bundled
`hand_detector.onnx` / `crops_classifier.onnx` models are adapted from the same
Reachy Mini Home Assistant reference commit above under Apache License 2.0.
Hermes adds checksum verification, repeated-frame confirmation, edge triggering,
cooldowns, no-auto-wake behavior, and its own privacy/Kids/action-ownership gates.

The camera joystick's 20 Hz target-stream cadence and drag-start geometry were
adapted from the official Pollen Robotics Reachy Mini Control controller module:

- Project: https://github.com/pollen-robotics/reachy-mini-desktop-app
- Reference commit: `0f150976f4a44db0cc4c3e30247f4d71e1fff42c`
- License: Apache License 2.0

The Hermes implementation is rewritten around gesture-bound sessions, a local
watchdog, cooperative Stop, privacy/Kids/Awake policy gates, and coupled
head-plus-base horizontal movement.

The Homebody browser camera viewer includes an unmodified bundled copy
of `gstwebrtc-api.js`, sourced from Pollen Robotics' Reachy Mini Control desktop
application and originally maintained by the GStreamer project:

- Project: https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/tree/main/net/webrtc/gstwebrtc-api
- License: Mozilla Public License 2.0
- Copyright: Copyright (C) 2022 Igalia S.L.
- Author: Loïc Le Page

The bundle contains `webrtc-adapter`:

- Project: https://github.com/webrtcHacks/adapter
- License: BSD 3-Clause
- Copyright: Copyright (c) 2014 The WebRTC project authors; Copyright (c) 2018 The adapter.js project authors

BSD 3-Clause license terms:

> Redistribution and use in source and binary forms, with or without modification,
> are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this
>    list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice,
>    this list of conditions and the following disclaimer in the documentation
>    and/or other materials provided with the distribution.
> 3. Neither the name of the copyright holder nor the names of its contributors
>    may be used to endorse or promote products derived from this software without
>    specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
> ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
> WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
> DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
> ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
> (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
> LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
> ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
> (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
> SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The original license notices remain intact at the top of the distributed JavaScript bundle.
