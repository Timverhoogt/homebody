# Standard Reachy sleep in Homebody

Homebody's Standby, Meeting and Sleep transitions use the **daemon's native
sleep routine**, the same routine invoked by Reachy Control. They no longer
call the Python SDK client's separate `ReachyMini.goto_sleep()` choreography.

## Sequence and ownership

1. Fence Homebody's voice-state poses, offset emissions and every app audio
   producer/cleanup against late callbacks. Offset callbacks carry their
   registration generation, so an old GLib callback cannot revive after a wake.
2. Stop app playback before disabling its SDK-side wobbler. SDK 1.9's audio
   callback must not have its wobbler removed during playback.
   Drain admitted media calls with a bounded wait before stopping pipelines;
   refuse sleep if a quarantined SDK push/clear is still pending. Ordinary Stop
   and clear remain responsive while an SDK push is quarantined.
3. Cancel/wait for app actions and enable torque for a controlled fold.
4. Subscribe to daemon move updates, then submit exactly one
   `POST /api/move/play/goto_sleep`. Wait for that exact move UUID's
   `move_completed`, not merely admission or disappearance from `/running`.
5. The daemon owns wobbling/offset/tracking cleanup, conditional neutral
   repositioning, its standard sleep sound, folded head/antenna targets,
   timing, and torque release. Even an already-folded robot goes through the
   daemon's own near-sleep decision rather than Homebody's former shortcut.
6. Homebody retains its measured fold check and final torque-disable request.
   Standby restores local wake-word capture; Meeting/Sleep keep capture off.

A late voice cue or action's `resume()` cannot release the power fence. Only a
confirmed new wake does that. Stopping playback in SDK 1.9 also stops its shared
capture pipeline, so automatic return-to-Standby restores capture explicitly.
Late Live/Realtime finalizers cannot call SDK `clear_player()` behind this
fence, because that method restarts the playback pipeline. Speaking uses the
SDK's public local-wobbler API with an app-owned serialized offset callback,
while retaining the SDK's daemon-wobbling command for normal awake speech.

## Failure behavior

Do not retry a sleep admission with an unknown result. On a completion timeout
or lost update connection, cancel only the admitted UUID and check that it no
longer runs. Failure is never presented as successful sleep. If admission or
cancellation cannot be resolved, mark motor state unknown and block additional
motor commands/duplicate sleeps in that runtime until inspected and restarted.
There is no fallback to the client SDK's different choreography.

## Verification scope

Unit/loopback tests cover transport receipts, missing/foreign/failing updates,
no retry, cancellation, app playback teardown ordering, stale voice cues,
Standby microphone restoration, direct wake-word admission and unknown-outcome
motor blocking. They do not prove physical movement or acoustic acceptance.
Release/install authority and an owner-supervised sleep/wake check remain
separate from source implementation.
