"""Evidence: a locally built wheel ships exactly the working-tree app and bridge resources.

Skips when no wheel for the current version is present in dist/. Reads only local files.
"""

import hashlib
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_built_wheel_resources_equal_sources():
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    wheel_path = ROOT / "dist" / f"reachy_mini_homebody-{version}-py3-none-any.whl"
    if not wheel_path.exists():
        pytest.skip("no locally built wheel for this version")
    expected = {f"homebody/bridge/{p.name}": p for p in (ROOT / "companion").glob("*.py")}
    expected.update({
        str(p.relative_to(ROOT)): p for p in (ROOT / "homebody").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    })
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        shipped = {name: wheel.read(name) for name in expected if name in names}
    assert not [n for n in names if ".artifacts" in n or "sdk-reference" in n]
    assert "homebody/face_model.py" in shipped and "homebody/bridge/live_transport.py" in shipped
    mismatched = [name for name, data in shipped.items() if data != expected[name].read_bytes()]
    assert mismatched == []
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    print(f"wheel {wheel_path.name} sha256={digest} compared={len(shipped)} "
          f"not_shipped={sorted(set(expected) - names)}")
