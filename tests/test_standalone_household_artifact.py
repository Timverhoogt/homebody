"""The exact development wheel carries the household implementation and existing consent UI."""
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

from homebody import __version__


def test_packaged_household_entrypoint_imports_without_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with ZipFile(root / f"dist/reachy_mini_homebody-{__version__}-py3-none-any.whl") as archive:
        archive.extractall(tmp_path)
        metadata = archive.read(f"reachy_mini_homebody-{__version__}.dist-info/entry_points.txt").decode()
        assert "homebody-household = homebody.household_launcher:main" in metadata
    code = (
        "import runpy, sys; sys.argv=['homebody-household', '--help']; "
        f"runpy.run_path({str(tmp_path / 'homebody/household_launcher.py')!r})['main']()"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Household-only MCP and voice service" in result.stdout


def test_standalone_household_wheel_matches_source():
    root = Path(__file__).resolve().parents[1]
    wheel = root / f"dist/reachy_mini_homebody-{__version__}-py3-none-any.whl"
    with ZipFile(wheel) as archive:
        for name in ("household_catalogue.py", "household_server.py", "household_mcp.py", "household_robot.py",
                     "everyday_tools.py",
                     "everyday_manifest.py", "hermes_reachy_bridge.py", "reachy_agent_broker.py"):
            assert archive.read("homebody/bridge/" + name) == (root / "companion" / name).read_bytes()
        for name in ("mcp_server.py", "household.py", "household_client.py", "household_launcher.py",
                     "everyday_runtime.py",
                     "static/household.js",
                     "static/main.js", "static/index.html", "static/service-worker.js"):
            assert archive.read("homebody/" + name) == (root / "homebody" / name).read_bytes()
