#!/usr/bin/env python3
"""Verify every wheel resource/RECORD and prepare a private offline admin bundle.

No release publication, target discovery, privileged installation or robot action.
Run after committing the candidate and building its wheel. Same-version output
is explicitly a development candidate, never an immutable published replacement.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if git("status", "--porcelain", "--untracked-files=normal"):
        parser.error("Commit the exact candidate first; working tree must be clean")
    wheel = args.wheel.resolve()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    expected = {}
    for package, folder in (("homebody", ROOT / "homebody"), ("homebody.bridge", ROOT / "companion")):
        prefix = package.replace(".", "/") + "/"
        patterns = ["*.py", *config["tool"]["setuptools"]["package-data"][package]]
        for pattern in patterns:
            for path in folder.glob(pattern):
                if path.is_file():
                    expected[prefix + path.relative_to(folder).as_posix()] = path.read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        members = {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}
    resources = {n: raw for n, raw in members.items() if n.startswith("homebody/")}
    if resources != expected:
        changed = sorted(n for n in resources.keys() & expected.keys() if resources[n] != expected[n])
        raise ValueError(f"Wheel/source mismatch: missing={sorted(expected.keys()-resources.keys())}, "
                         f"extra={sorted(resources.keys()-expected.keys())}, changed={changed}")
    record = [n for n in members if n.endswith(".dist-info/RECORD")]
    if len(record) != 1:
        raise ValueError("Ambiguous wheel RECORD")
    checked = set()
    for name, digest, size in csv.reader(io.StringIO(members[record[0]].decode())):
        if name == record[0]:
            continue
        raw = members[name]
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        if digest != "sha256=" + encoded or str(len(raw)) != size:
            raise ValueError(f"Invalid wheel RECORD: {name}")
        checked.add(name)
    if checked != members.keys() - {record[0]}:
        raise ValueError("Incomplete wheel RECORD")
    commit = git("rev-parse", "HEAD")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "tools/install_shutdown_helper.py": (ROOT / "tools/install_shutdown_helper.py").read_bytes(),
        "docs/host-shutdown.md": (ROOT / "docs/host-shutdown.md").read_bytes(),
        "homebody/shutdown_helper.py": resources["homebody/shutdown_helper.py"],
        "candidate/" + wheel.name: wheel.read_bytes(),
    }
    files.update({n: raw for n, raw in resources.items() if n.startswith("homebody/provision/")})
    receipt = {
        "kind": "private-development-candidate-not-published-not-deployed", "source_commit": commit,
        "baseline_commit": "3878f6a7c410f1da342b532d2891cb1417d40c83", "version": config["project"]["version"],
        "wheel_sha256": sha(wheel.read_bytes()), "verified_wheel_resources": len(resources),
        "verified_record_entries": len(checked),
        "resource_sha256": {n: sha(raw) for n, raw in sorted(resources.items())},
        "target_provisioning": "not-installed-awaiting-parent-managed-deployment",
        "physical_shutdown_acceptance": "not-run",
    }
    files["candidate-receipt.json"] = (json.dumps(receipt, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(f"{sha(raw)}  {n}\n" for n, raw in sorted(files.items())).encode()
    target = output / f"homebody-shutdown-candidate-{commit[:12]}.tar.gz"
    if target.exists():
        raise FileExistsError("Refusing to overwrite an existing private bundle")
    with tarfile.open(target, "w:gz") as archive:
        for name, raw in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(raw), 0o644, int(git("show", "-s", "--format=%ct", "HEAD"))
            archive.addfile(info, io.BytesIO(raw))
    # Independently reopen and verify every bundled checksum, not just the tar writer.
    with tarfile.open(target, "r:gz") as archive:
        bundled = {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
    for line in bundled["SHA256SUMS"].decode().splitlines():
        digest, name = line.split("  ", 1)
        if sha(bundled[name]) != digest:
            raise ValueError(f"Bundle checksum failed: {name}")
    checksum = sha(target.read_bytes())
    target.with_suffix(target.suffix + ".sha256").write_text(f"{checksum}  {target.name}\n")
    (output / "candidate-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"bundle": str(target), "sha256": checksum, "receipt": receipt}, indent=2))


if __name__ == "__main__":
    main()
