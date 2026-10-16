# Durable native Workspace verification

Native completion and Homebody verification are separate outcomes. A native agent can
finish successfully while Homebody reports `verification_failed`: Ready to test also
requires the configured fixed checks and artifact hashes. These checks prove only their
explicit scope, not arbitrary task correctness or physical owner acceptance.

## Provision a durable host environment

Keep every `verification_commands` executable, its interpreter, dependencies and base
Python outside temporary directories and prunable agent caches. An absolute argv path
is not sufficient if cleanup can delete it. Use a dedicated environment on the trusted
bridge host, separate from the robot SDK and the primary agent environment.

For example, the following provisions a Python 3.11 environment for the subscription
unit checks shown below. These tests mock the robot SDK; do not install or change the
robot's media stack to run them. Other project checks may need different dependencies.

```bash
VERIFIER="$HOME/.local/share/homebody/verifier/venv"
python3.11 -m venv "$VERIFIER"
"$VERIFIER/bin/python" -m pip install \
  'pytest==9.1.1' 'fastapi==0.133.1' 'httpx==0.28.1' 'aiohttp==3.14.3'
"$VERIFIER/bin/python" -m pip freeze > "${VERIFIER%/venv}/requirements.lock.txt"
```

Do not overwrite an existing environment used by running work. Provision and test a
replacement first. Verify the base Python is durable too: a virtual environment may
link to its base interpreter rather than copy it. Retain the dependency lock for an
explicit rebuild; do not upgrade shared runtime dependencies incidentally.

Run the exact approved checks from the **same actual checkout** used by the native
worker, with the verifier's scrubbed environment. For example, after setting
`PROJECT_ROOT` to that checkout's absolute path:

```bash
cd "$PROJECT_ROOT"
env -i PATH=/usr/bin:/bin HOME="$PROJECT_ROOT" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  PYTHONNOUSERSITE=1 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
  "$VERIFIER/bin/python" -m pytest -q -o addopts= \
  tests/test_all_subscription_modes.py tests/test_subscription_reasoning.py
```

Do not inherit provider credentials, `PYTHONPATH`, `PYTHONHOME` or agent plugins into
verification. A scrubbed environment is not a filesystem or network sandbox; the fixed
commands still require trusted project code and appropriately restricted host access.
The subscription examples are not task-specific acceptance for unrelated edits.

Set the selected native target's `verification_commands[0][0]` to the **expanded
absolute path** of the tested interpreter. JSON argv is not executed through a shell:
`$HOME` and `~` will not expand there. Preserve the remaining argv, artifact paths,
project/session identity, permissions and credentials. See
[native Workspace](native-workspace.md) for target registration and approvals.

## Diagnose an empty failed receipt

Correlate the exact native run and session before changing anything:

1. Read the native backend's run status. A successful HTTP response or generated
   answer alone does not prove terminal completion.
2. Read Homebody's private native journal and verification receipt. A completed
   native run with `verification_failed`, `checks: []` and `artifacts: []` can mean
   the first subprocess never started. It is not evidence that pytest ran and failed.
3. Inspect the bridge process's **effective** `REACHY_NATIVE_TARGETS_JSON`, not only
   a shell or an obsolete environment file. Confirm the executable and its symlink
   targets exist and are executable under the bridge service's sandbox.
4. Check the configured working tree and imports. Reproduce the exact fixed command
   with credentials scrubbed. Only a recorded check exit code establishes that a
   check ran; missing executables and other startup errors can leave no check entry.
5. Exercise the installed `NativeWorkspace._verify` routine with isolated diagnostic
   objects and the candidate command. Require successful checks and hashes for every
   configured artifact. Do not bind a real device or submit another agent job merely
   to test a verifier, and do not replace a historical failure receipt with this result.

Keep raw settings, journal backups, session identifiers and provider credentials
private. Reports need only the failure category, check results and relevant artifact
hashes. Never publish `.env` files or copy live credentials into an example.

## Activate an authorized repair safely

Changing a verification command changes the native configuration fingerprint. The
journal deliberately refuses a different configuration on startup; do not delete the
journal or bypass this guard to make a restart work.

1. Back up the private configuration and journal with owner-only permissions. Retain
   the original failed receipt and exact rollback configuration.
2. Reconcile **every** journaled binding against its native backend. Unknown admission,
   disconnected or unconfirmed stopped work is still owned work: resolve it before
   replacing configuration. A Stop request alone is not terminal-state evidence.
3. Validate that only the intended verifier executable changes. Keep native target,
   project, session, approval policy and all other settings unchanged.
4. Ask the owner to end active Kids/voice use before interrupting the shared bridge.
   Confirm idle status; do not fold, wake, enable camera or move the robot for a
   host-only repair. For a safe-Standby deployment, read back folded/motors-off state.
5. Stop only the discovered bridge service. Under explicit maintenance authority,
   migrate the journal's configuration identity using the installed implementation's
   fingerprint of the exact new configuration, **only after** the reconciliation
   above. Preserve bindings, native run IDs, historical statuses and receipts. Keep
   rollback files; do not substitute a new session or reset unknown work.
6. Start the bridge and read back its effective command, authenticated health and
   native capabilities. Confirm unrelated agent/native-listener processes were not
   restarted, the robot app/release is unchanged, and Reachy reconnects without
   acquiring camera, speech or motion authority.

A repaired verifier applies to future approved runs. The original failed run remains
an honest historical record. Passing fixed checks does not establish a new coding
workflow's end-to-end voice or physical acceptance.
