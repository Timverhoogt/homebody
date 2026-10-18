"""One-time household setup through the real Owner boundary (pairing, same-origin, CSRF) and saved settings.

Pairing alone is not consent; only an explicit acknowledged POST applies choices, and applying never
captures, listens or touches the voice runtime. Revocation fails closed in RAM first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair
from test_owner_show_and_tell import owner_headers

from homebody.config import HOUSEHOLD_SETUP_VERSION, AppConfig, load_config, save_config
from homebody.everyday_runtime import natural_camera_requests


@pytest.fixture
def owner(monkeypatch, tmp_path):
    import homebody.main as main

    path = tmp_path / "config.json"
    monkeypatch.setenv("HOMEBODY_CONFIG", str(path))
    monkeypatch.setattr(main, "default_config_path", lambda: path)
    save_config(AppConfig(api_key="test-key", camera_enabled=False))
    robot = main.Homebody()
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    calls: list[str] = []
    robot._runtime = SimpleNamespace(
        kids_controls_locked=False,
        revoke_everyday_camera=lambda: calls.append("revoke_camera"),
        kids_revoke_preapproved_camera=lambda: calls.append("revoke_kids"),
        look_at_this=lambda *a, **k: pytest.fail("household setup never captures"),
    )
    with TestClient(robot.settings_app, base_url=ORIGIN) as client:
        yield robot, client, code, path, calls


CHOICES = {
    "disclosure_version": HOUSEHOLD_SETUP_VERSION,
    "acknowledged": True,
    "camera_on_request": True,
    "following_and_movement": True,
    "voice_power_controls": True,
    "kids_ispy_camera": False,
}


def test_household_setup_requires_owner_csrf_and_explicit_acknowledgement(owner):
    robot, client, code, path, calls = owner
    before = path.read_text()
    assert client.post("/api/household/setup", json=CHOICES).status_code == 401
    assert client.get("/api/household/setup").status_code == 401
    assert pair(client, code).status_code == 200
    # Pairing alone is not consent: nothing changed and the status says so.
    status = client.get("/api/household/setup").json()
    assert status["completed"] is False and status["choices"]["camera_on_request"] is False
    assert "purchases" in status["disclosure"]["excluded"] and path.read_text() == before
    headers = owner_headers(client)
    assert client.post("/api/household/setup", json=CHOICES, headers={"Origin": ORIGIN}).status_code == 403
    for body in ({**CHOICES, "acknowledged": False}, {**CHOICES, "disclosure_version": 0}):
        assert client.post("/api/household/setup", json=body, headers=headers).status_code == 400
    for body in ({**CHOICES, "purchases": True}, {**CHOICES, "camera_on_request": "maybe"}):
        assert client.post("/api/household/setup", json=body, headers=headers).status_code == 422
    assert path.read_text() == before
    robot._runtime.kids_controls_locked = True
    assert client.post("/api/household/setup", json=CHOICES, headers=headers).status_code == 423
    robot._runtime.kids_controls_locked = False

    applied = client.post("/api/household/setup", json=CHOICES, headers=headers)
    assert applied.status_code == 200 and applied.json()["completed"] is True
    saved = load_config(path)
    assert saved.camera_enabled and saved.camera_natural_requests_enabled and natural_camera_requests(saved)
    assert saved.face_tracking_enabled and saved.robot_tools_enabled and saved.power_tools_enabled
    assert saved.kids_ispy_camera_preapproved is False and saved.api_key == "test-key"
    assert calls == []  # enabling never captures, listens or touches the runtime

    revoked = client.post("/api/household/revoke", headers=headers)
    assert revoked.status_code == 200 and revoked.json()["completed"] is False
    after = load_config(path)
    assert not natural_camera_requests(after) and after.kids_ispy_camera_preapproved is False
    assert after.camera_enabled is True  # the general Camera access toggle keeps its own value
    assert calls == ["revoke_camera", "revoke_kids"]


def test_household_tools_connection_owner_boundary_and_saved_destination(owner, monkeypatch):
    import asyncio

    from test_everyday_workspace_bridge import ctx

    from companion.reachy_agent_broker import BrokerContext
    from homebody import household_client

    robot, client, code, path, calls = owner
    before = path.read_text()
    seen = []
    robot._runtime.agent_broker_context = lambda **kw: BrokerContext.parse(ctx(1, "conversation"))

    async def fake_callbacks(runtime, **kwargs):
        seen.append((runtime, kwargs))
        kwargs["ready"].set()
        await asyncio.Future()

    monkeypatch.setattr(household_client, "household_callbacks", fake_callbacks)
    for action in ("connect", "disconnect"):
        assert client.post("/api/household/tools/" + action).status_code == 401
    assert pair(client, code).status_code == 200
    assert client.post("/api/household/tools/connect", headers={"Origin": ORIGIN}).status_code == 403
    response = client.post("/api/household/tools/connect", headers=owner_headers(client),
                           json={"bridge_url": "https://untrusted.invalid", "context": {"kids_mode_active": False}})
    assert response.status_code == 200
    assert response.json()["connected"]
    assert seen[0][0] is robot._runtime
    assert seen[0][1]["bridge_url"] == load_config(path).bridge_url
    assert seen[0][1]["robot_token"] == "test-key"
    assert "test-key" not in response.text
    assert path.read_text() == before and calls == []
    assert client.post("/api/household/tools/disconnect", headers=owner_headers(client)).json() == {"connected": False}


def test_kids_preapproval_needs_the_camera_choice(owner):
    _robot, client, code, path, _calls = owner
    assert pair(client, code).status_code == 200
    headers = owner_headers(client)
    body = {**CHOICES, "camera_on_request": False, "kids_ispy_camera": True}
    assert client.post("/api/household/setup", json=body, headers=headers).status_code == 200
    saved = load_config(path)
    assert saved.camera_enabled is False and saved.kids_ispy_camera_preapproved is False
