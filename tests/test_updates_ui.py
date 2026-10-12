import shutil
import subprocess
from pathlib import Path

import pytest


def test_update_browser_behavior():
    if not shutil.which("node"):
        pytest.skip("node is required for browser DOM behavior")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "tests/fixtures/updates_dom.cjs"], cwd=root,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
