# Managed Homebody updates

## Supported boundary

Only Linux, Python 3.11, Reachy Mini SDK 1.9.x, and the fixed **user** unit
`homebody-app.service` with immutable app slots are supported. A daemon-managed app,
a source checkout, shared editable environment, or different supervisor is not
silently converted. Settings, models, daemon, and companion bridge remain outside
slots and are never overwritten or upgraded. This is app-only replacement, not a
robot firmware or dependency updater. Physical/systemd acceptance on a target
robot is still required before deployment; local tests use no robot.

Updates require owner pairing, CSRF and explicit INSTALL, verified folded Standby,
no voice/Kids/camera/action/announcement activity, and a live independent worker.
An unconfigured agent bridge is allowed in idle `waiting_for_configuration`;
runtime faults are not. A job is journaled **before** the runtime stop signal is
set. The app may exit immediately after admission; queued-without-prior recovery
restarts the unchanged slot. All failure/interruption paths restart and verify the
previous release, or record `rollback_failed` (requiring operator intervention).
Success means three authenticated checks of exact version/release-ID/commit plus
local safe runtime health; it does not depend on agent backend reachability.

## One-time administrator setup (not performed automatically)

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
