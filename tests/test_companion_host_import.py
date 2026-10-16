"""The host bridge is deployed as standalone companion resources, not with robot SDK/package imports."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_standalone_host_bridge_can_recognize_explicit_look_without_robot_package(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Match the real host deployment: only companion resources, no neighboring robot source tree.
    exported = tmp_path / "export"
    shutil.copytree(root / "companion", exported / "companion", ignore=shutil.ignore_patterns("__pycache__"))
    script = r"""
import importlib.abc
import sys
from pathlib import Path
root = Path(sys.argv[1])
class NoRobotPackage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'homebody' or fullname.startswith('homebody.'):
            raise ModuleNotFoundError('Robot package is intentionally not installed on bridge host')
sys.meta_path.insert(0, NoRobotPackage())
sys.path = [str(root/'companion')] + [p for p in sys.path if p and Path(p).resolve() != root]
import hermes_reachy_bridge as bridge
assert bridge._looks_like_look_request('Look at this letter') is True, 'Standalone Live look silently disabled'
assert bridge._looks_like_look_request('Do not look at this letter') is False
print('STANDALONE_BRIDGE_LOOK_OK')
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    run = subprocess.run(
        [sys.executable, "-c", script, str(exported)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "STANDALONE_BRIDGE_LOOK_OK" in run.stdout
