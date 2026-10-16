"""Independent queued camera-window revocation boundary; silent SDK only."""

from types import SimpleNamespace

from test_everyday_runtime import runtime

from homebody.everyday_runtime import EverydayCallbackScope


def test_queued_start_cannot_rebase_after_camera_stop(monkeypatch):
    r, _ = runtime(camera_enabled=True)
    captures, queued, delivered = [], [], []
    r._capture_camera_jpeg = lambda **kwargs: captures.append(1) or b"fixture-jpeg"

    def deferred_thread(*args, **kwargs):
        return SimpleNamespace(start=lambda: queued.append(kwargs["target"]))

    monkeypatch.setattr("homebody.everyday_runtime.threading.Thread", deferred_thread)
    original_fence = r.everyday_tool_fence()
    r.launch_everyday_tool(
        "start_reachy_camera",
        {"duration_seconds": 30},
        fence=original_fence,
        call_id="original-start",
        deliver=delivered.append,
        is_current=lambda: True,
        scope=EverydayCallbackScope(transcript="Turn on the camera"),
    )
    assert len(queued) == 1
    r.revoke_everyday_camera()
    queued[0]()
    assert captures == [], "Stopped queued start must not obtain fresh camera authority"
    assert delivered and delivered[0]["ok"] is False
    assert not r.everyday_local_capabilities()["activity"]["camera_session_active"]
    # Camera-only revocation must not retire ordinary controls from the same voice scope.
    assert r.execute_everyday_tool("get_reachy_status", {}, fence=original_fence)["ok"]
    # A genuinely fresh camera authority can still open an explicitly requested window.
    fresh = r.execute_everyday_callback(
        "start_reachy_camera",
        {"duration_seconds": 30},
        fence=r.everyday_tool_fence(),
        scope=EverydayCallbackScope(transcript="Turn on the camera"),
    )
    assert fresh["ok"] and fresh["camera_session_active"]
    assert captures == [1]
