# Motor reconnection on a fresh wake

Homebody can use an **optional local daemon recovery capability** when motor
power or the control connection has been lost. It never restarts Hermes, the
Jetson, or the entire Reachy daemon to recover a backend.

## Intended behaviour

1. Restore the motor power/connection.
2. Give a new wake request. Simply returning power must not wake Reachy.
3. If the daemon confirms its old motor backend has failed and disappeared,
   Homebody may request one torque-off reconstruction.
4. Only after measured readiness, disabled torque and a fresh control connection
   are verified may that original, still-authorized wake continue normally.

Stop, privacy, a newer transition or session cancellation invalidates the pending
wake. Late recovery may finish with motors off; it must not replay old movement,
speech or conversation work. A failed or ambiguous motor write is never retried.
If power is still absent, recovery fails without waking. After verified clean
cleanup, a **later explicit request** can try again; there is no background retry.
Unknown cleanup or unexpected enabled/mixed torque requires inspection instead.

## Compatibility is explicit

Stock SDK 1.9.0 does not provide this recovery interface. Homebody does not patch
or upgrade the SDK, install native libraries, flash motors, or bypass an updater
from a wake request. Without a verified compatible capability, recovery reports
unavailable and makes no wake attempt. Ordinary healthy-backend waking is
unchanged.

A local compatibility implementation must expose schema 1 at
`GET /api/daemon/motor-recovery`, with exact `supported: true` and
`torque_off_only: true`. It must register one `daemon-reconnect-motors` job through
`POST /api/daemon/reconnect-motors`, support existing UUID job polling, and restore
real ready `/api/state/full` telemetry with finite head pose and disabled mode.
Homebody retains its own immutable wake/Stop/privacy admission throughout.

The implementation must verify **all nine actuators disabled**, not infer it
from an aggregate false value. Native controller 1.5.6's existing torque flag
means “all on”, and its normal constructor can reboot faulted motors; neither
is sufficient for a strict recovery implementation. A compatible native
constructor must provide a distinct no-reboot recovery path. Preserve existing
calibration, PID configuration, callback, media and AudioDoA ownership. Cancel
and await old websocket clients, spawned moves and command workers before
replacing control resources; clearing outgoing queues alone is insufficient.

Homebody's 15-second client wait is not a hard termination of an SDK/native job.
A timed-out or cancelled attempt never gains authority to wake later. Cleanup
and individual native/OS operations retain their own limits.

## Installation and acceptance are separate

An initial SDK/native compatibility installation requires its own exact source
and artifact checks, original-package rollback, preserved settings, managed
updater coordination and supervised activation. Loading new daemon/native code
can require **one initial daemon restart**; later successful backend recovery is
the no-restart acceptance case. Do not silently install compatibility software
as a side effect of using Wake.

Offline HTTP, lifecycle and native PTY-bus fixtures do not establish physical
acceptance. A supervised test starts folded, safely supported and with motors
off; never unplug an upright torque-supported head as a test. Record daemon/media
identity, restore the connection, make a fresh wake request, verify normal wake
and Stop, then use the normal supervised fold back to Standby. Confirm no stale
work resumed and no daemon/media restart occurred. Camera, microphone and
speaker testing require their own permissions; they are not needed for offline
motor-recovery fixtures.
