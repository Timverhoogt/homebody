from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from companion.agent_backends import OpenClawBackend, OpenClawConfig, OpenClawConfigError
from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.hermes_client import HermesBridgeClient, HermesBridgeError

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, ROOT / "companion" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_module = load("hermes_reachy_bridge")


def config(**overrides: Any):  # type: ignore[no-untyped-def]
    values: dict[str, Any] = {"url": "http://127.0.0.1:18789/v1/", "token": "gateway-secret", "agents": "reachy"}
    values.update(overrides)
    return OpenClawConfig.build(**values)


def test_config_normalises_the_gateway_url_and_deduplicates_agents() -> None:
    cfg = config(agents="reachy, kitchen,reachy")

    assert cfg.url == "http://127.0.0.1:18789"
    assert cfg.agents == ("reachy", "kitchen")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"agents": "main"}, "dedicated agent"),
        ({"agents": "reachy,default"}, "dedicated agent"),
        ({"agents": ""}, "at least one"),
        ({"agents": "../etc"}, "Invalid OpenClaw agent id"),
        ({"token": " "}, "OPENCLAW_GATEWAY_TOKEN"),
        ({"url": "ws://127.0.0.1:18789"}, "http"),
    ],
)
def test_config_fails_closed(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(OpenClawConfigError, match=message):
        config(**overrides)


def test_primary_agent_needs_an_explicit_opt_in() -> None:
    assert config(agents="main", allow_primary_agent=True).agents == ("main",)


def test_from_env_reads_token_agents_and_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "from-env")
    monkeypatch.setenv("REACHY_OPENCLAW_AGENTS", "desk")
    monkeypatch.delenv("OPENCLAW_GATEWAY_URL", raising=False)

    cfg = OpenClawConfig.from_env()
    assert (cfg.url, cfg.token, cfg.agents) == ("http://127.0.0.1:18789", "from-env", ("desk",))

    monkeypatch.setenv("REACHY_OPENCLAW_AGENTS", "main")
    with pytest.raises(OpenClawConfigError):
        OpenClawConfig.from_env()
    monkeypatch.setenv("REACHY_OPENCLAW_ALLOW_PRIMARY_AGENT", "1")
    assert OpenClawConfig.from_env().agents == ("main",)


def test_requests_only_reach_allowlisted_agents_with_safe_fields() -> None:
    backend = OpenClawBackend(config(agents="reachy,kitchen"))

    assert backend.claims("openclaw/kitchen") and backend.claims("OpenClaw:x") and not backend.claims("hermes-agent")
    assert backend.target_for("openclaw/kitchen") == "openclaw/kitchen"
    assert backend.target_for("openclaw:kitchen") == "openclaw/kitchen"
    # The owner's agent or any unknown id is never reachable from Reachy.
    assert backend.target_for("openclaw/main") == "openclaw/reachy"
    assert backend.target_for("hermes-agent") == "openclaw/reachy"

    body = backend.chat_payload(
        {
            "model": "openclaw/main",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "tools": [{"type": "function", "function": {"name": "exec"}}],
            "user": "attacker-chosen",
            "temperature": 0.3,
        },
        session_id="reachy-robot-1-abc/../x",
    )
    assert body == {
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.3,
        "model": "openclaw/reachy",
        "stream": False,
        "user": "reachy:reachy-robot-1-abc..x",
    }


class FakeUpstreams:
    """In-process stand-ins for an OpenClaw Gateway and a Hermes API server."""

    def __init__(self, agents: tuple[str, ...] = ("openclaw", "openclaw/default", "openclaw/reachy")) -> None:
        self.agents = agents
        self.openclaw_requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.hermes_requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        self.toolset_checks = 0

    def openclaw_app(self) -> web.Application:
        async def models(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == "Bearer gateway-secret"
            return web.json_response({"object": "list", "data": [{"id": agent} for agent in self.agents]})

        async def chat(request: web.Request) -> web.Response:
            payload = await request.json()
            self.openclaw_requests.append((dict(request.headers), payload))
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Hi from OpenClaw"}}]})

        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_post("/v1/chat/completions", chat)
        return app

    def hermes_app(self) -> web.Application:
        async def health(request: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        async def models(request: web.Request) -> web.Response:
            return web.json_response({"object": "list", "data": [{"id": "hermes-agent"}]})

        async def toolsets(request: web.Request) -> web.Response:
            self.toolset_checks += 1
            return web.json_response([{"enabled": True, "tools": ["web_search"]}])

        async def chat(request: web.Request) -> web.Response:
            payload = await request.json()
            self.hermes_requests.append((dict(request.headers), payload))
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Hi from Hermes"}}]})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/v1/models", models)
        app.router.add_get("/v1/toolsets", toolsets)
        app.router.add_post("/v1/chat/completions", chat)
        return app


def run_bridge(fakes: FakeUpstreams, scenario, *, backends_: tuple[str, ...], agents: str = "reachy"):  # type: ignore[no-untyped-def]
    async def main() -> Any:
        openclaw_server = TestServer(fakes.openclaw_app())
        hermes_server = TestServer(fakes.hermes_app())
        await openclaw_server.start_server()
        await hermes_server.start_server()
        try:
            app = bridge_module.create_app(
                api_key="bridge-key",
                hermes_url=str(hermes_server.make_url("")).rstrip("/"),
                backends=backends_,
                openclaw=config(url=str(openclaw_server.make_url("")), agents=agents),
            )
            async with TestClient(TestServer(app)) as client:
                return await scenario(client)
        finally:
            await openclaw_server.close()
            await hermes_server.close()

    return asyncio.run(main())


AUTH = {"Authorization": "Bearer bridge-key"}


def test_openclaw_only_bridge_reports_health_models_and_routes_chat() -> None:
    fakes = FakeUpstreams()

    async def scenario(client: TestClient) -> dict[str, Any]:
        health = await (await client.get("/health")).json()
        models = await (await client.get("/v1/models", headers=AUTH)).json()
        voice = await (await client.get("/v1/voice-options", headers=AUTH)).json()
        chat = await client.post(
            "/v1/chat/completions",
            headers={**AUTH, "X-Hermes-Session-Id": "reachy-robot-1-abc", "x-openclaw-model": "openai/gpt-5.4"},
            json={"model": "hermes-agent", "stream": False, "messages": [{"role": "user", "content": "Hello"}]},
        )
        return {"health": health, "models": models, "voice": voice, "chat": await chat.json()}

    result = run_bridge(fakes, scenario, backends_=("openclaw",))

    health = result["health"]
    assert health["status"] == "ok" and health["agent_api"] is True and "hermes_api" not in health
    assert health["agent_backends"] == [{"name": "openclaw", "label": "OpenClaw", "ok": True}]
    assert [item["id"] for item in result["models"]["data"]] == ["openclaw/reachy"]
    assert result["voice"] == {"stt": [], "tts": []}
    assert result["chat"]["choices"][0]["message"]["content"] == "Hi from OpenClaw"
    headers, payload = fakes.openclaw_requests[0]
    # The Gateway gets its own token, never the bridge key; caller overrides are not forwarded.
    assert headers["Authorization"] == "Bearer gateway-secret"
    assert not any(name.lower().startswith("x-openclaw") for name in headers)
    assert payload["model"] == "openclaw/reachy" and payload["user"] == "reachy:reachy-robot-1-abc"
    assert fakes.hermes_requests == [] and fakes.toolset_checks == 0


def test_health_names_a_missing_openclaw_agent() -> None:
    fakes = FakeUpstreams(agents=("openclaw", "openclaw/default"))

    async def scenario(client: TestClient) -> dict[str, Any]:
        return await (await client.get("/health")).json()

    health = run_bridge(fakes, scenario, backends_=("openclaw",))

    assert health["status"] == "degraded" and health["agent_api"] is False
    assert health["agent_backends"][0]["error"] == "OpenClaw agent not found: openclaw/reachy"


def test_both_backends_route_by_model_and_merge_model_lists() -> None:
    fakes = FakeUpstreams()

    async def scenario(client: TestClient) -> dict[str, Any]:
        models = await (await client.get("/v1/models", headers=AUTH)).json()
        for model in ("hermes-agent", "openclaw/reachy"):
            response = await client.post(
                "/v1/chat/completions",
                headers=AUTH,
                json={"model": model, "messages": [{"role": "user", "content": "Hello"}]},
            )
            assert response.status == 200
        health = await (await client.get("/health")).json()
        return {"models": models, "health": health}

    result = run_bridge(fakes, scenario, backends_=("hermes", "openclaw"))

    assert [item["id"] for item in result["models"]["data"]] == ["hermes-agent", "openclaw/reachy"]
    assert len(fakes.hermes_requests) == 1 and fakes.hermes_requests[0][1]["model"] == "hermes-agent"
    assert fakes.toolset_checks == 1  # the Hermes tool boundary still guards Hermes requests
    assert len(fakes.openclaw_requests) == 1
    assert result["health"]["hermes_api"] is True and result["health"]["agent_api"] is True
    assert [item["name"] for item in result["health"]["agent_backends"]] == ["hermes", "openclaw"]


def test_realtime_agent_tool_follows_the_backend() -> None:
    bridge = bridge_module.Bridge(
        api_key="k",
        hermes_url="http://127.0.0.1:9",
        backends=("hermes", "openclaw"),
        openclaw=config(),
    )

    assert bridge._agent_identity("hermes-agent") == ("ask_hermes", "Hermes")
    assert bridge._agent_identity("openclaw/reachy") == ("ask_openclaw", "OpenClaw")
    tools = bridge_module._build_realtime_tools(
        False, False, True, False, agent_tool_name="ask_openclaw", agent_label="OpenClaw"
    )
    assert tools[0]["name"] == "ask_openclaw" and "OpenClaw memory" in tools[0]["description"]
    item = {"type": "function_call", "status": "completed", "name": "ask_openclaw", "call_id": "c1", "arguments": "{}"}
    event = {"item": item}
    assert bridge_module._completed_hermes_call("response.output_item.done", event) == ("c1", {})


def test_openclaw_answer_uses_the_realtime_session_and_allowlisted_agent() -> None:
    fakes = FakeUpstreams()

    async def scenario(client: TestClient) -> str:
        routes = client.server.app.router.routes()
        bridge = next(route.handler.__self__ for route in routes if route.resource.canonical == "/health")
        return await bridge._hermes_answer(
            "What's on my calendar?",
            model="openclaw/main",
            system_prompt="Be brief.",
            session_id="reachy-realtime-robot-1",
        )

    answer = run_bridge(fakes, scenario, backends_=("openclaw",))

    assert answer == "Hi from OpenClaw"
    payload = fakes.openclaw_requests[0][1]
    assert payload["model"] == "openclaw/reachy" and payload["user"] == "reachy:reachy-realtime-robot-1"
    assert payload["messages"][0] == {"role": "system", "content": "Be brief."}


def test_bridge_rejects_unknown_or_unconfigured_backends() -> None:
    with pytest.raises(ValueError):
        bridge_module.Bridge(api_key="k", hermes_url="http://x", backends=("openclaw",))
    with pytest.raises(ValueError):
        bridge_module.Bridge(api_key="k", hermes_url="http://x", backends=("claude",))


def test_configured_speech_explains_that_it_needs_hermes() -> None:
    bridge = bridge_module.Bridge(api_key="k", hermes_url="http://x", backends=("openclaw",), openclaw=config())

    with pytest.raises(web.HTTPConflict) as refused:
        bridge._require_hermes_speech()
    assert "ElevenLabs" in refused.value.text


def test_app_health_reports_which_agent_backend_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "degraded",
                "agent_api": False,
                "agent_backends": [
                    {"name": "openclaw", "label": "OpenClaw", "ok": False, "error": "OpenClaw agent not found"}
                ],
            },
        )

    client = HermesBridgeClient(
        AppConfig(bridge_url="http://bridge.test", api_key="k"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(HermesBridgeError, match="OpenClaw: OpenClaw agent not found"):
        client.health()


def test_app_health_accepts_an_openclaw_only_bridge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_api": True, "agent_backends": []})

    client = HermesBridgeClient(
        AppConfig(bridge_url="http://bridge.test", api_key="k"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.health()["agent_api"] is True


def test_settings_ui_labels_openclaw_agents() -> None:
    script = (ROOT / "reachy_mini_hermes" / "static" / "main.js").read_text(encoding="utf-8")

    assert "OpenClaw agent ·" in script
    assert json.dumps("hermes-agent") in script
