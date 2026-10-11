"""App-only slots using the existing Reachy daemon and its provisioned interpreter.

No new app supervisor, daemon restart, settings edits or dependency upgrades.
The package and dist-info import anchors both resolve through one atomic current link.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

import httpx

from .update_worker import SystemdSlots, dependency_env
from .updates import compatible_host

APP = "reachy_mini_homebody"
BASE = "http://127.0.0.1:8000"


class DaemonSlots(SystemdSlots):
    def api(self, path: str, post: bool = False):
        with httpx.Client(timeout=45 if post else 5, trust_env=False) as client:
            response = client.request("POST" if post else "GET", BASE + path)
            response.raise_for_status()
            return response.json()

    def physical_safe(self) -> None:
        state = self.api("/api/state/full")
        pose = state.get("head_pose") or {}
        if (state.get("control_mode") != "disabled"
                or not all(type(pose.get(k)) in (int, float) and math.isfinite(pose[k])
                           for k in ("x", "y", "z", "roll", "pitch", "yaw"))
                or pose["z"] > -.03 or pose["pitch"] < .25):
            raise ValueError("Finite physical fold and disabled torque are required")

        sdk = self.sleep_distance()
        if (sdk.get("safe") is not True
                or any(type(sdk.get(k)) not in (int, float) or not math.isfinite(sdk[k])
                       for k in ("distance", "threshold"))
                or sdk["threshold"] <= 0 or not 0 <= sdk["distance"] <= sdk["threshold"]):
            raise ValueError("SDK sleep-pose distance is unsafe; stop could enable motors")

    def sleep_distance(self) -> dict:
        # Import constants/helpers only: never construct a ReachyMini SDK client (it resets media).
        # The SDK stop route wakes heads outside this exact distance, even if a broad fold test passes.
        probe = """
import json, math, urllib.request
import numpy as np
from scipy.spatial.transform import Rotation
import reachy_mini.apps.manager as manager
from reachy_mini.utils.interpolation import distance_between_poses
with urllib.request.urlopen('http://127.0.0.1:8000/api/state/full', timeout=3) as response:
    state = json.load(response)
p = state.get('head_pose') or {}
safe = (state.get('control_mode') == 'disabled' and
        all(type(p.get(k)) in (int,float) and math.isfinite(p[k])
            for k in ('x','y','z','roll','pitch','yaw')))
if not safe:
    print(json.dumps({'safe': False, 'distance': None, 'threshold': manager.SLEEP_POSE_MAGIC_DISTANCE}))
else:
    matrix = np.eye(4)
    matrix[:3,3] = [p['x'],p['y'],p['z']]
    matrix[:3,:3] = Rotation.from_euler('xyz',[p['roll'],p['pitch'],p['yaw']]).as_matrix()
    distance = distance_between_poses(matrix,manager.RobotBackend.SLEEP_HEAD_POSE)[2]
    print(json.dumps({'safe': True, 'distance': float(distance), 'threshold': manager.SLEEP_POSE_MAGIC_DISTANCE}))
"""
        result = subprocess.run([self.python, "-c", probe], check=True, capture_output=True, text=True,
                                timeout=15, env=dependency_env(), cwd="/")
        return json.loads(result.stdout)

    def own_app(self):
        app = self.api("/api/apps/current-app-status")
        if app is not None and app.get("info", {}).get("name") != APP:
            raise ValueError("Another robot app owns the daemon; refusing to replace it")
        return app

    def preflight(self) -> None:
        if not compatible_host():
            raise ValueError("Supported worker is Linux / Python 3.11 or 3.12")
        probe = (
            "import importlib.metadata as m,json,sys,sysconfig; "
            "eps=list(m.entry_points(group='reachy_mini_apps',name='reachy_mini_homebody')); "
            "print(json.dumps({'python':'%d.%d'%sys.version_info[:2], 'sdk':m.version('reachy-mini'),"
            "'module':eps[0].module if len(eps)==1 else None,'site':sysconfig.get_paths()['purelib']}))"
        )
        result = subprocess.run([self.python, "-c", probe], check=True, capture_output=True,
                                text=True, timeout=15, env=dependency_env(), cwd="/")
        info = json.loads(result.stdout)
        import re
        if (info.get("python") not in {"3.11", "3.12"}
                or not re.fullmatch(r"1\.9\.\d+", str(info.get("sdk")))
                or info.get("module") != "homebody.main"
                or info.get("site") != self.value["site_packages"]):
            raise ValueError("Daemon target environment/entrypoint does not match the provisioned app")
        self.physical_safe()
        self.own_app()

    def install(self, wheel: Path, slot: Path) -> None:
        super().install(wheel, slot)
        metadata = list(slot.glob("reachy_mini_homebody-*.dist-info"))
        if len(metadata) != 1 or not (metadata[0] / "METADATA").is_file():
            raise ValueError("Installed app metadata is missing or ambiguous")
        (slot / ".homebody-dist-info").symlink_to(metadata[0].name)

    def wait_stopped(self, app):
        # SDK 1.9 retains DONE after a clean exit; STOPPING is active but must not
        # receive another Stop. ERROR remains active until explicit SDK cleanup.
        deadline = time.monotonic() + 35
        while app is not None and app.get("state") != "done":
            if app.get("state") != "stopping":
                raise ValueError("Daemon-owned app has not entered stopping")
            if time.monotonic() >= deadline:
                raise ValueError("Daemon-owned app did not stop")
            time.sleep(.25)
            app = self.own_app()
        return app

    def stop(self) -> None:
        # The SDK can wake an unfolded app during stop. Verify physical safety FIRST.
        self.physical_safe()
        app = self.own_app()
        if app is not None and app.get("state") not in {"done", "stopping"}:
            if app.get("state") not in {"starting", "running", "error"}:
                raise ValueError("Unknown daemon app lifecycle state")
            self.physical_safe()
            try:
                self.api("/api/apps/stop-current-app", post=True)
            except httpx.HTTPStatusError as exc:
                # Clean exit or an existing stop can win between GET and POST.
                # Accept ONLY the SDK's specific 400 race with verified own-app
                # DONE/STOPPING/null; other failures still abort and recover.
                app = self.own_app()
                if (exc.response.status_code != 400 or
                        (app is not None and app.get("state") not in {"done", "stopping"})):
                    raise
            else:
                app = self.own_app()
        self.wait_stopped(app)
        self.physical_safe()

    def start(self) -> None:
        self.physical_safe()
        app = self.own_app()
        if app is not None and app.get("state") == "stopping":
            app = self.wait_stopped(app)
            self.physical_safe()
        if app is None or app.get("state") == "done":
            self.api("/api/apps/start-app/" + APP, post=True)
        elif app.get("state") not in {"starting", "running"}:
            raise ValueError("Daemon app has not reached a startable state")

    def healthy(self, identity: dict, deadline: float = 90) -> bool:
        if not super().healthy(identity, deadline):
            return False
        try:
            self.physical_safe()
            app = self.own_app()
            return app is not None and app.get("state") == "running"
        except (ValueError, httpx.HTTPError):
            return False
