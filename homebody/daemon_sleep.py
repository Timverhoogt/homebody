"""Run Reachy Control's daemon-owned sleep, never the SDK client's choreography.

Subscribe before admission so even an immediate completion cannot be missed. A
move disappearing from /running is NOT success: require its exact completion
receipt. Never retry an ambiguous POST, which could start a second movement.
"""

from __future__ import annotations

import json
import time
from uuid import UUID

import httpx
from websockets.sync.client import connect

_HTTP = "http://127.0.0.1:8000/api/move"
_UPDATES = "ws://127.0.0.1:8000/api/move/ws/updates"


class DaemonSleepError(RuntimeError):
    """Sleep failed; uncertain means a daemon move may still own the head."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def run_daemon_sleep() -> None:
    """Wait for the same native sleep task used by Reachy Control (bounded)."""
    move_id: str | None = None
    admitted = False
    terminal = False
    try:
        with connect(_UPDATES, open_timeout=2.0, close_timeout=1.0, proxy=None, max_size=16384) as ws:
            with httpx.Client(timeout=2.0, trust_env=False) as client:
                try:
                    # A transport error after this point has an unknown admission outcome.
                    admitted = True
                    response = client.post(f"{_HTTP}/play/goto_sleep")
                    response.raise_for_status()
                    payload = response.json()
                    raw_id = payload["uuid"]
                    if not isinstance(raw_id, str) or str(UUID(raw_id)) != raw_id:
                        raise ValueError("Daemon returned a noncanonical move identifier")
                    move_id = raw_id
                    deadline = time.monotonic() + 12.0
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Native sleep completion timed out")
                        event = json.loads(ws.recv(timeout=remaining))
                        if not isinstance(event, dict) or event.get("uuid") != move_id:
                            continue
                        kind = event.get("type")
                        if kind == "move_completed":
                            terminal = True
                            return
                        if kind in {"move_failed", "move_cancelled"}:
                            terminal = True
                            raise DaemonSleepError("Daemon did not complete its native sleep movement")
                except Exception as exc:
                    if admitted and move_id is not None and not terminal:
                        # Stop only OUR task. An absent task is not proof of successful sleep,
                        # but it does establish that no outstanding task can race a later wake.
                        try:
                            stopped = client.post(f"{_HTTP}/stop", json={"uuid": move_id})
                            stopped.raise_for_status()
                            running = client.get(f"{_HTTP}/running")
                            running.raise_for_status()
                            rows = running.json()
                            terminal = (
                                isinstance(rows, list)
                                and all(isinstance(row, dict) and "uuid" in row for row in rows)
                                and all(row["uuid"] != move_id for row in rows)
                            )
                        except Exception:
                            terminal = False
                    if isinstance(exc, DaemonSleepError) and terminal:
                        raise
                    raise DaemonSleepError(
                        "Native sleep failed or its completion could not be verified",
                        uncertain=admitted and not terminal,
                    ) from exc
    except DaemonSleepError:
        raise
    except Exception as exc:
        raise DaemonSleepError("Could not connect to the daemon's native sleep service") from exc
