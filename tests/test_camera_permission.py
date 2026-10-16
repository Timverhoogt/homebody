"""One Owner-controlled camera permission; no implicit activation."""

import json

from test_everyday_runtime import runtime

from homebody.config import load_config


def test_existing_camera_permission_unlocks_look_and_short_sessions_without_activation():
    r, c = runtime(camera_enabled=True)
    names = r.everyday_local_tools()
    assert {"capture_reachy_camera", "start_reachy_camera", "stop_reachy_camera"} <= set(names)
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]
    assert not r.everyday_local_capabilities()["activity"]["camera_request_active"]
    assert not c.camera_feed_enabled and not c.face_tracking_enabled


def test_old_temporary_permission_does_not_silently_enable_general_camera(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"camera_enabled": False, "everyday_camera_enabled": True}))
    c = load_config(path)
    assert not c.camera_enabled
    assert "everyday_camera_enabled" not in c.redacted_dict()


def test_explicit_single_look_needs_no_separate_window_and_does_not_create_one():
    r, _ = runtime(camera_enabled=True)
    r._capture_camera_jpeg = lambda **kw: b"jpeg"
    r._everyday_describe_frame = lambda *a, **kw: "fixture description"
    assert r.execute_everyday_tool("capture_reachy_camera", {"purpose": "read label"})["ok"]
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]


def test_window_expiry_and_renewal_cannot_rescue_an_inflight_look(monkeypatch):
    r, _ = runtime(camera_enabled=True)
    now = [100.0]
    monkeypatch.setattr("homebody.everyday_runtime.time.monotonic", lambda: now[0])
    r._capture_camera_jpeg = lambda **kw: b"jpeg"
    assert r.execute_everyday_tool("start_reachy_camera", {"duration_seconds": 5})["ok"]

    def held_capture(**kwargs):
        now[0] = 106.0
        r._everyday_camera_until = 226.0  # Renewal cannot donate authority to the old look.
        return b"jpeg"

    r._capture_camera_jpeg = held_capture
    r._everyday_describe_frame = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no expired upload"))
    assert not r.execute_everyday_tool("capture_reachy_camera", {"purpose": "read label"})["ok"]
