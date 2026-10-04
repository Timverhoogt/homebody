"""Exercise the real SDK settings application boundary, not mocked authorization."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.owner_auth import MACHINE_ROUTES


@pytest.fixture
def application(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig())
    robot = Homebody()
    store = robot.settings_app.state.owner_store
    code = store.provision(ORIGIN)
    return robot, TestClient(robot.settings_app, base_url=ORIGIN), code


def test_every_registered_private_route_rejects_guest(application):
    robot, client, _ = application
    checked = []
    for route in robot.settings_app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", ()):
            if not path.startswith("/api/") or path in {
                "/api/owner/session",
                "/api/owner/pair",
                "/api/status",
                "/api/agent-setup/status",
            }:
                continue
            if (method, path) in MACHINE_ROUTES:
                continue
            response = client.request(method, path, json={}, headers={"X-Reachy-Adult-UI": "unlocked"})
            assert response.status_code == 401, (method, path, response.text)
            checked.append(path)
    assert len(checked) >= 60
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_machine_exceptions_keep_their_own_auth(application, monkeypatch):
    import homebody.main as main

    robot, client, _ = application
    monkeypatch.setattr(main, "load_config", lambda: AppConfig(api_key="machine-test-key"))
    for path, payload in [
        ("/api/presence/signal", {"source": "trusted_sensor", "occupied": True}),
        (
            "/api/initiative/offers",
            {
                "source": "timer",
                "topic": "test",
                "confidence": 1,
                "fingerprint": "test",
                "text": "test",
                "accepted_text": "test",
            },
        ),
        ("/api/agent/reminder-delivery", {"item_id": "timer-" + "a" * 16, "text": "test"}),
        ("/api/camera/snapshot", {"confirm": "camera"}),
    ]:
        assert client.post(path, json=payload).status_code == 401
        # Valid machine key reaches the runtime-not-started guard, not Owner pairing.
        response = client.post(path, json=payload, headers={"Authorization": "Bearer machine-test-key"})
        assert response.status_code in {409, 503}, (path, response.text)
    assert (
        client.post("/api/settings", json={}, headers={"Authorization": "Bearer machine-test-key"}).status_code == 401
    )
    assert client.post("/api/agent-setup/pair", json={"code": "invalid"}).status_code in {400, 401, 403}
    assert client.post("/mcp", json={}).status_code in {401, 404, 503}


def test_owner_settings_without_old_key_and_no_runtime_side_effect(application, monkeypatch):
    import homebody.main as main

    robot, client, code = application
    config = AppConfig(api_key="existing-secret")
    saved = []
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(main, "save_config", lambda value: saved.append(value))
    assert pair(client, code).status_code == 200
    session = client.get("/api/owner/session").json()
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}
    response = client.post(
        "/api/settings", json={"bridge_url": "http://new-agent:8080", "camera_controls_enabled": True}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert saved[0].camera_controls_enabled is True
    assert "existing-secret" not in response.text
    assert robot._runtime is None
    assert client.post("/api/agent-setup/start", json={"backend": "hermes"}, headers=headers).status_code == 200
    # Paired owner no longer needs cosmetic adult headers, but actual runtime readiness remains mandatory.
    assert client.post("/api/camera-control/session", headers=headers).status_code == 409


def test_agent_checks_setup_status_without_owner_pairing(application):
    """The agent's machine is never Owner-paired; it sees the setup state but not the bridge or errors."""
    robot, client, code = application
    robot._agent_setup.start("hermes", mcp=True)
    robot._agent_setup.record_error("bridge unreachable at http://10.0.0.5:8643")
    # The agent may use the robot's LAN or tailnet address, off the configured origin.
    guest = TestClient(robot.settings_app, base_url="http://100.64.27.89:8042")
    status = guest.get("/api/agent-setup/status")
    assert status.status_code == 200, status.text
    assert status.json() == {
        "state": "waiting",
        "backend": "hermes",
        "agent": "Hermes Agent",
        "mcp": True,
        "expires_in": status.json()["expires_in"],
    }
    assert pair(client, code).status_code == 200
    assert "10.0.0.5" in client.get("/api/agent-setup/status").json()["last_error"]


def test_kids_cannot_be_bypassed_and_stop_stays_accessible(application):
    robot, client, code = application
    pair(client, code)
    csrf = client.get("/api/owner/session").json()["csrf"]
    robot._runtime = SimpleNamespace(
        kids_controls_locked=True, stop_manual_robot_action=lambda: {"robot_stopped": True}
    )
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}
    assert client.post("/api/settings", json={}, headers=headers).status_code == 423
    assert client.post("/api/power", json={"mode": "awake"}, headers=headers).status_code == 423
    assert client.post("/api/robot/stop", headers=headers).json()["robot_stopped"] is True
    assert client.get("/api/owner/session").json()["owner"] is True
    assert client.post("/api/owner/logout", headers=headers).status_code == 200


def test_websocket_guest_foreign_origin_and_not_ready(application):
    _, client, code = application
    url = ORIGIN.replace("https:", "wss:") + "/api/camera/signaling"
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(url, headers={"Origin": ORIGIN}):
        pass
    assert exc.value.code == 4401
    pair(client, code)
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(url, headers={"Origin": "https://evil.test"}),
    ):
        pass
    assert exc.value.code == 4403
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(url, headers={"Origin": ORIGIN}):
        pass
    assert exc.value.code == 4403
