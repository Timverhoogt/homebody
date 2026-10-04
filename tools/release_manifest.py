"""Generate release-bound package identity before build, then the checksum manifest after build.

Use a draft GitHub release ID, full commit SHA, versioned CHANGELOG section and SOURCE_DATE_EPOCH.
This tool never publishes or downloads anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homebody.updates import REPOSITORY, validate_manifest, verify_wheel, version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release-id", type=int, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/homebody-update.json"))
    args = parser.parse_args()
    project = tomllib.loads((args.root / "pyproject.toml").read_text())["project"]
    v = project["version"]
    version(v)
    if project["name"] != "reachy-mini-homebody" or args.release_id <= 0:
        raise ValueError("Invalid release package/ID")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise ValueError("An exact commit SHA is required")
    init = (args.root / "homebody/__init__.py").read_text()
    if not re.search(rf'__version__ = "{re.escape(v)}"', init):
        raise ValueError("Package and build versions disagree")
    changelog = (args.root / "CHANGELOG.md").read_text()
    section = re.search(rf"^## \[{re.escape(v)}\][^\n]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not section or not section[1].strip():
        raise ValueError("Write a versioned CHANGELOG section before creating release artifacts")
    identity = {"version": v, "release_id": args.release_id, "commit": args.commit}
    if args.prepare:
        (args.root / "homebody/release.json").write_text(json.dumps(identity, sort_keys=True) + "\n")
        return
    if args.wheel is None:
        raise ValueError("--wheel is required after preparing/building")
    data = {"schema": 1, "repository": REPOSITORY, **identity, "tag": "v" + v,
            "python": "3.11", "platform": "linux", "sdk": "1.9", "artifact": args.wheel.name,
            "size": args.wheel.stat().st_size, "sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
            "notes": section[1].strip()}
    validate_manifest(data)
    verify_wheel(args.wheel, data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
