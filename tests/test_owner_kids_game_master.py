"""Owner boundary for the Kids game-master caregiver control (real OwnerBoundary middleware)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody


@pytest.fixture
def application(monkeypatch, tmp_path):
    import homebody.main as main

    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: AppConfig())
    robot = Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    calls: list[tuple[str, bool]] = []
    robot._runtime = SimpleNamespace(
        kids_controls_locked=False,
        kids_host_select=lambda activity, camera_consent=False: calls.append((activity, camera_consent))
        or {"active": True},
    )
    return robot, TestClient(robot.settings_app, base_url=ORIGIN), code, calls


def owner_headers(client) -> dict[str, str]:
    session = client.get("/api/owner/session").json()
    return {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}


def test_owner_host_select_requires_pairing_and_stays_available_while_locked(application) -> None:
    robot, client, code, calls = application
    robot._runtime.kids_controls_locked = True
    assert client.post("/api/kids/host/select", json={"activity": "story"}).status_code == 401
    assert calls == []
    robot._runtime.kids_controls_locked = False
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    robot._runtime.kids_controls_locked = True
    response = client.post(
        "/api/kids/host/select", json={"activity": "ispy", "camera_consent": True}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert calls == [("ispy", True)]
    assert client.post("/api/kids/host/select", json={"activity": "hermes"}, headers=headers).status_code == 422
    assert client.post(
        "/api/kids/host/select", json={"activity": "story", "tools": True}, headers=headers,
    ).status_code == 422
    # CSRF and same-origin are still enforced for the caregiver control.
    assert client.post(
        "/api/kids/host/select", json={"activity": "story"}, headers={"Origin": ORIGIN},
    ).status_code == 403
    # Other parent controls stay locked during the child session.
    assert client.post("/api/settings", json={}, headers=headers).status_code == 423
    assert calls == [("ispy", True)]


def test_owner_loss_blocks_host_select_after_logout(application) -> None:
    robot, client, code, calls = application
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    assert client.post("/api/owner/logout", headers=headers).status_code in {200, 204}
    robot._runtime.kids_controls_locked = True
    assert client.post("/api/kids/host/select", json={"activity": "story"}, headers=headers).status_code == 401
    assert calls == []
