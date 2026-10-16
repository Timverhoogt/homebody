"""Bounded unprivileged client for the separately provisioned shutdown helper."""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
import time

from .shutdown_helper import DELAY, SOCKET


class ShutdownRefused(RuntimeError):
    """No shutdown request was admitted."""


class ShutdownUnknown(RuntimeError):
    """A scheduling request may have reached systemd; never retry automatically."""


def request(operation: str) -> dict:
    if operation not in {"CHECK", "SCHEDULE"}:
        raise ValueError("Invalid shutdown operation")
    sent = False
    try:
        info = os.lstat(SOCKET)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & stat.S_IWOTH:
            raise ValueError("Shutdown socket must be root owned and not world writable")
        deadline = time.monotonic() + 8
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(SOCKET)
            uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid != 0:
                raise ValueError("Shutdown helper peer is not root")
            # sendall can partially write before failure: outcome becomes unknown first.
            sent = operation == "SCHEDULE"
            connection.sendall(operation.encode("ascii") + b"\n")
            raw = b""
            while not raw.endswith(b"\n") and len(raw) <= 1024:
                connection.settimeout(max(.001, deadline - time.monotonic()))
                part = connection.recv(1025 - len(raw))
                if not part:
                    raise ValueError("Incomplete shutdown acknowledgement")
                raw += part
            if len(raw) > 1024:
                raise ValueError("Oversized shutdown acknowledgement")
            body = json.loads(raw)
        if not isinstance(body, dict) or type(body.get("protocol")) is not int or body.get("protocol") != 1:
            raise ValueError("Invalid helper acknowledgement")
        if body.get("state") == "refused":
            raise ShutdownRefused("Shutdown helper refused: verify provisioning and measured safe fold/torque")
        expected = "ready" if operation == "CHECK" else "scheduled"
        if body.get("state") != expected or (operation == "SCHEDULE" and body.get("delay_seconds") != DELAY):
            raise ValueError("Shutdown scheduling was not confirmed")
        return body
    except ShutdownRefused:
        raise
    except Exception as exc:
        if sent:
            raise ShutdownUnknown("Shutdown outcome unknown; check Reachy before deliberately trying again") from exc
        raise ShutdownRefused("Shutdown helper is unavailable; host provisioning is required") from exc
