#!/usr/bin/env python3
"""Explicit host-admin provisioning; --stage renders artifacts without privilege/install.

Run with system Python -I. Never run this from the app or updater. It does not
change the app service, sudoers, Polkit, settings, boot app or Python environment.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ("homebody-shutdown.socket", "homebody-shutdown@.service",
         "homebody-poweroff.timer", "homebody-poweroff.service")


def trusted_directory(path: Path, owner: int) -> None:
    if not path.exists():
        trusted_directory(path.parent, owner)
        path.mkdir(mode=0o755)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        raise ValueError(f"Untrusted install directory: {path}")


def put(path: Path, content: bytes, owner: int) -> None:
    trusted_directory(path.parent, owner)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise ValueError(f"Untrusted existing install file: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-user", required=True, help="Actual non-root daemon/app account")
    parser.add_argument("--stage", type=Path, help="Render only beneath an existing private staging directory")
    args = parser.parse_args()
    account = pwd.getpwnam(args.app_user)
    if account.pw_uid == 0:
        parser.error("Root app accounts are not supported")
    group = grp.getgrgid(account.pw_gid).gr_name
    if not group.replace("_", "").replace("-", "").isalnum():
        parser.error("Unsupported primary group name")
    staged = args.stage is not None
    if staged:
        base = args.stage.absolute()
        trusted_directory(base, os.geteuid())
    else:
        if os.geteuid() != 0:
            parser.error("Explicit root admin provisioning required; use --stage for offline review")
        model = Path("/proc/device-tree/model").read_text().lower()
        if "raspberry pi" not in model and "nvidia jetson" not in model:
            parser.error("This is not a dedicated Reachy Jetson or Raspberry Pi")
        base = Path("/")
    owner = os.geteuid()
    # Verify every ancestor for the real privileged destinations, not just the leaf.
    if not staged:
        for target in (Path("/opt"), Path("/etc/systemd/system")):
            for parent in reversed((target, *target.parents)):
                trusted_directory(parent, 0)
    put(base / "opt/homebody-shutdown/shutdown_helper.py",
        (ROOT / "homebody/shutdown_helper.py").read_bytes(), owner)
    for name in UNITS:
        text = (ROOT / "homebody/provision" / name).read_text()
        text = text.replace("@APP_UID@", str(account.pw_uid)).replace("@APP_GROUP@", group)
        put(base / "etc/systemd/system" / name, text.encode(), owner)
    if not staged:
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True, timeout=15)
        # Enable ONLY socket admission. Never start/enable the poweroff timer here.
        subprocess.run(["/usr/bin/systemctl", "enable", "--now", "homebody-shutdown.socket"],
                       check=True, timeout=15)
    print("Rendered only; no services changed" if staged else
          "Helper socket installed. Verify app sandbox access and authenticated readiness; no shutdown was requested.")


if __name__ == "__main__":
    main()
