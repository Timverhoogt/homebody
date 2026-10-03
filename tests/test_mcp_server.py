from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody
from homebody.mcp_server import (
    ANNOUNCEMENTS_PER_TEN_MINUTES,
    CALLS_PER_MINUTE,
    McpServer,
    new_token,
    token_digest,
    token_matches,
)

TOKEN = "hb_test-token"


class FakeRuntime:
    def __init__(self, *, power_mode: str = "awake", kids: bool = False, ready: bool = True) -> None:
        self.control_ready = ready
        self.power_mode = power_mode
        self.kids = kids
        self.presence_level = "present"
        self.announcements: list[str] = []
        self.actions: list[tuple[str, str, bool]] = []
        self.questions: list[str] = []
        self.announcement_error = ""

    def status(self) -> dict[str, Any]:
        return {
            "power_mode": self.power_mode,
            "state": "waiting_for_wake_word",
            "kids_mode": {"active": self.kids, "locked": False},
            "presence": {"enabled": True, "level": self.presence_level},
        }

    def queue_announcement(self, text: str) -> dict[str, Any]:
        if self.announcement_error:
            raise RuntimeError(self.announcement_error)
        self.announcements.append(text)
        return {"ok": True, "queued": True, "queue_depth": len(self.announcements)}

    def queue_manual_robot_action(self, action: str, value: str, *, wake_if_standby: bool = True) -> dict[str, Any]:
        self.actions.append((action, value, wake_if_standby))
        return {"ok": True}

    def describe_camera_view(self, question: str, *, config: AppConfig) -> dict[str, object]:
        self.questions.append(question)
        return {"answer": "A mug on a wooden desk.", "model": "qwen2.5vl:3b", "latency_ms": 900}


def enabled_config(**overrides: Any) -> AppConfig:
    values: dict[str, Any] = {"mcp_enabled": True, "mcp_token_sha256": token_digest(TOKEN)}
    values.update(overrides)
    return AppConfig(**values)


def call(server: McpServer, name: str, arguments: dict[str, Any] | None = None, config: AppConfig | None = None):  # type: ignore[no-untyped-def]
    params = {"name": name, "arguments": arguments or {}}
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}
    return server.handle(message, config or enabled_config())


def test_tokens_are_random_and_only_their_hash_is_checked() -> None:
    first, second = new_token(), new_token()

    assert first != second and first.startswith("hb_")
    assert token_matches(first, token_digest(first))
    assert not token_matches(second, token_digest(first))
    assert not token_matches("", token_digest(first)) and not token_matches(first, "")


def test_initialize_negotiates_the_protocol_and_lists_safe_tools() -> None:
    server = McpServer(lambda: FakeRuntime())

    init = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        enabled_config(),
    )
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, enabled_config()) is None
    unknown = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        enabled_config(),
    )
    assert unknown["result"]["protocolVersion"] == "2025-06-18"

    listed = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, enabled_config())
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == ["get_status", "announce", "express_emotion"]
    with_vision = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, enabled_config(mcp_vision_enabled=True)
    )["result"]["tools"]
    assert with_vision[-1]["name"] == "look_and_describe"


def test_unknown_methods_and_tools_are_json_rpc_errors() -> None:
    server = McpServer(lambda: FakeRuntime())

    missing = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, enabled_config())
    assert missing["error"]["code"] == -32601
    assert call(server, "move_joint")["error"]["code"] == -32602
    # The vision tool does not exist unless the owner turned it on.
    assert call(server, "look_and_describe", {"question": "?"})["error"]["code"] == -32602
    assert server.handle({"id": 1, "method": "ping"}, enabled_config())["error"]["code"] == -32600


def test_status_explains_what_reachy_can_do_now() -> None:
    runtime = FakeRuntime(power_mode="standby")
    result = call(McpServer(lambda: runtime), "get_status")["result"]

    assert result["isError"] is False
    assert result["structuredContent"] == {
        "power_mode": "standby",
        "activity": "waiting for wake word",
        "someone_present": True,
        "can_announce": True,
        "can_express_emotion": False,
        "can_look": False,
    }
    assert "only while it is Awake" in result["content"][0]["text"]


def test_announce_queues_a_short_message_and_respects_the_runtime_gates() -> None:
    runtime = FakeRuntime()
    server = McpServer(lambda: runtime)

    ok = call(server, "announce", {"text": "  Your   3 pm call starts in five minutes. "})["result"]
    assert ok["isError"] is False and runtime.announcements == ["Your 3 pm call starts in five minutes."]

    runtime.announcement_error = "Announcements are blocked in Meeting and Sleep"
    blocked = call(server, "announce", {"text": "Hello"})["result"]
    assert blocked["isError"] is True and "Meeting and Sleep" in blocked["content"][0]["text"]
    assert call(server, "announce", {"text": "x" * 501})["result"]["isError"] is True


def test_child_sessions_refuse_every_action() -> None:
    runtime = FakeRuntime(kids=True)
    server = McpServer(lambda: runtime)
    config = enabled_config(mcp_vision_enabled=True)

    for name, arguments in (
        ("announce", {"text": "Hello"}),
        ("express_emotion", {"emotion": "happy"}),
        ("look_and_describe", {"question": "What is there?"}),
    ):
        result = call(server, name, arguments, config)["result"]
        assert result["isError"] is True and "child session" in result["content"][0]["text"]
    assert runtime.announcements == [] and runtime.actions == [] and runtime.questions == []


def test_emotions_only_play_while_awake_and_never_wake_reachy() -> None:
    runtime = FakeRuntime(power_mode="standby")
    server = McpServer(lambda: runtime)

    refused = call(server, "express_emotion", {"emotion": "happy"})["result"]
    assert refused["isError"] is True and "won't wake up" in refused["content"][0]["text"]
    assert runtime.actions == []

    runtime.power_mode = "awake"
    assert call(server, "express_emotion", {"emotion": "happy"})["result"]["isError"] is False
    assert runtime.actions == [("emotion", "happy", False)]
    assert call(server, "express_emotion", {"emotion": "rage"})["result"]["isError"] is True


def test_look_and_describe_returns_text_only() -> None:
    runtime = FakeRuntime()
    config = enabled_config(mcp_vision_enabled=True)
    question = {"question": "What is on the desk?"}
    result = call(McpServer(lambda: runtime), "look_and_describe", question, config)["result"]

    assert result["content"] == [{"type": "text", "text": "A mug on a wooden desk."}]
    assert runtime.questions == ["What is on the desk?"]


def test_a_starting_runtime_refuses_politely() -> None:
    result = call(McpServer(lambda: FakeRuntime(ready=False)), "get_status")["result"]
    assert result["isError"] is True and "starting up" in result["content"][0]["text"]
    assert call(McpServer(lambda: None), "get_status")["result"]["isError"] is True


def test_rate_limits_stop_runaway_agents() -> None:
    now = [0.0]
    runtime = FakeRuntime()
    server = McpServer(lambda: runtime, clock=lambda: now[0])

    for _ in range(ANNOUNCEMENTS_PER_TEN_MINUTES):
        assert call(server, "announce", {"text": "Hi"})["result"]["isError"] is False
    assert "several messages" in call(server, "announce", {"text": "Hi"})["result"]["content"][0]["text"]
    now[0] = 601.0
    assert call(server, "announce", {"text": "Hi"})["result"]["isError"] is False

    for _ in range(CALLS_PER_MINUTE - 1):
        call(server, "get_status")
    assert "Too many requests" in call(server, "get_status")["result"]["content"][0]["text"]
    assert server.status()["refused"] >= 2


def build(monkeypatch: pytest.MonkeyPatch, config: AppConfig) -> tuple[Homebody, TestClient, list[AppConfig]]:
    stored = [config]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda value: stored.append(value))
    app = Homebody(False)
    app._runtime = FakeRuntime()  # type: ignore[assignment]
    app._runtime.kids_controls_locked = False  # type: ignore[attr-defined]
    return app, TestClient(app.settings_app), stored


AUTH = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}


def test_endpoint_is_off_by_default_and_needs_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, _stored = build(monkeypatch, AppConfig())
    assert client.post("/mcp", json=INIT, headers=AUTH).status_code == 404

    _app, client, _stored = build(monkeypatch, enabled_config())
    missing = client.post("/mcp", json=INIT)
    assert missing.status_code == 401 and missing.headers["www-authenticate"].startswith("Bearer")
    assert client.post("/mcp", json=INIT, headers={"Authorization": "Bearer wrong"}).status_code == 401

    ok = client.post("/mcp", json=INIT, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["result"]["serverInfo"]["name"] == "homebody"


def test_endpoint_transport_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, _stored = build(monkeypatch, enabled_config())

    foreign = client.post("/mcp", json=INIT, headers={**AUTH, "Origin": "http://evil.example"})
    assert foreign.status_code == 403
    same = client.post("/mcp", json=INIT, headers={**AUTH, "Origin": "http://testserver"})
    assert same.status_code == 200
    assert client.post("/mcp", json=INIT, headers={**AUTH, "MCP-Protocol-Version": "1999-01-01"}).status_code == 400
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
    assert note.status_code == 202
    assert client.post("/mcp", json=[INIT], headers=AUTH).status_code == 400
    assert client.post("/mcp", content=b"{not json", headers=AUTH).json()["error"]["code"] == -32700
    assert client.post("/mcp", content=b"x" * 70_000, headers=AUTH).status_code == 413
    assert client.get("/mcp", headers=AUTH).status_code == 405


def test_tool_call_over_http_reaches_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client, _stored = build(monkeypatch, enabled_config())

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "announce", "arguments": {"text": "Tea is ready"}},
        },
        headers=AUTH,
    )
    assert response.status_code == 200 and response.json()["result"]["isError"] is False
    assert app._runtime.announcements == ["Tea is ready"]  # type: ignore[union-attr]
    assert client.get("/api/mcp/status").json()["last_tool"] == "announce"


def test_token_management_needs_the_owner_key_and_stores_only_a_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, stored = build(monkeypatch, AppConfig(api_key="sk-owner", mcp_enabled=True))

    assert client.post("/api/mcp/token", json={"current_api_key": "guess"}).status_code == 403
    created = client.post("/api/mcp/token", json={"current_api_key": "sk-owner"}).json()
    token = created["token"]
    assert stored[-1].mcp_token_sha256 == token_digest(token)
    assert token not in str(stored[-1].redacted_dict())
    assert stored[-1].redacted_dict()["mcp_token_configured"] is True

    ok = client.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    revoked = client.post("/api/mcp/token/revoke", json={"current_api_key": "sk-owner"}).json()
    assert revoked["token_configured"] is False
    assert client.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_the_settings_form_cannot_set_the_token_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, stored = build(monkeypatch, AppConfig())

    client.post("/api/settings", json={"mcp_token_sha256": "0" * 64, "mcp_enabled": True})
    # Only the token routes, which need the owner's key, can set the hash.
    assert all(not config.mcp_token_sha256 for config in stored)


def test_invalid_token_hash_is_rejected() -> None:
    with pytest.raises(ValueError):
        AppConfig(mcp_token_sha256="not-a-hash")
