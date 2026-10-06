"""Exercise real owner middleware/CSRF; no legacy header bypass for transcripts."""

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.owner_auth import OwnerStore, default_owner_path
from homebody.runtime import HermesVoiceRuntime

ORIGIN = "https://homebody.example.ts.net"


@pytest.fixture
def workspace_client(monkeypatch):
    monkeypatch.setattr(main_module, "load_config", AppConfig)
    app = Homebody(False)
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), config_loader=AppConfig)
    runtime._publish_remote_agent_session = lambda: None
    app._runtime = runtime
    store = OwnerStore(default_owner_path())
    code = store.provision(ORIGIN)
    client = TestClient(app.settings_app, base_url=ORIGIN)
    return client, runtime, store, code


def pair(client, code):
    assert client.post("/api/owner/pair", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 200
    csrf = client.get("/api/owner/session").json()["csrf"]
    return {"Origin": ORIGIN, "X-Homebody-CSRF": csrf}


def test_owner_workspace_reads_writes_and_public_redaction(workspace_client):
    client, runtime, _store, code = workspace_client
    for path in ["/api/agent/workspace", "/api/agent/workspace/start", "/api/agent/workspace/clear"]:
        response = client.get(path) if path.endswith("workspace") else client.post(path)
        assert response.status_code == 401
    assert client.get("/api/agent/workspace", headers={"X-Reachy-Adult-UI": "unlocked"}).status_code == 401
    headers = pair(client, code)
    assert client.post("/api/agent/workspace/start").status_code == 403
    response = client.post("/api/agent/workspace/start", headers=headers)
    assert response.status_code == 200 and response.json()["enabled"]
    assert response.headers["cache-control"] == "no-store"
    runtime._workspace_event(runtime._workspace_lease(), "user", "private marker")
    assert "private marker" in client.get("/api/agent/workspace").text
    assert "private marker" not in client.get("/api/status").text
    assert client.post("/api/agent/workspace/clear", headers=headers).json()["events"] == []


def test_owner_workspace_kids_lock_and_revoked_owner(workspace_client):
    client, runtime, store, code = workspace_client
    headers = pair(client, code)
    client.post("/api/agent/workspace/start", headers=headers)
    runtime._workspace_event(runtime._workspace_lease(), "user", "adult secret")
    runtime.cancel_agent_work("kids_mode")
    runtime._kids_locked = True
    response = client.get("/api/agent/workspace")
    assert response.status_code == 423
    assert "adult secret" not in response.text
    runtime._kids_locked = False
    assert client.get("/api/agent/workspace").json()["events"] == []
    with store.connect() as db:
        db.execute("DELETE FROM devices")
    assert client.get("/api/agent/workspace").status_code == 401


@pytest.mark.parametrize(
    "action,body",
    [
        ("read", {}),
        ("show", {}),
        ("bind", {"target_id": "registered"}),
        ("prepare", {"text": "Do work"}),
        ("approve", {"approval_id": "exact"}),
        ("cancel", {}),
    ],
)
def test_owner_native_workspace_requires_pairing_and_csrf(workspace_client, action, body):
    client, runtime, _store, code = workspace_client
    path = f"/api/agent/workspace/native/{action}"
    assert client.post(path, json=body, headers={"X-Reachy-Adult-UI": "unlocked"}).status_code == 401
    headers = pair(client, code)
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers=headers).status_code == 423
    client.post("/api/agent/workspace/start", headers=headers)
    assert client.post(path, json=body, headers=headers).status_code == 423  # conversation profile is not Agent
    assert "registered" not in client.get("/api/status").text


def test_owner_native_workspace_proxy_current_generation_and_privacy(workspace_client, monkeypatch):
    client, runtime, store, code = workspace_client
    calls = []

    class FakeBridge:
        def __init__(self, _config):
            pass

        def establish_agent_session(self, context, **options):
            calls.append(("session", context.session_generation, options))

        def native_workspace_action(self, action, context, **fields):
            calls.append((action, context.session_generation, fields))
            return {"targets": [], "messages": [{"role": "assistant", "text": "private native history"}]}

        def close(self):
            pass

    monkeypatch.setattr(main_module, "HermesBridgeClient", FakeBridge)
    headers = pair(client, code)
    runtime.set_capability_profile("agent", adult_ui_unlocked=True)
    client.post("/api/agent/workspace/start", headers=headers)
    response = client.post("/api/agent/workspace/native/read", json={}, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "private native history" in response.text
    assert "private native history" not in client.get("/api/status").text
    assert calls[-1][1] == runtime._agent_session_generation
    assert (
        client.post(
            "/api/agent/workspace/native/prepare", json={"text": "x", "root": "/"}, headers=headers
        ).status_code
        == 422
    )
    runtime.cancel_agent_work("privacy")
    assert client.post("/api/agent/workspace/native/read", json={}, headers=headers).status_code == 423
    with store.connect() as db:
        db.execute("DELETE FROM devices")
    assert client.post("/api/agent/workspace/native/read", json={}, headers=headers).status_code == 401
