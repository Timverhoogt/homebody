"""Owner boundary for Kids Chat Show and Tell (real OwnerBoundary middleware, loopback fakes only)."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair
from test_show_and_tell_runtime import Bridge, Robot

from homebody.config import AppConfig, load_config, save_config
from homebody.kids_mode import KidsProfile
from homebody.main import Homebody
from homebody.runtime import HermesVoiceRuntime


def owner_headers(client) -> dict[str, str]:
    session = client.get("/api/owner/session").json()
    return {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig())
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    calls: list[tuple[str, object]] = []
    robot._runtime = SimpleNamespace(
        kids_controls_locked=True,
        show_tell_sync_permission=lambda revoke=False: calls.append(("sync", revoke)) or {"enabled": not revoke},
        show_tell_look_now=lambda: calls.append(("look-now", None)) or {"accepted": True},
    )
    monkeypatch.setattr("homebody.main.save_config", lambda config: calls.append(("save", config)))
    return robot, TestClient(robot.settings_app, base_url=ORIGIN), code, calls


def test_owner_show_and_tell_requires_pairing_and_csrf_and_stays_available_while_locked(stub) -> None:
    robot, client, code, calls = stub
    for route, body in (("setting", {"enabled": False}), ("look-now", {})):
        assert client.post(f"/api/kids/show-and-tell/{route}", json=body).status_code == 401
    assert calls == []
    robot._runtime.kids_controls_locked = False
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    robot._runtime.kids_controls_locked = True
    for route, body in (("setting", {"enabled": False}), ("look-now", {})):
        # Same-origin CSRF is enforced for every caregiver control.
        response = client.post(f"/api/kids/show-and-tell/{route}", json=body, headers={"Origin": ORIGIN})
        assert response.status_code == 403
    assert calls == []
    response = client.post("/api/kids/show-and-tell/setting", json={"enabled": False}, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls[-2][0] == "save" and calls[-2][1].kids_show_and_tell_enabled is False
    assert calls[-1] == ("sync", True)  # a saved opt-out always cancels outstanding work
    assert client.post("/api/kids/show-and-tell/look-now", headers=headers).status_code == 200
    # A client boolean-ish value, a missing flag or extra authority never becomes a setting.
    count = len(calls)
    for body in ({"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "camera_enabled": True},
                 {"enabled": True, "session_id": "kids-x"}):
        assert client.post("/api/kids/show-and-tell/setting", json=body, headers=headers).status_code == 422
    assert len(calls) == count
    # Other parent controls, including the global camera approval, stay locked during the session.
    assert client.post("/api/settings", json={"camera_enabled": True}, headers=headers).status_code == 423
    # The per-session approval routes of the earlier opt-in design are gone.
    robot._runtime.kids_controls_locked = False
    for route in ("challenge", "consent", "revoke"):
        assert client.post(f"/api/kids/show-and-tell/{route}", json={}, headers=headers).status_code in {404, 405}


def test_owner_logout_blocks_show_and_tell(stub) -> None:
    robot, client, code, calls = stub
    robot._runtime.kids_controls_locked = False
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    assert client.post("/api/owner/logout", headers=headers).status_code in {200, 204}
    robot._runtime.kids_controls_locked = True
    assert client.post("/api/kids/show-and-tell/setting", json={"enabled": True}, headers=headers).status_code == 401
    assert calls == []


@pytest.fixture
def persisted(monkeypatch, tmp_path):
    """Real saved settings file, real load/save and a real runtime reading the same file."""
    import homebody.main as main

    path = tmp_path / "config.json"
    monkeypatch.setenv("HOMEBODY_CONFIG", str(path))
    monkeypatch.setattr(main, "default_config_path", lambda: path)
    save_config(AppConfig(api_key="test-key", camera_enabled=True))
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    events: list[object] = []

    def runtime_for() -> HermesVoiceRuntime:
        runtime = HermesVoiceRuntime(Robot(events, {}), threading.Event(), config_loader=load_config)
        runtime._audio_ready = True
        runtime._output_sample_rate = 24000
        monkeypatch.setattr(runtime, "_new_bridge_client", lambda config: Bridge(events, {}, config))
        monkeypatch.setattr(runtime, "set_power_mode", lambda *_args, **_kwargs: None)
        robot._runtime = runtime
        return runtime

    runtime = runtime_for()
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    yield runtime_for, runtime, client, headers, events, path
    if robot._runtime is not None:
        robot._runtime.stop_kids_mode(fold=False)


def kids_state(client):
    return client.get("/api/status").json()["runtime"]["kids_mode"]["show_and_tell"]


def test_owner_opt_out_persists_and_turning_on_needs_no_session_approval(persisted) -> None:
    runtime_for, runtime, client, headers, events, path = persisted
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    assert kids_state(client) == {
        "camera_approved": True, "enabled": True, "session": True, "available": True, "looking": False,
    }
    off = client.post("/api/kids/show-and-tell/setting", json={"enabled": False}, headers=headers)
    assert off.status_code == 200 and off.json()["available"] is False
    assert json.loads(path.read_text())["kids_show_and_tell_enabled"] is False
    assert client.post("/api/kids/show-and-tell/look-now", headers=headers).status_code == 409
    runtime.stop_kids_mode(fold=False)

    restarted = runtime_for()  # a restart reads the saved opt-out; nothing captures on startup
    restarted.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    assert kids_state(client)["enabled"] is False and kids_state(client)["available"] is False
    on = client.post("/api/kids/show-and-tell/setting", json={"enabled": True}, headers=headers)
    assert on.status_code == 200 and on.json()["available"] is True
    assert events.count("capture") == 0  # enabling never captures, wakes or listens
    assert client.post("/api/kids/show-and-tell/look-now", headers=headers).json()["accepted"] is True
    deadline = time.monotonic() + 5
    while restarted._voice_activity_lock.locked():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert events.count("capture") == 1
    text = client.get("/api/status").text
    assert "I saw" not in text and restarted._kids_session_id not in text and "permit" not in text


def test_owner_global_camera_revoke_keeps_show_and_tell_off(persisted) -> None:
    _runtime_for, runtime, client, headers, events, path = persisted
    saved = client.post("/api/settings", json={"camera_enabled": False}, headers=headers)
    assert saved.status_code == 200, saved.text
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    assert kids_state(client)["camera_approved"] is False and kids_state(client)["available"] is False
    # The Show and Tell setting cannot widen a camera approval the Owner did not give.
    response = client.post("/api/kids/show-and-tell/setting", json={"enabled": True}, headers=headers)
    assert response.status_code == 200 and response.json()["available"] is False
    assert client.post("/api/kids/show-and-tell/look-now", headers=headers).status_code == 409
    assert json.loads(path.read_text())["camera_enabled"] is False and events.count("capture") == 0


def test_owner_fresh_install_without_camera_approval_stays_disabled(persisted) -> None:
    runtime_for, _runtime, client, _headers, events, path = persisted
    path.unlink()  # no saved settings at all, as on a fresh install
    runtime = runtime_for()
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    assert kids_state(client)["camera_approved"] is False and kids_state(client)["available"] is False
    assert events.count("capture") == 0
