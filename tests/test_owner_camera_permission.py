"""Actual Owner/CSRF camera access: sign-in is not activation."""

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody


@pytest.fixture
def owner_camera(monkeypatch, tmp_path):
    import homebody.main as main

    configs = [AppConfig(api_key="fixture-only", camera_enabled=False)]
    saves = []
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(main, "load_config", lambda: configs[0])

    def save(config):
        configs[0] = config
        saves.append(config)
        return tmp_path / "not-written.json"

    monkeypatch.setattr(main, "save_config", save)
    robot = Homebody()
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    return robot, client, code, configs, saves


def test_owner_sign_in_does_not_change_camera_permission_or_start_runtime(owner_camera):
    robot, client, code, configs, saves = owner_camera
    assert pair(client, code).status_code == 200
    assert client.get("/api/owner/session").json()["owner"]
    assert not configs[0].camera_enabled and not saves
    assert robot._runtime is None
    assert not configs[0].camera_feed_enabled and not configs[0].face_tracking_enabled


def test_only_explicit_owner_setting_enables_ai_camera_without_starting_any_media(owner_camera):
    robot, client, code, configs, saves = owner_camera
    refused = client.post("/api/settings", json={"camera_enabled": True}, headers={"Origin": ORIGIN})
    assert refused.status_code in {401, 403} and not configs[0].camera_enabled and not saves
    assert pair(client, code).status_code == 200
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}
    assert client.post("/api/settings", json={"camera_enabled": True}, headers=headers).status_code == 200
    assert configs[0].camera_enabled and len(saves) == 1
    assert not configs[0].camera_feed_enabled and not configs[0].face_tracking_enabled
    assert robot._runtime is None
