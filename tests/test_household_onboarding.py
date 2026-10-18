"""Offline onboarding contracts: no live endpoints, credentials, providers or devices."""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_everyday_tools import make_tools
from test_everyday_workspace_bridge import ctx
from test_mcp_server import build, enabled_config
from test_standalone_household_tools import standalone

from companion.household_mcp import household_mcp
from companion.household_smoke import NoRedirect, check, endpoint, main
from homebody import __version__
from homebody.agent_setup import BACKENDS, bridge_manifest, render_guide

ROOT = Path(__file__).resolve().parents[1]


class InMemoryHTTP:
    """Exercise the actual MCP handler in-process, not a network server."""

    def __init__(self, bridge, runner):
        self.bridge, self.runner = bridge, runner
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 20
        body = json.loads(request.data)
        self.requests.append((request, body))

        async def payload():
            return body

        response = self.runner.run(household_mcp(self.bridge, SimpleNamespace(
            path="/mcp", headers={k.title(): v for k, v in request.header_items()}, json=payload)))
        data = response.body or b""
        return SimpleNamespaceResponse(response.status, data)


class SimpleNamespaceResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.body[:limit]


def test_smoke_uses_actual_mcp_handler_clock_and_stale_refusal(monkeypatch):
    bridge = standalone(monkeypatch)
    tools = make_tools("http://unused.invalid")
    bridge.agent_broker, bridge.everyday = tools.broker, tools
    bridge.http = object()  # Clock has no HTTP calls; any accidental HTTP fails.
    context = ctx(1, "conversation")
    scope = {"connected": True, "device_id": "device", "homebody_context": context, "maximum_seconds": 300}
    bridge.robot_api_key = "synthetic-robot-only"
    bridge.household_robots["device"] = SimpleNamespace(
        context=None, current=lambda: True, transport=SimpleNamespace(names=[]), tasks=set())
    from companion.reachy_agent_broker import BrokerContext
    bridge.household_robots["device"].context = BrokerContext.parse(context)
    with asyncio.Runner() as runner:
        runner.run(bridge.agent_broker.establish_session("device", context))
        http = InMemoryHTTP(bridge, runner)
        assert check("http://127.0.0.1:8644", "synthetic-household", None, opener=http) == {
            "health": "ok", "scoped_tools": "not_checked"}
        report = check("http://127.0.0.1:8644", "synthetic-household", scope, opener=http)
        assert report["readback_ok"] and report["tool_count"] > 0
        methods = [body["method"] for _, body in http.requests]
        assert methods == ["initialize", "notifications/initialized", "ping"] * 2 + ["tools/list", "tools/call"]
        for request, body in http.requests:
            assert request.full_url == "http://127.0.0.1:8644/mcp"
            if body["method"].startswith("tools/"):
                assert body["params"]["_meta"] == {"homebody_context": context}
                assert request.get_header("X-reachy-device-id") == "device"
            if body["method"] == "tools/call":
                assert body["params"]["name"] == "get_current_time"
                assert body["params"]["arguments"] == {}
        assert "synthetic" not in json.dumps(report) and "homebody_context" not in report
        runner.run(bridge.agent_broker.establish_session("device", ctx(2, "conversation")))
        with pytest.raises(ValueError, match="refused"):
            check("http://127.0.0.1:8644", "synthetic-household", scope, opener=http)
        runner.run(bridge.agent_broker.actions.shutdown())


@pytest.mark.parametrize("url", ["http://robot.local:8644", "http://192.0.2.1", "file:///tmp/x",
                                  "https://user:secret@example.invalid", "https://example.invalid/mcp",
                                  "https://example.invalid/?token=x", "https://example.invalid/#x"])
def test_smoke_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        endpoint(url)


def test_smoke_allows_tls_and_loopback_and_refuses_redirects():
    assert endpoint("https://household.example.invalid") == "https://household.example.invalid/mcp"
    assert endpoint("http://[::1]:8644/") == "http://[::1]:8644/mcp"
    with pytest.raises(ValueError, match="Redirect refused"):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")


@pytest.mark.parametrize("scope", [[], {}, {"connected": False}, {"connected": True, "device_id": "device"}])
def test_smoke_rejects_missing_scope_without_network(scope):
    with pytest.raises(ValueError, match="Owner connect"):
        check("http://127.0.0.1:8644", "synthetic", scope, opener=object())


def test_cli_failure_is_redacted(monkeypatch, capsys):
    monkeypatch.setenv("HOMEBODY_HOUSEHOLD_TOKEN", "synthetic-secret")
    with pytest.raises(SystemExit) as exc:
        main(["--url", "https://user:synthetic-secret@example.invalid"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out and "synthetic-secret" not in captured.err


@pytest.mark.parametrize("reply", [[], {"id": 1, "result": {}},
                                   {"jsonrpc": "2.0", "id": 2, "result": {}},
                                   {"jsonrpc": "2.0", "id": 1, "error": {"message": "private"}},
                                   {"jsonrpc": "2.0", "id": 1, "result": []}])
def test_malformed_or_failed_wire_reply_never_passes(reply):
    opener = SimpleNamespace(open=lambda *a, **kw: SimpleNamespaceResponse(200, json.dumps(reply).encode()))
    with pytest.raises(ValueError, match="refused"):
        check("http://127.0.0.1:8644", "synthetic", None, opener=opener)


def test_guide_is_served_linked_and_not_a_pairing_backend(monkeypatch):
    app, client, _ = build(monkeypatch, enabled_config())
    response = client.get("/agent-setup/household.md")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Copyable AI-agent instructions" in response.text
    assert "household" not in BACKENDS  # Do not falsely add a pairing wizard backend.
    assert client.get("/agent-setup/unknown.md").status_code == 404
    for backend in BACKENDS:
        assert "https://robot.example.invalid/agent-setup/household.md" in render_guide(
            backend, "https://robot.example.invalid")
    manifest = {item["name"] for item in bridge_manifest()}
    assert {"household_smoke.py", "household_server.py"} <= manifest
    for file in ("README.md", "docs/agent-setup.md", "docs/marketplace-quickstart.md",
                 "docs/standalone-household-tools.md"):
        assert "agent_guides/household.md" in (ROOT / file).read_text()


def test_guide_checklist_and_critical_boundaries():
    guide = (ROOT / "homebody/agent_guides/household.md").read_text()
    checklist = json.loads(re.search(r"```json\n(.*?)\n```", guide, re.S)[1])
    assert checklist and set(checklist.values()) == {"not_attempted"}
    for term in ("Released support since 0.5.35", "Independent household routing", "300 seconds",
                 "X-Homebody-CSRF", "homebody_context",
                 "HOMEBODY_HOUSEHOLD_ROBOT_TOKEN", "HOMEBODY_HOUSEHOLD_TOKEN", "HTTPS/WSS",
                 "No autonomous", "rollback", "GPT-Live", "STT/TTS", "Hermes installation"):
        assert term.lower() in guide.lower()


def test_wheel_includes_exact_runbook_and_smoke(tmp_path):
    wheel = ROOT / f"dist/reachy_mini_homebody-{__version__}-py3-none-any.whl"
    with ZipFile(wheel) as archive:
        for dest, source in (("homebody/agent_guides/household.md", "homebody/agent_guides/household.md"),
                             ("homebody/agent_setup.py", "homebody/agent_setup.py"),
                             ("homebody/bridge/household_smoke.py", "companion/household_smoke.py")):
            assert archive.read(dest) == (ROOT / source).read_bytes()
        archive.extractall(tmp_path)
    script = tmp_path / "homebody/bridge/household_smoke.py"
    result = subprocess.run([sys.executable, "-I", str(script), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "--scope" in result.stdout
