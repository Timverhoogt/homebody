import threading
import time

from homebody.agent_connection import AgentConnectionMonitor, probe_connection
from homebody.config import AppConfig


def wait_for(monitor, config):
    for _ in range(100):
        result = monitor.snapshot(config)
        if result["state"] != "checking":
            return result
        time.sleep(0.005)
    raise AssertionError("Probe did not finish")


def test_configuration_and_failure_do_not_report_connected():
    config = AppConfig()
    monitor = AgentConnectionMonitor(probe=lambda _: {"state": "unavailable", "detail": "Bridge unavailable."})
    assert monitor.snapshot(config)["state"] == "unconfigured"
    config = AppConfig(bridge_url="http://bridge.test", api_key="test-only")
    assert wait_for(monitor, config)["state"] == "unavailable"


def test_cache_freshness_and_single_flight():
    now = [100.0]
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def probe(config):
        calls.append(config.model)
        entered.set()
        release.wait(2)
        return {"state": "connected", "detail": "Authenticated agent ready."}

    monitor = AgentConnectionMonitor(probe=probe, clock=lambda: now[0])
    config = AppConfig(bridge_url="http://bridge.test", api_key="test-only")
    assert monitor.snapshot(config)["state"] == "checking"
    assert entered.wait(1)
    for _ in range(20):
        assert monitor.snapshot(config)["state"] == "checking"
    assert len(calls) == 1
    release.set()
    assert wait_for(monitor, config)["state"] == "connected"
    now[0] += 26
    result = monitor.snapshot(config)
    assert result["state"] == "checking"


def test_changed_credentials_discard_old_probe():
    entered, release = threading.Event(), threading.Event()

    def probe(config):
        entered.set()
        release.wait(2)
        return (
            {"state": "connected", "detail": "Ready."}
            if config.api_key == "old"
            else {"state": "unavailable", "detail": "Authentication failed."}
        )

    monitor = AgentConnectionMonitor(probe=probe)
    old = AppConfig(bridge_url="http://bridge.test", api_key="old")
    new = AppConfig(bridge_url="http://bridge.test", api_key="new")
    monitor.snapshot(old)
    assert entered.wait(1)
    assert monitor.snapshot(new)["state"] == "checking"
    release.set()
    assert wait_for(monitor, new)["state"] == "unavailable"


def test_status_route_suppresses_adult_connection_under_kids_lock(monkeypatch):
    from test_agent_setup import build

    config = AppConfig(bridge_url="http://private.test", api_key="never-expose-this")
    app, client, _ = build(monkeypatch, config)
    calls = []
    app._agent_connection = AgentConnectionMonitor(
        probe=lambda _: calls.append(1) or {"state": "connected", "detail": "Ready."}
    )
    app._runtime.status = lambda: {"power_mode": "sleep", "kids_mode": {"locked": True}}
    body = client.get("/api/status").json()
    assert body["agent_connection"]["state"] == "unknown"
    assert body["agent_connection"]["label"] == "Agent"
    assert "never-expose-this" not in str(body) and "private.test" not in str(body)
    assert calls == []
    assert client.post("/api/agent-connection/retry").status_code == 423


def test_selected_backend_and_authentication(monkeypatch):
    class Client:
        def __init__(self, config):
            self.config = config

        def health(self):
            return {
                "status": "ok",
                "agent_backends": [{"name": "hermes", "ok": True}, {"name": "openclaw", "ok": False}],
            }

        def models(self, *, timeout):
            if self.config.api_key == "wrong":
                raise RuntimeError("SECRET and http://private:9999 must not leak")
            return [{"id": self.config.model}]

        def close(self):
            pass

    monkeypatch.setattr("homebody.agent_connection.HermesBridgeClient", Client)
    config = AppConfig(bridge_url="http://bridge.test", api_key="good")
    assert probe_connection(config)["state"] == "connected"
    assert (
        probe_connection(AppConfig(bridge_url=config.bridge_url, api_key="good", model="openclaw/reachy"))["state"]
        == "unavailable"
    )
    result = probe_connection(AppConfig(bridge_url=config.bridge_url, api_key="wrong"))
    assert result["state"] == "unavailable"
    assert "SECRET" not in str(result) and "private" not in str(result)
