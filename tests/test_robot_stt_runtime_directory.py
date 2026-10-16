"""Keep native STT available when systemd hides an inherited XDG_RUNTIME_DIR."""
import os
from pathlib import Path

import pytest

from homebody import robot_stt
from homebody.asr_worker import UntrustedEndpointError, private_dir, require_trusted_endpoint


def test_accessible_xdg_is_preserved(monkeypatch, tmp_path):
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base))
    assert robot_stt.default_socket_path() == base / f"homebody-robot-stt-{os.getuid()}" / "worker.sock"


@pytest.mark.parametrize("reason", ["missing", "inaccessible", "readonly", "relative", "file"])
def test_unusable_xdg_uses_private_temp_parent(monkeypatch, tmp_path, reason):
    base = tmp_path / "xdg"
    fallback = tmp_path / "private-tmp"
    fallback.mkdir(mode=0o700)
    if reason == "file":
        base.touch(mode=0o700)
    elif reason != "missing":
        base.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative-runtime" if reason == "relative" else str(base))
    monkeypatch.setattr(robot_stt.tempfile, "gettempdir", lambda: str(fallback))
    access = os.access
    if reason in {"inaccessible", "readonly"}:
        monkeypatch.setattr(robot_stt.os, "access", lambda p, mode: False if Path(p) == base else access(p, mode))
    result = robot_stt.default_socket_path()
    assert result.parent.parent == fallback
    private_dir(result.parent)
    assert result.parent.stat().st_mode & 0o777 == 0o700
    assert not require_trusted_endpoint(result)


def test_unset_xdg_retains_temp_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(robot_stt.tempfile, "gettempdir", lambda: str(tmp_path))
    assert robot_stt.default_socket_path().parent.parent == tmp_path


@pytest.mark.parametrize("kind", ["symlink", "public"])
def test_fallback_does_not_bypass_existing_endpoint_trust(monkeypatch, tmp_path, kind):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(robot_stt.tempfile, "gettempdir", lambda: str(tmp_path))
    parent = tmp_path / f"homebody-robot-stt-{os.getuid()}"
    if kind == "symlink":
        target = tmp_path / "other"
        target.mkdir(mode=0o700)
        parent.symlink_to(target, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
        parent.chmod(0o755)
    with pytest.raises(UntrustedEndpointError):
        private_dir(robot_stt.default_socket_path().parent)
