"""Exercise the real owner middleware and MCP transport, with only the speaker hardware faked."""

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair
from test_speaker_volume import service

from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.mcp_server import McpServer, token_digest
from homebody.runtime import HermesVoiceRuntime

TOKEN = "hb_volume-test"


@pytest.fixture
def app(monkeypatch, tmp_path):
    import homebody.main as main

    config = AppConfig(mcp_enabled=True, mcp_token_sha256=token_digest(TOKEN))
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(main, "load_config", lambda: config)
    robot = Homebody()
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), config_loader=lambda: config)
    runtime._control_ready.set()
    runtime._speaker, mixer = service()
    robot._runtime = runtime
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    return robot, client, code, mixer


def paired(app):
    robot, client, code, mixer = app
    assert pair(client, code).status_code == 200
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}
    return robot, client, headers, mixer


def test_volume_is_owner_only_and_csrf_protected(app):
    _, client, _, mixer = app
    assert client.get("/api/audio/volume").status_code == 401
    assert client.post("/api/audio/volume", json={"volume": 30}).status_code == 401
    _, client, headers, _ = paired(app)
    response = client.get("/api/audio/volume")
    assert response.status_code == 200 and response.json()["volume"] == 62
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/audio/volume", json={"volume": 30}, headers={"Origin": ORIGIN}).status_code == 403
    assert (
        client.post(
            "/api/audio/volume", json={"volume": 30}, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    changed = client.post("/api/audio/volume", json={"volume": 30}, headers=headers)
    assert changed.status_code == 200 and changed.json()["volume"] == 30
    assert client.get("/api/audio/volume").json()["volume"] == 30
    assert mixer.writes == [30]
    assert client.post("/api/audio/volume", json={"muted": True}, headers=headers).json()["volume"] == 0
    assert client.post("/api/audio/volume", json={"muted": False}, headers=headers).json()["volume"] == 30


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"volume": True},
        {"volume": "30"},
        {"volume": 30.5},
        {"volume": -1},
        {"volume": 101},
        {"volume": 20, "muted": True},
        {"muted": "yes"},
        {"volume": 10, "wake": True},
    ],
)
def test_volume_schema_rejects_coercion_or_extra_permissions(app, payload):
    _, client, headers, mixer = paired(app)
    assert client.post("/api/audio/volume", json=payload, headers=headers).status_code == 422
    assert mixer.writes == []


@pytest.mark.parametrize("kids", ["_kids_active", "_kids_locked"])
def test_volume_rechecks_kids_state_and_never_mutates(app, kids):
    robot, client, headers, mixer = paired(app)
    setattr(robot._runtime, kids, True)
    assert client.post("/api/audio/volume", json={"volume": 20}, headers=headers).status_code in {409, 423}
    assert client.get("/api/audio/volume").status_code in {409, 423}
    assert mixer.writes == []


def test_volume_false_success_never_returns_200(app):
    _, client, headers, mixer = paired(app)
    mixer.ignore = True
    response = client.post("/api/audio/volume", json={"volume": 20}, headers=headers)
    assert response.status_code == 409 and "not confirmed" in response.text


def rpc(client, name, arguments=None, authenticated=True):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={"Authorization": f"Bearer {TOKEN}"} if authenticated else {},
    )


def test_volume_mcp_schema_and_real_authenticated_transport(app):
    robot, client, _, mixer = app
    schemas = {t["name"]: t for t in McpServer.tools(AppConfig())}
    assert {"get_volume", "set_volume", "set_mute"} <= schemas.keys()
    assert schemas["set_volume"]["inputSchema"]["properties"]["volume"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 100,
    }
    assert schemas["set_mute"]["inputSchema"]["properties"]["muted"] == {"type": "boolean"}
    assert all(
        schemas[name]["inputSchema"]["additionalProperties"] is False
        for name in ("get_volume", "set_volume", "set_mute")
    )
    assert rpc(client, "set_volume", {"volume": 30}, authenticated=False).status_code == 401
    assert rpc(client, "get_volume").json()["result"]["structuredContent"]["volume"] == 62
    result = rpc(client, "set_volume", {"volume": 30}).json()["result"]
    assert result["isError"] is False and result["structuredContent"]["volume"] == 30
    assert rpc(client, "set_mute", {"muted": True}).json()["result"]["structuredContent"]["volume"] == 0
    assert rpc(client, "get_volume").json()["result"]["structuredContent"]["muted"] is True
    assert rpc(client, "set_mute", {"muted": False}).json()["result"]["structuredContent"]["volume"] == 30
    assert mixer.writes == [30, 0, 30]
    assert robot._runtime._effective_power_mode() == "standby"
    assert not robot._runtime._motors_enabled


@pytest.mark.parametrize(
    "name,args",
    [
        ("set_volume", {"volume": True}),
        ("set_volume", {"volume": 101}),
        ("set_volume", {"volume": 20.5}),
        ("set_volume", {"volume": 30, "wake": True}),
        ("set_mute", {"muted": "yes"}),
        ("get_volume", {"extra": True}),
    ],
)
def test_mcp_volume_arguments_enforced_server_side(app, name, args):
    _, client, _, mixer = app
    assert rpc(client, name, args).json()["result"]["isError"] is True
    assert mixer.writes == []


@pytest.mark.parametrize("mode", ["sleep", "meeting", "privacy", "kids"])
def test_mcp_volume_respects_existing_private_and_child_rules(app, mode):
    robot, client, _, mixer = app
    if mode == "privacy":
        robot._runtime._privacy_requested.set()
    elif mode == "kids":
        robot._runtime._kids_active = True
    else:
        robot._runtime._effective_power_mode = lambda: mode
        robot._runtime._status.power_mode = mode
    result = rpc(client, "set_volume", {"volume": 20}).json()["result"]
    assert result["isError"] is True
    assert rpc(client, "set_mute", {"muted": True}).json()["result"]["isError"] is True
    assert mixer.writes == []
