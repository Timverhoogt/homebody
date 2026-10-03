from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module

SESSION = "kids-" + "c" * 32
AUTH = {"Authorization": "Bearer secret"}


def run_with_client(scenario, monkeypatch: pytest.MonkeyPatch):
    module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    async def main():
        app = module.create_app(api_key="secret", hermes_url="http://127.0.0.1:9")
        bridge = app.middlewares[0].__self__
        async with TestClient(TestServer(app)) as client:
            return await scenario(module, bridge, client)

    return asyncio.run(main())


def test_live_kids_session_latches_adult_routes_until_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(_module, bridge, client) -> None:
        async def adult_chat_status() -> int:
            response = await client.post("/v1/chat/completions", headers=AUTH, json={"messages": []})
            return response.status

        assert await adult_chat_status() != 423

        started = await client.post("/v1/kids/session", headers=AUTH, json={"session_id": SESSION, "state": "active"})
        assert started.status == 200
        assert bridge.kids_session_live() is True

        assert await adult_chat_status() == 423
        assert (await client.post("/v1/agent/execute", headers=AUTH, json={})).status == 423
        assert (await client.post("/v1/agent/approve-pending", headers=AUTH, json={})).status == 423
        assert (await client.get("/v1/realtime", headers=AUTH)).status == 423
        # Without credentials the latch does not reveal Kids state.
        assert (await client.post("/v1/chat/completions", json={})).status == 401
        # Reachy can always wind adult work down.
        assert (await client.post("/v1/agent/cancel/agent-123", headers=AUTH, json={})).status != 423
        assert (await client.post("/v1/agent/run/cancel", headers=AUTH, json={})).status != 423

        ended = await client.post("/v1/kids/session", headers=AUTH, json={"session_id": SESSION, "state": "ended"})
        assert ended.status == 200
        assert await adult_chat_status() != 423

        # A start notification that arrives after its end must not re-latch the bridge.
        late = await client.post("/v1/kids/session", headers=AUTH, json={"session_id": SESSION, "state": "active"})
        assert late.status == 200
        assert bridge.kids_session_live() is False

    run_with_client(scenario, monkeypatch)


def test_kids_latch_expires_after_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(module, bridge, _client) -> None:
        bridge._mark_kids_live(SESSION)
        assert bridge.kids_session_live() is True
        bridge._kids_live[SESSION] = time.monotonic() - module._KIDS_LIVE_MAX_SECONDS - 1
        assert bridge.kids_session_live() is False

    run_with_client(scenario, monkeypatch)


def test_generic_speech_is_moderated_while_kids_session_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(_module, bridge, client) -> None:
        screened: list[str] = []

        async def flagged(text: str, _key: str) -> bool:
            screened.append(text)
            return True

        bridge._moderation_flagged = flagged
        bridge._mark_kids_live(SESSION)
        response = await client.post("/v1/audio/speech", headers=AUTH, json={"input": "unsafe adult text"})
        assert response.status == 403
        assert screened == ["unsafe adult text"]

    run_with_client(scenario, monkeypatch)


def test_kids_session_route_rejects_malformed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(_module, _bridge, client) -> None:
        for payload in (
            {"session_id": "kids-short", "state": "active"},
            {"session_id": SESSION, "state": "paused"},
            {"session_id": SESSION, "state": "active", "extra": True},
        ):
            assert (await client.post("/v1/kids/session", headers=AUTH, json=payload)).status == 400
        assert (await client.post("/v1/kids/session", json={"session_id": SESSION, "state": "active"})).status == 401

    run_with_client(scenario, monkeypatch)
