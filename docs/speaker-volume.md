# Speaker volume

Open **Settings → Speaker volume** on a paired Owner device.

- The slider is a draft. **Apply volume** commits it; the displayed speaker level is hardware readback, not the requested number. ALSA steps can round by 1–2 percentage points.
- **Mute speaker** sets output to 0%. **Unmute speaker** restores the last measured nonzero level, never an invented maximum/default.
- After restart at 0%, select a level explicitly: the prior in-memory restore level is unknown.
- **Read current volume** refreshes the measured level without changing it. Failed or malformed write responses lock further changes until fresh readback; owner loss, Kids and offline state invalidate late replies.
- These controls are silent. They do not play the SDK test tone, start media, wake Reachy, move motors, or change microphone capture/privacy.
- Owner controls may quietly adjust output while Reachy is asleep/private. Agent changes remain refused in Meeting, Sleep and privacy. All reads/changes are unavailable during startup/shutdown or an active/locked supervised child session.

## MCP

Existing bearer/OAuth authentication and rate limits apply. No new credentials or permissions to microphones/cameras/motors are added.

- `get_volume {}` → `{volume: 0..100, muted: boolean, can_unmute: boolean}`.
- `set_volume {"volume": 30}` → the actual measured output level. Only an integer 0–100 is accepted.
- `set_mute {"muted": true}` / `set_mute {"muted": false}` → mute/restore with readback.

An unavailable device or unconfirmed write is an error, not success. MCP clients may need to reconnect/refresh tools after deployment.

## Hardware and Home Assistant

Quiet control currently supports Linux ALSA with exactly one recognized Reachy/ReSpeaker playback card and one index-zero playback-only volume control. It never selects the default or HDMI device. Commands are shell-free, use a restricted environment and have one three-second transaction deadline. No SDK mixer/client is constructed: SDK 1.9 volume setting plays a tone, and its Linux mixer construction can reset capture controls.

Integrated Home Assistant controls use the same guarded service. Existing standalone telemetry-only bridges retain read-only speaker observation; this change does not enable HA controls or replace that bridge. A measured zero remains zero and the media-player mute flag reflects output mute.

Unit/browser fixtures are not hardware acceptance. Deployment acceptance must compare microphone capture before/after quiet volume/mute/restore, verify folded motor-off state, authenticate the real dashboard and MCP routes, and restore the initial speaker level.
