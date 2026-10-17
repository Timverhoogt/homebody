"""Development-wheel checks; build first, never confuse this with release acceptance."""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wheel():
    # The reviewed build launcher writes to dist/; exact-byte checks below reject a stale wheel.
    paths = sorted((ROOT / "dist").glob("reachy_mini_homebody-*.whl"), key=lambda path: path.stat().st_mtime)
    if not paths:
        pytest.skip("Build the development wheel before running artifact checks")
    return paths[-1]


def test_development_wheel_contains_exact_core_and_no_test_artifacts(wheel):
    with zipfile.ZipFile(wheel) as archive:
        for name in ("homebody/show_and_tell.py", "homebody/show_and_tell_runtime.py", "homebody/config.py",
                     "homebody/static/main.js"):
            assert archive.read(name) == (ROOT / name).read_bytes(), name
        bridge_module = (ROOT / "companion/kids_show_and_tell.py").read_bytes()
        assert archive.read("homebody/bridge/kids_show_and_tell.py") == bridge_module
        assert not any(name.startswith((".artifacts/", "tests/")) for name in archive.namelist())


def test_development_wheel_import_and_policy_execution(wheel, tmp_path):
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from dataclasses import replace
from homebody.config import AppConfig
from homebody.show_and_tell import Authority, SessionGate, explicit_look_request, Unavailable
assert explicit_look_request('Kijk naar mijn tekening!', 'nl')
assert not explicit_look_request('do not look now', 'en')
assert AppConfig().camera_enabled is False  # fresh installs keep the camera approval off
state = {'authority': Authority('synthetic', 1, 1, 1, 1000, 'buddy', 'en')}
gate = SessionGate(lambda: state['authority'], clock=lambda: 100)
try:
    gate.begin()
except Unavailable:
    pass
else:
    raise AssertionError('camera access without Owner permission')
state['authority'] = replace(state['authority'], permitted=True, permission_generation=1)
permit = gate.begin()  # no per-session approval inside the Owner's standing permission
gate.require(permit)
gate.revoke()
try:
    gate.require(permit)
except Unavailable:
    pass
else:
    raise AssertionError('stale permit')
print('wheel policy smoke passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(wheel)], cwd=tmp_path,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wheel policy smoke passed"
