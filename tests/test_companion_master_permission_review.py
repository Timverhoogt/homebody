"""Single-owner camera permission × immutable Companion window; no real media/providers."""

import json

import pytest
from test_companion import make_runtime

from homebody.companion import LookCancelled
from homebody.config import load_config


@pytest.mark.parametrize("legacy", [False, True])
def test_legacy_flag_and_window_never_override_disabled_owner_camera(monkeypatch, tmp_path, legacy):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"camera_enabled": False, "everyday_camera_enabled": legacy}))
    loaded = load_config(path)
    assert "everyday_camera_enabled" not in loaded.redacted_dict()
    r, client, captures = make_runtime(camera_enabled=loaded.camera_enabled)
    monkeypatch.setattr("homebody.companion.time.monotonic", lambda: 1000.0)
    r._everyday_camera_until = 1060.0
    try:
        assert r.companion_public_status(r.config_loader())["camera"]["permitted"] is False
        with pytest.raises(RuntimeError, match="Camera access is disabled"):
            r.look_at_this("Read this letter")
        assert captures == [] and client.looks == [] and r._look_context is None
    finally:
        r._companion_teardown("test_cleanup")


def test_master_permission_needs_no_legacy_switch_and_bounds_retained_context(monkeypatch):
    r, client, captures = make_runtime(camera_enabled=True)
    clock = [1000.0]
    monkeypatch.setattr("homebody.companion.time.monotonic", lambda: clock[0])
    r._everyday_camera_until = 1005.0
    try:
        assert r.look_at_this("Read this letter")["ok"]
        assert captures == [1] and len(client.looks) == 1
        assert r._look_context is not None and r._look_context.expires_at == 1005.0
        clock[0] = 1006.0
        with pytest.raises(RuntimeError):
            r.follow_up_look("Which day?")
        assert r._look_context is None and client.follow_ups == []
        assert r.config_loader().camera_enabled is True
    finally:
        r._companion_teardown("test_cleanup")


def test_new_window_cannot_extend_original_queued_look_authority(monkeypatch):
    r, client, captures = make_runtime(camera_enabled=True)
    clock = [1000.0]
    monkeypatch.setattr("homebody.companion.time.monotonic", lambda: clock[0])
    r._everyday_camera_until = 1005.0
    try:
        original = r._companion_authority("phone")
        r._everyday_camera_until = 1020.0
        clock[0] = 1001.0
        nested = r._companion_authority("phone", original)
        assert nested.camera_deadline == original.camera_deadline == 1005.0
        assert nested()
        clock[0] = 1006.0
        assert not original() and not nested()
        with pytest.raises(LookCancelled):
            r.look_at_this("Read this letter", authority=original)
        assert captures == [] and client.looks == [] and r._look_context is None
    finally:
        r._companion_teardown("test_cleanup")


def test_expired_window_renewed_during_client_construction_never_submits_old_image(monkeypatch):
    r, client, captures = make_runtime(camera_enabled=True)
    clock = [1000.0]
    monkeypatch.setattr("homebody.companion.time.monotonic", lambda: clock[0])
    r._everyday_camera_until = 1005.0

    def prepare(_config):
        clock[0] = 1006.0
        r._everyday_camera_until = 1020.0
        return client

    r._new_bridge_client = prepare
    try:
        with pytest.raises(LookCancelled, match="after bridge client preparation"):
            r.look_at_this("Read this letter")
        assert captures == [1]  # Authorized capture preceded the held client construction.
        assert client.looks == [] and r._look_context is None
    finally:
        r._companion_teardown("test_cleanup")
