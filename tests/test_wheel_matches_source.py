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


# Source resources deliberately left out of a development wheel (not a release build).
_NOT_SHIPPED = frozenset({"homebody/bridge/README.md"})  # companion/README.md is repository documentation


def test_full_wheel_manifest_matches_source_by_sha256(record_testsuite_property):
    """Every wheel entry maps to a source file with an equal SHA-256, and every source resource ships."""
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    wheel_path = ROOT / "dist" / f"reachy_mini_homebody-{version}-py3-none-any.whl"
    if not wheel_path.exists():
        pytest.skip("no locally built wheel for this version")
    source = {f"homebody/bridge/{p.name}": p for p in (ROOT / "companion").iterdir() if p.is_file()}
    source.update({
        str(p.relative_to(ROOT)): p for p in (ROOT / "homebody").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    })
    with zipfile.ZipFile(wheel_path) as wheel:
        entries = {name: hashlib.sha256(wheel.read(name)).hexdigest() for name in wheel.namelist()
                   if not name.endswith("/")}
    generated = {name for name in entries if ".dist-info/" in name}
    unmapped = sorted(set(entries) - generated - set(source))
    mismatched = sorted(name for name in set(entries) & set(source)
                        if entries[name] != hashlib.sha256(source[name].read_bytes()).hexdigest())
    missing = sorted(set(source) - set(entries))
    record_testsuite_property("wheel_sha256", hashlib.sha256(wheel_path.read_bytes()).hexdigest())
    record_testsuite_property("wheel_entries", len(entries))
    record_testsuite_property("dist_info_entries", len(generated))
    record_testsuite_property("source_matched", len(set(entries) & set(source)))
    record_testsuite_property("not_shipped", ",".join(missing))
    assert unmapped == [] and mismatched == []
    assert missing == sorted(_NOT_SHIPPED), missing
