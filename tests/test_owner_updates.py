import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair
from test_updates import manifest

from homebody.config import AppConfig
from homebody.main import Homebody


@pytest.fixture
def app(monkeypatch, tmp_path):
    import homebody.main as main
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig())
    robot = Homebody()
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    return robot, client, code


def test_owner_updates_reject_guest_and_kids(app):
    robot, client, code = app
    assert client.get("/api/updates").status_code == 401
    assert pair(client, code).status_code == 200
    assert client.get("/api/updates").status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    assert client.post("/api/updates/check", json={}, headers={"Origin": ORIGIN}).status_code == 403
    robot._runtime = SimpleNamespace(kids_controls_locked=True)
    assert client.get("/api/updates").status_code == 423
    assert client.post("/api/updates/install", json={}, headers=headers).status_code == 423


def test_owner_install_requires_support_runtime_and_exact_confirmation(app, monkeypatch):
    robot, client, code = app
    pair(client, code)
    csrf = client.get("/api/owner/session").json()["csrf"]
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    response = client.post("/api/updates/install", json={"confirm": "INSTALL", "release_id": 42}, headers=headers)
    assert response.status_code == 409
    assert "topology" in response.text.lower() or "setup" in response.text.lower()
    assert client.get("/api/updates/health").status_code == 401


def test_reservation_stops_runtime_only_after_durable_queue(tmp_path):
    from homebody.runtime import HermesVoiceRuntime
    from homebody.updates import Store
    safe = {"state": "waiting_for_wake_word", "power_mode": "standby", "motors_enabled": False,
            "head_safely_folded": True, "robot_action_busy": False, "camera_control_active": False,
            "announcement_busy": False, "announcement_queue_depth": 0,
            "kids_mode": {"active": False, "locked": False}, "last_error": ""}
    runtime = object.__new__(HermesVoiceRuntime)
    runtime._motor_transition_lock = threading.RLock()
    runtime._voice_activity_lock = threading.Lock()
    runtime._kids_lock = threading.RLock()
    runtime.stop_event = threading.Event()
    runtime._control_ready = threading.Event()
    runtime._control_ready.set()
    runtime.status = lambda: safe
    store = Store(tmp_path)
    runtime.reserve_update(lambda: store.enqueue(manifest(), "0.4.0", []))
    assert runtime.stop_event.is_set()
    assert store.read()["job"]["state"] == "queued"
    # The app can now exit, but the queued journal is already durable for worker recovery.
    runtime.stop_event.clear()
    runtime._control_ready.set()
    runtime._voice_activity_lock.acquire()
    with pytest.raises(ValueError, match="voice"):
        runtime.reserve_update(lambda: None)
    assert not runtime.stop_event.is_set()


def test_later_is_persistent_across_manual_checks(app, monkeypatch):
    from homebody import update_api
    from homebody.updates import atomic_json
    robot, client, code = app
    pair(client, code)
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}
    service = robot.settings_app.state.updates
    atomic_json(service.root / "catalog.json", {"available": manifest(), "notes": [], "checked_at": 1})
    response = client.post("/api/updates/later", json={"release_id": 42}, headers=headers)
    assert response.status_code == 200
    assert client.get("/api/updates").json()["deferred_release_id"] == 42
    monkeypatch.setattr(update_api, "discover", lambda _: {"available": manifest(), "notes": []})
    assert client.post("/api/updates/check", json={}, headers=headers).status_code == 200
    assert client.get("/api/updates").json().get("deferred_release_id") == 42


def test_stopped_runtime_never_enables_motors(monkeypatch):
    from homebody.runtime import HermesVoiceRuntime
    runtime = object.__new__(HermesVoiceRuntime)
    runtime._motor_transition_lock = threading.RLock()
    runtime.stop_event = threading.Event()
    runtime.stop_event.set()
    with pytest.raises(RuntimeError, match="stopping"):
        runtime._set_motor_mode(True)
