"""Actual Owner/CSRF settings revocation and camera-specific late-admission races."""

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.runtime import HermesVoiceRuntime


def hold_camera_at_status(runtime):
    entered, release = threading.Event(), threading.Event()
    original = runtime._status_lock

    class Hold:
        def __enter__(self):
            if threading.current_thread().name == "held-camera":
                entered.set()
                assert release.wait(3)
            original.acquire()
            return self

        def __exit__(self, *_):
            original.release()

    runtime._status_lock = Hold()
    return entered, release


def ready(config_loader):
    runtime = HermesVoiceRuntime(
        SimpleNamespace(media=SimpleNamespace()), threading.Event(), config_loader=config_loader
    )
    runtime._control_ready.set()
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._accept_wake_turn()
    runtime._capture_camera_jpeg = lambda **kw: b"synthetic jpeg"
    called = []
    vision = SimpleNamespace(describe=lambda *a: called.append(1) or {"answer": "late"})
    runtime._new_local_vision_client = lambda config: nullcontext(vision)
    return runtime, called


@pytest.mark.parametrize(
    "name,args",
    [
        ("start_reachy_camera", {"duration_seconds": 30}),
        ("capture_reachy_camera", {"purpose": "read label"}),
    ],
)
def test_camera_off_wins_at_status_boundary_before_lease_commit_or_vision(name, args):
    config = AppConfig(camera_enabled=True, local_vision_enabled=True)
    runtime, called = ready(lambda: config)
    entered, release = hold_camera_at_status(runtime)
    results = []
    worker = threading.Thread(
        name="held-camera", target=lambda: results.append(runtime.execute_everyday_tool(name, args))
    )
    worker.start()
    assert entered.wait(2)
    assert runtime.execute_everyday_tool("stop_reachy_camera", {})["ok"]
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert not results[0]["ok"] and not called
    assert not runtime.everyday_local_capabilities()["activity"]["camera_session_active"]


def test_general_camera_reenable_cannot_restore_pre_revocation_capture(monkeypatch, tmp_path):
    import homebody.main as main

    configs = [
        AppConfig(api_key="fixture-only", camera_enabled=True, local_vision_enabled=True)
    ]
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: configs[0])

    def save(config):
        configs[0] = config
        return tmp_path / "not-written-config.json"

    monkeypatch.setattr(main, "save_config", save)
    robot = Homebody()
    runtime, called = ready(lambda: configs[0])
    robot._runtime = runtime
    robot._agent_listener = SimpleNamespace(sync=lambda config: None)
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    assert pair(client, code).status_code == 200
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}
    entered, release = hold_camera_at_status(runtime)
    results = []
    worker = threading.Thread(
        name="held-camera",
        target=lambda: results.append(
            runtime.execute_everyday_tool("capture_reachy_camera", {"purpose": "read label"})
        ),
    )
    worker.start()
    assert entered.wait(2)
    original = runtime._everyday_camera_generation
    assert client.post("/api/settings", json={"camera_enabled": False}, headers=headers).status_code == 200
    assert runtime._everyday_camera_generation > original
    assert client.post("/api/settings", json={"camera_enabled": True}, headers=headers).status_code == 200
    assert configs[0].camera_enabled
    release.set()
    worker.join(2)
    assert not worker.is_alive() and not results[0]["ok"] and not called
    assert not (tmp_path / "not-written-config.json").exists()
