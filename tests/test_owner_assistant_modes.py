"""Real owner middleware for Everyday/Workspace routes; runtime authority with a fake bridge client."""

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_owner_auth import ORIGIN, pair

from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.runtime import HermesVoiceRuntime


@pytest.fixture
def app(monkeypatch, tmp_path):
    import homebody.main as main

    config = AppConfig(api_key="bridge-key")
    monkeypatch.setattr(main, "default_config_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(main, "load_config", lambda: config)
    robot = Homebody()
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), config_loader=lambda: config)
    published = []

    class Bridge:
        def __init__(self, _config=None):
            pass

        def establish_agent_session(self, context, **flags):
            published.append((context.capability_profile, flags))

        def everyday_capabilities(self):
            return {"available": True, "text_provider": {"model": "gpt-4.1-mini"}}

        def native_badge(self):
            return {"status": "running", "running": True, "ready_to_test": False, "configured": True}

        def close(self):
            pass

    runtime._new_bridge_client = lambda _config: Bridge()
    runtime._publish_remote_agent_session = lambda: HermesVoiceRuntime._publish_remote_agent_session(runtime)
    monkeypatch.setattr(main, "HermesBridgeClient", Bridge)
    monkeypatch.setattr("homebody.agent_session.threading.Thread",
                        lambda target, args=(), **_: SimpleNamespace(start=lambda: target(*args)))
    robot._runtime = runtime
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    return robot, client, code, runtime, published


def owner_headers(client, code):
    assert pair(client, code).status_code == 200
    return {"Origin": ORIGIN, "X-Homebody-CSRF": client.get("/api/owner/session").json()["csrf"]}


def test_new_routes_require_a_paired_owner(app):
    _, client, _, _, _ = app
    for path in ("/api/voice/timings", "/api/everyday/capabilities", "/api/agent/workspace/native-badge"):
        assert client.get(path).status_code == 401
    assert client.post("/api/voice/stop").status_code == 401


def test_timings_are_owner_only_no_store_and_text_free(app):
    _, client, code, runtime, _ = app
    headers = owner_headers(client, code)
    runtime._voice_timings.begin("live", "everyday", warm=False)
    runtime._voice_timings.mark("first_provider_audio")
    runtime._voice_timings.end()
    response = client.get("/api/voice/timings", headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    turn = response.json()["turns"][0]
    assert set(turn) == {"engine", "mode", "warm", "marks"} and turn["engine"] == "live"
    badge = client.get("/api/agent/workspace/native-badge", headers=headers).json()
    assert badge == {"status": "running", "running": True, "ready_to_test": False, "configured": True}


def test_switch_to_everyday_parks_and_stop_speaking_preserves_native_work(app):
    _, client, code, runtime, published = app
    headers = owner_headers(client, code)
    runtime._effective_power_mode = lambda: "awake"
    assert client.post("/api/agent/profile", json={"profile": "agent"}, headers=headers).status_code == 200
    runtime._voice_workspace.start()
    runtime._voice_workspace.append(runtime._voice_workspace.lease(), "user", "keep me visible")
    stopped = client.post("/api/voice/stop", headers=headers)
    assert stopped.status_code == 200
    assert published[-1] == ("agent", {"preserve_native": True})  # Stop speaking keeps native work
    assert runtime._voice_workspace.snapshot()["events"], "Stop speaking must not wipe the conversation view"
    switched = client.post("/api/agent/profile", json={"profile": "conversation", "preserve_work": True},
                           headers=headers)
    assert switched.status_code == 200
    assert published[-1] == ("conversation", {"park_native": True})
    runtime.cancel_agent_work("stopped")  # Stop all Agent work still revokes
    assert published[-1] == ("conversation", {})
    assert json.dumps(published).count("park_native") == 1
