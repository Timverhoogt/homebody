"""Standalone, stdlib-only root helper. Install a root-owned copy; never import the app.

Socket activation supplies one connected AF_UNIX stream as stdin. Only fixed
CHECK/SCHEDULE messages from the provisioned UID are accepted. No paths, shell,
network targets or delay are supplied by the caller. --execute is timer-only.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import socket
import struct
import subprocess
from pathlib import Path

SOCKET = "/run/homebody-shutdown.sock"
TIMER = "homebody-poweroff.timer"
SERVICE = "homebody-poweroff.service"
SYSTEMCTL = "/usr/bin/systemctl"
DELAY = 5


def dedicated_host() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text().lower()
    except OSError:
        return False
    return "raspberry pi" in model or "nvidia jetson" in model


def safe_state(state: dict) -> bool:
    """SDK 1.9 sleep proximity, without importing/constructing the SDK.

    Mirror SLEEP_HEAD_POSE and distance_between_poses/delta_angle_between_mat_rot:
    translation in mm + acos(clipped relative-rotation trace) in degrees <=10.
    Euler xyz is the daemon's radians convention (Rz @ Ry @ Rx). Keep the SDK's
    rounded matrix, rather than silently projecting it to a different rotation.
    """
    if not isinstance(state, dict) or state.get("control_mode") != "disabled":
        return False
    p = state.get("head_pose")
    if not isinstance(p, dict) or not all(
        type(p.get(k)) in (int, float) and math.isfinite(p[k])
        for k in ("x", "y", "z", "roll", "pitch", "yaw")
    ):
        return False
    translation = math.hypot(p["x"] + .021, p["y"] - .001, p["z"] + .044) * 1000
    cr, sr = math.cos(p["roll"]), math.sin(p["roll"])
    cp, sp = math.cos(p["pitch"]), math.sin(p["pitch"])
    cy, sy = math.cos(p["yaw"]), math.sin(p["yaw"])
    rotation = ((cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
                (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
                (-sp, cp * sr, cp * cr))
    target = ((.911, .004, .413), (-.004, 1., -.001), (-.413, -.001, .911))
    trace = sum(rotation[i][j] * target[i][j] for i in range(3) for j in range(3))
    angle = math.degrees(math.acos(max(-1., min(1., (trace - 1) / 2))))
    return p["z"] <= -.03 and p["pitch"] >= .25 and translation + angle <= 10


def physical_safe() -> bool:
    # No proxies, redirects, SDK construction, media requests, or unbounded reads.
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=2)
    try:
        connection.request("GET", "/api/state/full")
        response = connection.getresponse()
        raw = response.read(16385)
        return response.status == 200 and len(raw) <= 16384 and safe_state(json.loads(raw))
    finally:
        connection.close()


def command(*args: str) -> str:
    return subprocess.run([SYSTEMCTL, *args], check=True, timeout=2, capture_output=True,
                          text=True, cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C"}).stdout.strip()


def readiness() -> None:
    if not dedicated_host():
        raise ValueError("Shutdown helper is limited to a dedicated Jetson or Raspberry Pi")
    if command("show", "--property=LoadState", "--value", TIMER, SERVICE).split() != ["loaded", "loaded"]:
        raise ValueError("Shutdown timer/service are not provisioned")


def dispatch(operation: str) -> dict:
    try:
        readiness()
        if operation == "CHECK":
            return {"protocol": 1, "state": "ready"}
        if operation != "SCHEDULE":
            raise ValueError("Unsupported shutdown request")
        if not physical_safe():
            raise ValueError("Shutdown refused: measured safe sleep pose and disabled torque required")
    except Exception:
        return {"protocol": 1, "state": "refused", "detail": "Shutdown helper unavailable or robot not safely folded"}
    try:
        # systemd owns the fixed timer independently of app/helper lifetime. Starting
        # an already-active timer is idempotent: duplicates never postpone shutdown.
        command("start", TIMER)
        if command("is-active", TIMER) != "active":
            raise ValueError("Timer activation was not confirmed")
    except Exception:
        return {"protocol": 1, "state": "unknown", "detail": "OS scheduling acknowledgement unavailable"}
    return {"protocol": 1, "state": "scheduled", "delay_seconds": DELAY}


def serve(connection: socket.socket, uid: int) -> None:
    connection.settimeout(3)
    peer_uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if peer_uid != uid or uid == 0:
        return
    data = b""
    while not data.endswith(b"\n") and len(data) <= 16:
        chunk = connection.recv(17 - len(data))
        if not chunk:
            return
        data += chunk
    if data not in {b"CHECK\n", b"SCHEDULE\n"}:
        return
    result = dispatch(data.decode("ascii").strip())
    connection.sendall(json.dumps(result, separators=(",", ":")).encode() + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        readiness()
        if not physical_safe():
            raise SystemExit("Poweroff refused: measured fold/torque changed or unavailable")
        command("--no-block", "poweroff", "--no-wall")
    elif args.uid is not None and args.uid > 0:
        with socket.socket(fileno=0) as connection:
            serve(connection, args.uid)
    else:
        parser.error("A non-root provisioned UID or --execute is required")


if __name__ == "__main__":
    main()
