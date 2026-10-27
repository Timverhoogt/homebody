# Dedicated robot computer shutdown

## Lost-backend shutdown recovery

The confirmed Owner shutdown route now uses a dedicated serialized shutdown
operation rather than routing through Awake or blindly enabling torque for Sleep.
It revokes voice/camera/action work and retains one immutable Stop receipt from
admission through lock waits, reconstruction, fresh measurement and OS scheduling.
Stop or another power transition before scheduling cancels the old request.
Scheduling is serialized with Stop admission; a later Stop does not cancel an
already acknowledged OS poweroff timer.

- A freshly measured SDK-safe folded pose with disabled torque skips all motor
  enabling and native sleep movement.
- Only exact local GET `/api/state/full` 503 `Backend not running` permits one
  reconstruction, using the existing advertised torque-off-only recovery ABI.
  Recovery independently requires ERROR with absent backend. Transport failures,
  unknown outcomes and other HTTP errors never cause retries.
- Reconstructed hardware must already be folded inside the helper's exact SDK
  sleep-distance gate with finite readings and disabled torque. An upright or
  uncertain recovered robot refuses: recovery never leads into a fold or wake.
- A healthy, unreconstructed upright robot retains the normal native Sleep
  behavior, followed by fresh safe-state verification. No new SDK/native package,
  daemon restart, media client construction or control-socket replacement occurs.
- Refusals keep Sleep/privacy applied so the wake loop cannot turn a failed
  recovery into an automatic folding attempt. Existing root helper admission and
  execute-time checks are unchanged; runtime checks never replace root checks.

`tests/test_shutdown_recovery.py` exercises actual runtime guards with synthetic
hardware. `tests/test_owner_shutdown.py` exercises the real middleware and handler
with that runtime. These tests do not establish deployed physical acceptance.

## Historical helper repair: what was wrong, and what was not established

The 0.5.19 handler folded via the runtime, started a Python timer and immediately
returned `ok / shutting_down`. Later it tried `sudo -n systemctl poweroff` and only
logged failures. Dedicated-host detection was not permission discovery. The
installed daemon runs as `timverhoogt` with `NoNewPrivileges=yes`, so this is not a
valid way for its app to acquire root privileges. The phone reported HTTP 502 over
Tailnet HTTPS. That reply alone cannot establish whether a request reached the
app, whether the app had already stopped, or whether the later command failed.
The privilege defect is established by the code/sandbox contract, **not proven
to be the historical cause of that 502**. No origin, pairing, CSRF or Tailnet
security setting needs weakening.

## Admission and truthful UI contract

- GET `/api/shutdown/readiness` is paired-owner, no-store, and side-effect-free:
  dedicated host plus a root-owned socket/peer and loaded fixed systemd units.
  It does not fold, stop the app, start a timer or prove physical shutdown.
- POST still requires exact existing confirmation, Owner, same-origin CSRF and
  existing Kids management lock. A missing helper refuses with 503 **before**
  any folding. Missing/starting runtime refuses. Sleep uses the existing serialized
  runtime power transition; no new motor path was introduced.
- The independent root helper reads only daemon `/api/state/full`, requiring
  finite measured fold and disabled torque, using SDK 1.9's exact <=10 magic-mm
  sleep distance: translation in mm plus clipped relative-rotation trace angle
  in degrees, preserving its rounded sleep matrix. It constructs no SDK client.
  Rootless randomized scipy Euler/SDK trace-formula regression tests independently
  verify both accepted and refused poses. Revalidate SDK constants/version before
  provisioning; a future SDK change requires updating this reviewed helper.
- Only after `systemctl start homebody-poweroff.timer` succeeds and that fixed
  timer is observed active does the app return HTTP **202**, protocol 1,
  `state: scheduled`, fixed `delay_seconds: 5`. The timer belongs to the system
  manager, not the app or socket helper. Repeated starts of an active timer do
  not extend its deadline. This receipt means scheduled, **not powered off**.
- At timer execution the dedicated-host and live physical checks run again;
  unavailable daemon, enabled torque or changed/unsafe pose fails closed. The
  sole final command is `systemctl --no-block poweroff --no-wall`. No app Stop
  request is needed; systemd handles host service teardown after admission.
- Chromium uses the original relative same-origin `/api/shutdown` URL and the
  existing Owner CSRF wrapper on Tailnet HTTPS. It suppresses duplicate clicks
  while pending and fences late replies across Owner loss/re-pairing.
- A verified 202 says shutdown is scheduled and offline shortly is expected.
  Proxy 502/503/504, disconnect, timeout, malformed or old false-success replies
  say **outcome unknown**, not “not started”; they never automatically retry.
  Only explicit app refusals re-enable deliberate retry (503 requires the
  `X-Homebody-Shutdown: refused` response marker). Reload is a deliberate user
  action, not automatic recovery. Status polls are reads only.

## Privilege boundary

Four reviewable unit templates ship in `homebody/provision/`; the stdlib-only
helper ships as `homebody/shutdown_helper.py`. Admin installs a root-owned copy
under `/opt/homebody-shutdown/`, outside writable managed slots. System Python
`-I` ignores app/user Python paths and environment injection. The application
and updater remain non-root and keep their existing `NoNewPrivileges` sandbox.
There are **no sudoers/Polkit grants**, app service changes, dependencies,
network control listener, configurable commands, paths, addresses or delays.

`/run/homebody-shutdown.sock` is root-owned, group 0660; each accepted stream runs
a short, hardened root service. Both socket peer credentials and the configured
exact non-root app UID are checked: group membership alone does not authorize
another UID. CHECK/SCHEDULE are the entire bounded protocol. The client checks
root ownership, socket type, no world-write and root peer before sending.
The authorized local UID remains trusted: other processes already running as
that UID have the same fixed shutdown capability; this is not per-process
executable authentication. The daemon state endpoint is trusted local telemetry.

Both root services use NNP, empty capability bounding/ambient sets, strict
filesystem/home/device/kernel protections, bounded execution and localhost-only
IP access. `PrivateNetwork` is intentionally absent: the helper must read the
existing host-loopback daemon. The socket is not exposed over the network.
Systemd's in-memory timer survives app/helper exit, **not reboot or deliberate
manager/unit cancellation**; after reboot no pending shutdown is restored.
A lost socket/HTTP acknowledgement remains unknown, including scheduling timeout.

## Offline artifacts and later authorized provisioning

This is a private same-version **development candidate**, not a replacement for
published 0.5.19 assets, an updater-eligible release, or deployment evidence.
The bundle contains a candidate wheel, helper/templates, installer and this
runbook with checksums and source identity. Do not publish or deploy without
separate parent/owner approval. No privileged install or actual poweroff was
performed on the Hermes VM; no camera/mic/motor action or Jetson poll was used.
The Jetson was initially powered OFF. The owner has since powered it on and the
parent verified a running, folded, torque-disabled app over trusted LAN SSH.
This source work has not provisioned/deployed anything; target installation is
handed to the parent and supervised physical shutdown acceptance remains unrun.

Once the owner says the robot is online and approves provisioning:

1. Discover the actual app service UID, primary GID, interpreter, managed slot,
   sandbox (`systemctl cat/show reachy-mini-jetson-daemon.service`), existing
   poweroff units/policy, and finite folded/torque state. Preserve settings,
   primary Hermes bridge, fastpaths, prewarm and all seven Kids voices.
2. Verify the bundle's checksum manifest and review files. Back up any existing
   exact destination files before first install. Do not overwrite an active
   shutdown schedule or install a second lifecycle owner. Do not stop/restart
   or move Reachy merely to provision this helper.
3. From the extracted bundle root, explicitly as host admin (not from the app):
   `sudo /usr/bin/python3 -I tools/install_shutdown_helper.py --app-user timverhoogt`
   Substitute only the actually verified daemon account. The installer refuses
   root app UIDs, non-dedicated hosts, symlink/untrusted writable destinations.
   It copies reviewed files and enables **only** the request socket, never the
   poweroff timer. It does not install the wheel or modify app/bridge settings.
4. Read back exact root-owned helper/template bytes and effective units, socket
   ownership/mode, `systemctl is-active homebody-shutdown.socket`, and inactive
   timer. Confirm NNP/capability restrictions and app sandbox visibility of
   `/run/homebody-shutdown.sock` and primary-group access. Use the app's actual
   interpreter and GET Owner HTTPS readiness; expect protocol 1 / ready with
   no-store. If sandbox mount restrictions hide the socket, stop and review a
   narrowly scoped mount permission; do not disable NNP or broaden privileges.
5. Separately install an authorized canonical newer app release through the
   established managed updater, or obtain explicit exceptional maintenance
   authority for a private candidate. A wheel by itself never provisions root
   host services. Read back every installed wheel resource and retained settings.
6. Only with the owner physically present, space clear, and explicit shutdown
   approval: test the real phone over its normal Tailnet HTTPS origin. Confirm
   scheduled acknowledgement, expected offline, and actual safe physical poweroff.
   Inspect retained root-service journal on next boot. Do not substitute an SSH
   poweroff, synthetic timer receipt, or inaccessible port for physical acceptance.

Rollback is an explicit host-admin maintenance action: first confirm no pending
shutdown. Disable/stop `homebody-shutdown.socket`, remove/restore **only** the four
installed unit files and `/opt/homebody-shutdown/shutdown_helper.py`, then
`systemctl daemon-reload`. Do not cancel a real owner's pending shutdown or
restore older environment/settings snapshots incidentally. App rollback is the
independent existing managed-slot procedure. Without the helper, this repaired
app fails closed and explains provisioning rather than claiming success.

Offline render only (no sudo/services): create a private 0755 staging directory,
then `/usr/bin/python3 -I tools/install_shutdown_helper.py --app-user "$USER" --stage /absolute/stage`.
The rendered UID is illustrative until replaced by the verified robot account.
