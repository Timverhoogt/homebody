# Daemon-managed updater: target acceptance

Verified on 2026-10-11, on the existing Jetson with Python 3.12.3 and Reachy Mini SDK 1.9.0.
This records measured execution, not a claim that a new public release exists.

## Accepted installation

- Existing daemon remains the app lifecycle owner. Existing daemon/HA units and HA
  bootstrap were not edited or restarted; their hashes and the daemon PID were preserved.
- Homebody package and its one distribution-metadata anchor resolve through a
  single atomic `current` link. Fresh-process package/version/entrypoint checks
  agree with the selected slot.
- Independent system updater is enabled, resource-bounded and hardened with
  `ProtectSystem=strict` and `ProtectHome=true`. It is not a second app supervisor.
- App slots and stable worker live under `/opt/reachy-mini-lite/homebody-managed`.
  The shared journal/locks/heartbeat live in the existing app-writable
  `/opt/reachy-mini-lite/state/home/.local/share/homebody/updates` directory.
  The new worker can write precisely its managed base and this shared state;
  the existing daemon sandbox remains unchanged and cannot write app slots.
- Original configuration and logical owner records were preserved. Temporary
  operator/browser owner devices were revoked and their session files removed.
- All **6,517 shared dependency files** in the provisioned SDK environment were
  hash-compared before/after. Bytecode caches and the intentionally replaced
  Homebody package/metadata were excluded. Target `pip check` passed.
- Bootstrap app artifact is source-built **0.4.0**, from
  `6cd9a8cc4ae8a9cfec969c8e59e1082e08eaec92`, SHA-256
  `50aa650a7d4fffee896de4ac29bda0c354f57e2dd5064ce6270c9de272973783`.
  Its **95 Homebody resources** were verified against source and installed slot.
  It deliberately has no fabricated public release ID or release commit identity.

## Real lifecycle execution

Private, explicitly labelled local wheel fixtures exercised the production worker
and daemon routes. Only the fixture download source and deliberate fault points
were injected by a temporary runtime-only worker ExecStart override. The package,
SDK process lifecycle, pip slot installation, pointer switch, durable journal,
authenticated HTTP health and physical pose/torque checks were real.

The following all passed, and were repeated after selecting the final shared-state
location and worker write-path policy:

- **Clean exit / successful installation:** a genuine daemon-owned app SIGINT
  shutdown produced retained SDK `done`; the queued job installed the target,
  restarted through the daemon, passed exact target health and ended `succeeded`.
- **Deliberately unhealthy target:** the candidate returned failed startup health.
  The worker stopped it, restored the exact prior pointer, restarted/verified the
  prior identity, and ended `rolled_back` rather than reporting success.
- **Worker death after Stop:** one intentional SIGKILL triggered the real systemd
  restart policy; journal recovery restored/reverified the prior slot and ended
  `rolled_back`, without restarting the daemon or HA unit.
- **Worker death after Start:** one intentional SIGKILL after candidate start
  likewise restarted the worker and restored/reverified the prior slot.
- **Dead worker admission:** with the worker stopped, a real private HTTPS
  Owner+CSRF install request returned HTTP 409/unavailable. The existing app
  stayed safely ready; the request did not admit another job or shut it down.
- **Final restore:** the exact approved initial artifact was restored, all four
  fixture slots/downloads and the runtime override were removed, and the
  production journal/history were reset only after proving they contained solely
  these finished local fixture jobs. Private fixture receipts remain in the
  restricted pre-setup backup; no fixture became a public release.

Repeated authenticated health checks required matching version/release ID/commit
and safe runtime readiness. Physical checks required finite measured fold, disabled
torque, and the installed SDK's actual safe sleep-pose distance (threshold 10).
No wake, camera capture, speech-generation or firmware operation was requested.
Final runtime was `waiting_for_wake_word`, folded Standby, motors disabled, no
active/locked Kids session, no runtime error, and Home Assistant connected with
its existing meaningful device identity.

## Dashboard and packaging

- Actual Chromium on the private HTTPS dashboard at 390×844 exercised the real
  Owner transport and CSRF-protected **Check for updates**.
- `/api/updates` returned `supported=true`, empty error, no job, empty history,
  installed 0.4.0 and no offered update. The live phone screenshot was inspected:
  controls and status were readable, with no journal filesystem error or clipping.
- A worker-only writable journal originally produced EROFS in the existing app
  sandbox. The fix was shared state in the already-writable app data directory,
  not widening the daemon's permissions or changing startup ownership. A source
  regression protects the worker example and this setup guidance.
- The official SDK app-assistant checked the exported Space metadata and completed
  its real temporary installation/entrypoint/uninstallation checks. The clean
  GitHub README was preserved; `tools/prepare_space_readme.py` generated metadata
  only in the validation export. `PIP_NO_DEPS=1` bounded this packaging check;
  separate target dependency and execution checks provided compatibility evidence.
- Independent safety follow-up approved retained DONE/STOPPING handling and final
  live-worker admission. Separate Chromium suites passed **16 tests**, and the
  synthetic camera-UI suite passed **24 tests**; these are not physical camera tests.

## Explicit limits

- The latest canonical GitHub stable tag remains `v0.2.0`; this source-built 0.4.0
  therefore correctly offers no newer update. Commits, branch pushes and PR merges
  are not software releases. A newer compatible stable release needs its real
  release ID, exact source commit, release-bound wheel and checksum manifest before
  the normal dashboard Install flow can be offered.
- Local fixture execution is not a claimed download/install of such a public
  release. No production exception permits local fixtures or arbitrary URLs.
- Worker recovery/restart was exercised. Whole-robot reboot, full power loss,
  storage exhaustion and a permanent service failure were not induced.
  Enabled boot ownership is configured, but reboot/power-cycle acceptance is separate.
- If the worker cannot return within its restart policy, the app can remain stopped.
  Inspect and repair the worker/journal; do not erase a pending job or falsely claim
  automatic recovery. See [managed update operations](managed-updates.md).
