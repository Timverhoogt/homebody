from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import homebody.agent_setup as agent_setup_mod
import homebody.main as main_module
from homebody.agent_setup import (
    CODE_ATTEMPTS,
    SETUP_SECONDS,
    AgentSetup,
    SetupError,
    bridge_manifest,
    probe_bridge,
    render_guide,
    robot_url,
    setup_message,
)
from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.mcp_server import token_matches

ROOT = Path(__file__).resolve().parents[1]
KEY = "k" * 64
BRIDGE = "http://192.168.1.20:8643"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def healthy(backend: str = "hermes", *, ok: bool = True, error: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"name": backend, "label": backend, "ok": ok}
    if error:
        entry["error"] = error
    return {"status": "ok" if ok else "degraded", "agent_backends": [entry], "realtime_available": True}


def fake_bridge(health: dict[str, Any] | None = None, *, key: str = KEY) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=health or healthy())
        if request.url.path == "/v1/models":
            if request.headers.get("authorization") != f"Bearer {key}":
                return httpx.Response(401, json={"error": "Invalid API key"})
            return httpx.Response(200, json={"data": [{"id": "hermes-agent"}]})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


# -- the bridge bundle and guides --------------------------------------------------------------------


def test_manifest_lists_every_bridge_file_with_its_checksum() -> None:
    manifest = {item["name"]: item["sha256"] for item in bridge_manifest()}

    assert "hermes_reachy_bridge.py" in manifest and "agent_backends.py" in manifest
    assert "__init__.py" not in manifest
    for name, digest in manifest.items():
        assert hashlib.sha256((ROOT / "companion" / name).read_bytes()).hexdigest() == digest
    # Every module the bridge imports ships with it.
    source = (ROOT / "companion" / "hermes_reachy_bridge.py").read_text(encoding="utf-8")
    for module in ("agent_backends", "llm_providers", "reachy_agent_broker", "reachy_agent_runs", "warm_agents"):
        assert f"from {module} import" in source and f"{module}.py" in manifest


def test_bridge_files_are_allowlisted() -> None:
    assert agent_setup_mod.bridge_file("agent_backends.py").name == "agent_backends.py"
    for name in ("../homebody/main.py", "__init__.py", "README.md", ".env", "x.py/../y.py"):
        with pytest.raises(FileNotFoundError):
            agent_setup_mod.bridge_file(name)


def test_the_package_ships_the_bridge_and_guides() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"homebody.bridge" = "companion"' in pyproject and '"agent_guides/*.md"' in pyproject


@pytest.mark.parametrize("backend", ["hermes", "openclaw"])
def test_guides_are_filled_in_for_this_robot(backend: str) -> None:
    guide = render_guide(backend, "http://192.168.1.50:8042")

    assert "{{" not in guide
    assert "http://192.168.1.50:8042/api/agent-setup/pair" in guide
    assert f'"backend\\": \\"{backend}\\"' in guide
    for item in bridge_manifest():
        assert item["sha256"] in guide
    assert "Only send keys to" in guide and "wait for their OK" in guide


def test_hermes_guide_restricts_the_profile_the_bridge_checks() -> None:
    guide = render_guide("hermes", "http://r:8042")
    bridge = (ROOT / "companion" / "hermes_reachy_bridge.py").read_text(encoding="utf-8")
    # Every toolset the guide disables holds tools the bridge refuses.
    for toolset in ("terminal", "file", "code_execution", "delegation", "cronjob", "skills", "computer_use"):
        assert toolset in guide
    for tool in ("delegate_task", "cronjob", "skill_manage", "computer_use"):
        assert f'"{tool}"' in bridge
    assert "--profile reachy" in guide and "API_SERVER_KEY" in guide


def test_openclaw_guide_uses_the_minimal_profile() -> None:
    guide = render_guide("openclaw", "http://r:8042")
    assert '"profile":"minimal","deny":["gateway","presence","session_status"]' in guide
    assert "--agent-backends openclaw" in guide


def test_setup_message_carries_no_secret_and_names_the_guide() -> None:
    message = setup_message("openclaw", "http://192.168.1.50:8042", "ABCD-EFGH", mcp=True)
    assert "http://192.168.1.50:8042/agent-setup/openclaw.md" in message and "ABCD-EFGH" in message
    assert "MCP" in message and "wait for my OK" in message


def test_robot_url_replaces_loopback_with_the_lan_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_setup_mod, "lan_address", lambda: "192.168.1.50")
    assert robot_url("reachy-mini.local:8042") == "http://reachy-mini.local:8042"
    assert robot_url("127.0.0.1:8042") == "http://192.168.1.50:8042"
    assert robot_url("localhost") == "http://192.168.1.50:8042"


# -- codes ---------------------------------------------------------------------------------------------


def test_codes_expire_are_attempt_limited_and_replace_each_other() -> None:
    clock = Clock()
    setup = AgentSetup(clock=clock)
    with pytest.raises(SetupError) as idle:
        setup.redeem("ABCD-EFGH", "hermes")
    assert idle.value.status == 410

    code = setup.start("hermes", mcp=False)
    assert len(code) == 9 and code[4] == "-"
    assert setup.redeem(code.lower().replace("-", " "), "hermes")  # forgiving about case and separators
    with pytest.raises(SetupError, match="not OpenClaw|for Hermes Agent"):
        setup.redeem(code, "openclaw")

    old = code
    code = setup.start("hermes", mcp=False)
    with pytest.raises(SetupError, match="not right"):
        setup.redeem(old, "hermes")

    for _ in range(CODE_ATTEMPTS - 1):
        with pytest.raises(SetupError, match="not right"):
            setup.redeem("WRONG-CODE", "hermes")
    assert setup.status()["state"] == "failed"
    with pytest.raises(SetupError) as burned:
        setup.redeem(code, "hermes")
    assert burned.value.status == 410

    code = setup.start("openclaw", mcp=True)
    clock.now += SETUP_SECONDS + 1
    assert setup.status()["state"] == "expired"
    with pytest.raises(SetupError):
        setup.redeem(code, "openclaw")


def test_pair_requests_are_validated() -> None:
    good = {"code": "X", "backend": "hermes", "bridge_url": BRIDGE + "/", "api_key": KEY}
    assert AgentSetup.validate_pair_request(good)[2] == BRIDGE
    for change, message in (
        ({"backend": "other"}, "backend"),
        ({"api_key": "short"}, "api_key"),
        ({"api_key": "k" * 40 + " x"}, "api_key"),
        ({"bridge_url": "ftp://192.168.1.20"}, "bridge_url"),
        ({"bridge_url": "http://192.168.1.20:8643/path"}, "bridge_url"),
        ({"bridge_url": "http://user:pw@192.168.1.20:8643"}, "credentials"),
        ({"bridge_url": "http://127.0.0.1:8643"}, "robot itself"),
        ({"bridge_url": "http://0.0.0.0:8643"}, "robot itself"),
    ):
        with pytest.raises(SetupError, match=message):
            AgentSetup.validate_pair_request({**good, **change})


# -- probing the bridge -------------------------------------------------------------------------------


def test_probe_accepts_a_ready_bridge_with_the_right_key() -> None:
    assert probe_bridge(BRIDGE, KEY, "hermes", client=fake_bridge())["status"] == "ok"


@pytest.mark.parametrize(
    ("health", "key", "message"),
    [
        (healthy("openclaw"), KEY, "--agent-backends hermes"),
        (healthy("hermes", ok=False, error="Hermes exposes broad host tools (terminal)"), KEY, "broad host tools"),
        (healthy(), "x" * 64, "refused that api_key"),
    ],
)
def test_probe_explains_what_to_fix(health: dict[str, Any], key: str, message: str) -> None:
    with pytest.raises(SetupError, match=message) as problem:
        probe_bridge(BRIDGE, key, "hermes", client=fake_bridge(health))
    assert problem.value.status == 424


def test_probe_reports_an_unreachable_bridge() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SetupError, match="--host 0.0.0.0"):
        probe_bridge(BRIDGE, KEY, "hermes", client=httpx.Client(transport=httpx.MockTransport(refuse)))


# -- HTTP routes ---------------------------------------------------------------------------------------


class FakeRuntime:
    control_ready = True
    kids_controls_locked = False

    def status(self) -> dict[str, Any]:
        return {"power_mode": "awake", "kids_mode": {"active": False}, "presence": {"enabled": False}}


def build(monkeypatch: pytest.MonkeyPatch, config: AppConfig, bridge: httpx.Client | None = None):  # type: ignore[no-untyped-def]
    stored = [config]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda value: stored.append(value))
    real_probe = agent_setup_mod.probe_bridge
    monkeypatch.setattr(
        main_module,
        "probe_bridge",
        lambda url, key, backend: real_probe(url, key, backend, client=bridge or fake_bridge()),
    )
    app = Homebody(False)
    app._runtime = FakeRuntime()  # type: ignore[assignment]
    return app, TestClient(app.settings_app, base_url="http://192.168.1.50:8042"), stored


def as_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests bypass the Owner boundary (see conftest); make handlers see a paired Owner."""
    monkeypatch.setattr(main_module, "owner_authenticated", SimpleNamespace(get=lambda: True))


def pair(client: TestClient, code: str, **overrides: Any) -> httpx.Response:
    body = {"code": code, "backend": "hermes", "bridge_url": BRIDGE, "api_key": KEY, "agent_name": "Hermes on box"}
    body.update(overrides)
    return client.post("/api/agent-setup/pair", content=json.dumps(body))


def test_full_pairing_saves_the_bridge_only_after_verifying_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, stored = build(monkeypatch, AppConfig())

    started = client.post("/api/agent-setup/start", json={"backend": "hermes", "mcp": True}).json()
    assert started["state"] == "waiting" and started["guide_url"] == "http://192.168.1.50:8042/agent-setup/hermes.md"
    assert started["code"] in started["message"]

    guide = client.get("/agent-setup/hermes.md")
    assert guide.status_code == 200 and guide.headers["content-type"].startswith("text/markdown")
    assert "http://192.168.1.50:8042/api/agent-setup/pair" in guide.text and started["code"] not in guide.text
    manifest = client.get("/agent-setup/bridge/manifest.json").json()
    first = manifest["files"][0]
    downloaded = client.get(f"/agent-setup/bridge/{first['name']}").content
    assert hashlib.sha256(downloaded).hexdigest() == first["sha256"]

    wrong = pair(client, "WRONG-CODE")
    assert wrong.status_code == 403 and len(stored) == 1

    paired = pair(client, started["code"])
    assert paired.status_code == 200, paired.text
    answer = paired.json()
    assert answer["ok"] is True and answer["realtime_available"] is True
    saved = stored[-1]
    assert saved.bridge_url == BRIDGE and saved.api_key == KEY and saved.mcp_enabled
    token = answer["mcp"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert answer["mcp"]["url"] == "http://192.168.1.50:8042/mcp" and token_matches(token, saved.mcp_token_sha256)

    status = client.get("/api/agent-setup/status").json()
    assert status["state"] == "paired" and "bridge_url" not in status and "agent_name" not in status
    as_owner(monkeypatch)
    status = client.get("/api/agent-setup/status").json()
    assert status["state"] == "paired" and status["bridge_url"] == BRIDGE and status["agent_name"] == "Hermes on box"
    assert pair(client, started["code"]).status_code == 410  # single use


def test_a_failing_bridge_keeps_the_code_and_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = fake_bridge(healthy("hermes", ok=False, error="Hermes exposes broad host tools (terminal)"))
    _app, client, stored = build(monkeypatch, AppConfig(), bridge=broken)
    code = client.post("/api/agent-setup/start", json={"backend": "hermes"}).json()["code"]

    failed = pair(client, code)
    assert failed.status_code == 424 and "broad host tools" in failed.json()["error"]
    assert len(stored) == 1
    status = client.get("/api/agent-setup/status").json()
    assert status["state"] == "waiting" and "last_error" not in status
    as_owner(monkeypatch)
    status = client.get("/api/agent-setup/status").json()
    assert status["state"] == "waiting" and "broad host tools" in status["last_error"]

    for _ in range(CODE_ATTEMPTS + 1):  # fixing and retrying never burns the code
        assert pair(client, code).status_code == 424


def test_without_mcp_no_token_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, stored = build(monkeypatch, AppConfig())
    code = client.post("/api/agent-setup/start", json={"backend": "hermes"}).json()["code"]
    answer = pair(client, code).json()
    assert "mcp" not in answer and not stored[-1].mcp_enabled and not stored[-1].mcp_token_sha256


def test_starting_and_cancelling_need_the_owner_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, _stored = build(monkeypatch, AppConfig(api_key="owner-key"))

    assert client.post("/api/agent-setup/start", json={"backend": "hermes"}).status_code == 403
    started = client.post("/api/agent-setup/start", json={"backend": "openclaw", "current_api_key": "owner-key"})
    assert started.status_code == 200
    assert (
        client.post("/api/agent-setup/start", json={"backend": "nope", "current_api_key": "owner-key"}).status_code
        == 422
    )
    assert client.post("/api/agent-setup/cancel", json={}).status_code == 403
    cancelled = client.post("/api/agent-setup/cancel", json={"current_api_key": "owner-key"}).json()
    assert cancelled["state"] == "cancelled"
    assert pair(client, started.json()["code"], backend="openclaw").status_code == 410


def test_pairing_rejects_bad_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, _stored = build(monkeypatch, AppConfig())
    client.post("/api/agent-setup/start", json={"backend": "hermes"})

    assert client.post("/api/agent-setup/pair", content=b"[1]").status_code == 400
    assert client.post("/api/agent-setup/pair", content=b"{nope").status_code == 400
    assert client.post("/api/agent-setup/pair", content=b"x" * 20_000).status_code == 413
    assert pair(client, "X", bridge_url="http://127.0.0.1:8643").status_code == 400
    assert client.get("/agent-setup/other.md").status_code == 404


def test_setup_routes_never_reach_the_public_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig(mcp_enabled=True, mcp_oauth_enabled=True, mcp_public_url="https://reachy.example.com")
    _app, client, _stored = build(monkeypatch, config)
    public = {"Host": "reachy.example.com"}

    for path in ("/agent-setup/hermes.md", "/agent-setup/bridge/manifest.json", "/api/agent-setup/status"):
        assert client.get(path, headers=public).status_code == 404
    assert client.post("/api/agent-setup/pair", content=b"{}", headers=public).status_code == 404


def test_kids_lock_blocks_pairing(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client, _stored = build(monkeypatch, AppConfig())
    code = client.post("/api/agent-setup/start", json={"backend": "hermes"}).json()["code"]
    app._runtime.kids_controls_locked = True  # type: ignore[union-attr]
    assert pair(client, code).status_code == 423
