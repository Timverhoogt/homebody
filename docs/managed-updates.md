# Managed Homebody updates

## Supported boundary

Linux, Python **3.11 or 3.12**, and Reachy Mini SDK 1.9.x are supported in two explicit topologies:

- `reachy-daemon-slots-v1`: preserves the existing daemon-owned app process, dependency
  venv, startup owner and Home Assistant bootstrap. Only Homebody's package and
  distribution-metadata import anchors resolve through one atomic app-slot pointer.
- `systemd-user-slots-v1`: the original dedicated user `homebody-app.service` topology,
  for installations which already chose that layout. Do not migrate a working
  daemon-managed robot into it merely to use updates.

Settings, owner sessions, models, daemon, calibration and the companion bridge remain
outside slots and are never overwritten or upgraded. This is app-only replacement,
not a firmware, dependency or operating-system updater. Python compatibility is
explicit in each release manifest; a 3.11-only release is refused on a 3.12 target.

Updates require owner pairing, CSRF and explicit INSTALL, verified folded Standby,
no voice/Kids/camera/action/announcement activity, and a live independent worker.
An unconfigured agent bridge is allowed in idle `waiting_for_configuration`;
runtime faults are not. A job is journaled **before** the runtime stop signal is
set. The app may exit immediately after admission; queued-without-prior recovery
restarts the unchanged slot. All failure/interruption paths restart and verify the
previous release, or record `rollback_failed` (requiring operator intervention).
Success means three authenticated checks of exact version/release-ID/commit plus
local safe runtime health; it does not depend on agent backend reachability.

## Daemon-managed setup (recommended for the existing Jetson)

Keep the existing daemon and HA bootstrap enabled and byte-for-byte unchanged.
Do **not** install `homebody-app.service`, change daemon startup, add a PYTHONPATH,
create a `.pth` hook, reinstall the SDK, or replace the Home Assistant identity.
The independently installed worker uses the daemon's supported app routes only.

One-time administrator provisioning must:

1. Verify live folded Standby, no child/voice/camera/action ownership, and a finite
   pose with disabled torque. Also require the **installed SDK's exact sleep-pose
   distance** before stop: its stop route otherwise enables motors and returns to
   zero. A broad folded-status heuristic alone is insufficient. Do not construct
   an SDK client just for this check; that can release/reacquire daemon media.
2. Back up the current package, dist-info, persistent config/owner store, existing
   daemon and HA units/bootstrap, and record hashes. Keep the private backup.
3. Prepare an immutable initial slot containing the verified Homebody package and
   its one real dist-info directory. Create `.homebody-dist-info` inside that slot
   as a relative symlink to its actual dist-info directory, then an atomic `current`
   symlink pointing to the slot.
4. Stop only Homebody through the existing daemon. Replace only the existing
   `site-packages/homebody` and original Homebody dist-info paths with links to
   `current/homebody` and `current/.homebody-dist-info`. The original dist-info
   anchor filename stays fixed; its METADATA version/entrypoints come from the
   currently selected slot. SDK/dependency files are not moved or written.
5. Install a separate stable worker environment from the verified wheel with
   `--no-deps`, explicitly provisioning only its `httpx` and `packaging` dependencies.
   Do not point that worker at `current` or the app's shared environment.
6. Write private `updater.json` next to the **existing runtime's** `config.json`
   (use its actual HOME/config overrides, not the SSH user's assumed home). Set:
   `topology=reachy-daemon-slots-v1`; absolute `state`, `slots`, `current`, `python`,
   `site_packages`, `metadata_link`; `status_url=http://127.0.0.1:8042/api/updates/health`;
   and an independent random 64-character hexadecimal `health_token`. Mode 0600;
   token stays private and is never a general app-control credential.
7. Put `state` (the shared journal/locks/heartbeat) inside the **existing app sandbox**
   writable data path: for this Jetson, `/opt/reachy-mini-lite/state/home/.local/share/homebody/updates`.
   Keep slots/current/worker under `/opt/reachy-mini-lite/homebody-managed`; the
   daemon app must not need write access to these. Allow the independent worker
   to write its managed base and this one shared state directory, **without changing the daemon**
   service, sandbox or startup. A worker-only writable journal can pass worker
   lifecycle tests yet break the real dashboard's status/admission with EROFS.
   Install only the independent worker service, run it as the existing robot user,
   with its private configuration and writable managed slots/state. Start Homebody
   through the same daemon route/HA bootstrap, verify preserved owner/config/HA,
   exact installed bytes, three authenticated safe-health checks and physical
   torque-off fold. Check `/api/updates` as Owner for `supported=true`, `error=""`,
   readable saved progress and a working CSRF-protected Check from the real browser.
8. Before declaring acceptance, exercise a genuine inactive-slot pip install,
   daemon-owned restart and deliberately failing candidate rollback with private,
   explicitly labeled local test fixtures. Restore the exact approved initial
   artifact afterward; fixtures must not become public releases or leave fake
   release IDs in the production app/history. Verify unchanged settings, dependency
   hashes, HA identity, daemon PID/boot ownership and final safe state.

The worker does not require sudo, does not stop the daemon or HA service, and cannot
select an arbitrary app, command, repository or URL from an HTTP request. For boot
recovery, retain the existing HA bootstrap and enable the worker independently.
Availability requires both a fresh (<15 seconds) heartbeat and a live worker PID;
recheck these inside the final serialized reservation after network/preflight work.
If it is unavailable then, reject without enqueueing or stopping the app.
After an admitted job, worker death is recovered by systemd (`Restart=on-failure`,
5-second delay, at most three starts within five minutes) and the durable journal.
Queued work can proceed; interrupted replacement restores the prior artifact;
interrupted work without a prior pointer restarts/verifies the unchanged app.
SDK `done` is stopped/restartable even though its app object remains; `stopping`
is awaited for at most 35 seconds without sending another Stop. Unknown states,
a different app, unexpected HTTP errors and unverified physical safety fail closed.
If the service cannot return (rate limit/configuration/storage failure), the app
can remain stopped; do not claim autonomous recovery. Inspect the journal and
service, fix the underlying cause, then restart only the updater service. Never
clear a pending journal or manually start a competing supervisor to hide the fault.
A source install, broken import anchors, duplicate app distributions, missing
worker, unknown physical pose, incompatible dependencies/interpreter, and another
active app are rejected. Do not run pip/app-store installs or uninstalls of Homebody
while this managed layout is enabled; those may replace the import anchors. Use
managed updates or deliberately restore the private pre-setup package backup.

## Deployment acceptance

See [the measured Jetson acceptance record](managed-updates-target-acceptance.md)
for daemon-owned install, deliberately failed-candidate rollback, real worker
interruption/restart, preserved settings/HA/dependency checks, and the live private
HTTPS dashboard. Local fixtures are not public releases; a compatible newer
canonical stable release is still required before the normal Install offer appears.

## Dedicated user-service setup (alternative, not a daemon migration)

First back up your settings and record the current wheel/version. Stop the existing
app through its original supervisor and disable its automatic startup so only the
new app service owns the runtime. Do not run both. Keep the Reachy daemon running.
The following commands are examples for the **robot user's** shell; substitute a
verified local wheel. Do not perform them during an active robot session.

```sh
BASE="$HOME/.local/share/homebody-managed"
WHEEL=/absolute/path/to/reachy_mini_homebody-0.4.0-py3-none-any.whl
mkdir -p "$BASE/slots" "$BASE/state" "$HOME/.config/homebody" "$HOME/.config/systemd/user"
python3.11 -m venv "$BASE/deps"
# Provision the current app dependencies once, with the supported SDK range.
"$BASE/deps/bin/python" -m pip install "$WHEEL" 'reachy-mini>=1.9,<1.10'
"$BASE/deps/bin/python" -m pip check
mkdir "$BASE/slots/initial"
"$BASE/deps/bin/python" -m pip install --no-deps --no-index --target "$BASE/slots/initial" "$WHEEL"
ln -s "$BASE/slots/initial" "$BASE/current"
python3.11 -m venv "$BASE/updater"
# Stable independent copy: do NOT point the updater's PYTHONPATH at current.
"$BASE/updater/bin/python" -m pip install --no-deps "$WHEEL"
"$BASE/updater/bin/python" -m pip install 'httpx>=0.28,<1' 'packaging>=24'
```

The dependency venv keeps an initial Homebody distribution, but the app always
imports the current slot via its exact PYTHONPATH. Runtime probes and pip installs
strip PYTHONPATH/PYTHONHOME/VIRTUAL_ENV and disable user site packages; requirement
markers are evaluated against the dependency interpreter. Missing or incompatible
dependencies fail before switching; provision them manually during maintenance.
The updater venv is independent of both app slots and runtime dependencies.

Create the private updater configuration (paths must be absolute and contain no
whitespace/systemd delimiters):

```sh
python3.11 -c 'import json,os,pathlib,secrets; h=pathlib.Path.home(); b=h/".local/share/homebody-managed"; p=h/".config/homebody/updater.json"; p.write_text(json.dumps({"topology":"systemd-user-slots-v1","state":str(b/"state"),"slots":str(b/"slots"),"current":str(b/"current"),"python":str(b/"deps/bin/python"),"status_url":"http://127.0.0.1:8042/api/updates/health","health_token":secrets.token_hex(32)},indent=2)); os.chmod(p,0o600)'
chmod 700 "$BASE/state" "$HOME/.config/homebody"
cp docs/systemd/homebody-{app,updater}.service "$HOME/.config/systemd/user/"
```

Set `HOMEBODY_CONFIG` in the app unit to the **existing** settings path if different
from the example. No EnvironmentFile, wrapper command, additional ExecStart, or
Restart policy is supported for the app unit. WorkingDirectory stays outside
slots; PYTHONPATH is exactly the current link and PYTHONNOUSERSITE is 1. The
updater service must not inherit the app's PYTHONPATH. If persistent boot ownership
is wanted, arrange user lingering with the administrator.

Only after reviewing the migration on the target:

```sh
systemctl --user daemon-reload
systemctl --user enable --now homebody-updater.service homebody-app.service
systemctl --user status homebody-updater.service homebody-app.service
journalctl --user -u homebody-updater.service -u homebody-app.service
```

Verify safe Standby, preserved settings and owner pairing. The phone shows a notice,
full accumulated release notes, Later (persistent for that release), explicit
installation confirmation, progress and persisted completion changelog until
acknowledged. New releases can prompt after a deferred older release. No scheduled
installation occurs. Slots and downloaded wheels are retained for recovery; prune
only inactive slots manually with services stopped, never current or journal prior.

For `rollback_failed`, stop the updater, inspect the private journal and service
logs, restore the recorded prior symlink within slots, start the app and verify its
exact health manually. Do not mark a job succeeded to bypass a hardware/runtime fault.

## Release artifacts (canonical Timverhoogt/homebody)

No release is published by the new workflow. Before releasing, bump both project
and package versions, write a nonempty `## [X.Y.Z]` changelog section, commit, and
create an immutable stable `vX.Y.Z` tag. Create a **draft** GitHub release for that
tag and obtain its numeric ID. The package identity needs this ID before build.
Run `.github/workflows/update-artifacts.yml` manually on the canonical repository
with the tag and draft release ID. It validates the draft/tag, sets
SOURCE_DATE_EPOCH from the tagged commit, prepares package release.json, builds a
real wheel and verifies the generated checksum manifest. Download the workflow
artifact. Review and separately attach exactly the wheel and `homebody-update.json`
to that draft before explicitly publishing it as a stable release. Never substitute
a different wheel after generating the manifest. Drafts/prereleases are ignored by
the app. Do not reuse a tag or edit an already published manifest.

Local equivalent on a clean tagged checkout (no network publication):

```sh
export SOURCE_DATE_EPOCH=$(git show -s --format=%ct HEAD)
COMMIT=$(git rev-parse HEAD)
python3.11 tools/release_manifest.py --release-id "$DRAFT_RELEASE_ID" --commit "$COMMIT" --prepare
python3.11 -m build --wheel
python3.11 tools/release_manifest.py --release-id "$DRAFT_RELEASE_ID" --commit "$COMMIT" --wheel dist/*.whl
```

The current 0.4.0 development checkout remains unversioned in the changelog; it is
not a publishable new release until the maintainer writes a versioned section and
bumps the version. Tests build an isolated release fixture, never invent a real
GitHub release ID. Artifact checksums and canonical HTTPS GitHub identity checks
are not an independent signing system: GitHub/repository compromise is outside
this trust model.
