from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from companion.llm_providers import LLMProviderConfigError, TextModelProvider

ROOT = Path(__file__).resolve().parents[1]


def provider(**env: str) -> TextModelProvider:
    return TextModelProvider.from_env(env.get)


def load_bridge():  # type: ignore[no-untyped-def]
    path = ROOT / "companion" / "hermes_reachy_bridge.py"
    spec = importlib.util.spec_from_file_location("hermes_reachy_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openai_stays_the_default_with_existing_models_and_fields() -> None:
    openai = provider()

    assert openai.chat_url == "https://api.openai.com/v1/chat/completions"
    assert openai.models == {"agent": "gpt-5-mini", "kids": "gpt-5-mini", "ispy": "gpt-4.1-mini"}
    body = openai.request({"model": "m", "max_completion_tokens": 40, "store": False, "reasoning_effort": "minimal"})
    assert body == {"model": "m", "max_completion_tokens": 40, "store": False, "reasoning_effort": "minimal"}


def test_existing_model_overrides_still_apply_to_openai() -> None:
    assert provider(REACHY_AGENT_MODEL="gpt-6-mini").model("agent") == "gpt-6-mini"


def test_cortecs_keeps_traffic_with_eu_providers_and_drops_openai_only_fields() -> None:
    cortecs = provider(REACHY_LLM_PROVIDER="cortecs", REACHY_LLM_MODEL="mistral-medium", REACHY_ISPY_MODEL="pixtral")

    assert cortecs.chat_url == "https://api.cortecs.ai/v1/chat/completions"
    assert cortecs.region == "EU"
    assert cortecs.models == {"agent": "mistral-medium", "kids": "mistral-medium", "ispy": "pixtral"}
    body = cortecs.request({"model": "m", "max_completion_tokens": 40, "store": False, "reasoning_effort": "minimal"})
    assert body == {"model": "m", "max_tokens": 40, "eu_native": True}

    relaxed = provider(REACHY_LLM_PROVIDER="cortecs", REACHY_LLM_MODEL="m", REACHY_CORTECS_EU_NATIVE="0")
    assert "eu_native" not in relaxed.request({"model": "m"})


def test_llmrouter_and_custom_providers() -> None:
    router = provider(REACHY_LLM_PROVIDER="llmrouter", REACHY_LLM_MODEL="claude-haiku-4-5")
    assert router.chat_url == "https://proxy.llmrouter.eu/v1/chat/completions"
    assert router.api_key({"LLMROUTER_API_KEY": "k"}.get) == "k"
    # An OpenAI key is never used for another provider.
    assert router.api_key({"OPENAI_API_KEY": "sk"}.get) == ""

    custom = provider(REACHY_LLM_PROVIDER="custom", REACHY_LLM_URL="https://llm.example.eu/v1/", REACHY_LLM_MODEL="m")
    assert custom.chat_url == "https://llm.example.eu/v1/chat/completions"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"REACHY_LLM_PROVIDER": "cortecs"}, "REACHY_LLM_MODEL"),
        ({"REACHY_LLM_PROVIDER": "llmrouter", "REACHY_AGENT_MODEL": "a"}, "REACHY_KIDS_MODEL, REACHY_ISPY_MODEL"),
        ({"REACHY_LLM_PROVIDER": "custom", "REACHY_LLM_MODEL": "m"}, "REACHY_LLM_URL"),
        ({"REACHY_LLM_PROVIDER": "mystery"}, "must be one of"),
        ({"REACHY_LLM_PROVIDER": "cortecs", "REACHY_LLM_MODEL": "m", "REACHY_LLM_URL": "ftp://x"}, "REACHY_LLM_URL"),
    ],
)
def test_incomplete_provider_settings_fail_closed(env: dict[str, str], message: str) -> None:
    with pytest.raises(LLMProviderConfigError, match=message):
        provider(**env)


def test_bridge_sends_ispy_judging_to_the_eu_router_with_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge_module = load_bridge()
    seen: list[tuple[str, dict[str, Any]]] = []

    async def chat(request: web.Request) -> web.Response:
        seen.append((request.headers["Authorization"], await request.json()))
        return web.json_response({"choices": [{"message": {"content": '{"match": true}'}}]})

    async def main() -> tuple[bool, dict[str, Any]]:
        router = web.Application()
        router.router.add_post("/v1/chat/completions", chat)
        server = TestServer(router)
        await server.start_server()
        try:
            llm = provider(
                REACHY_LLM_PROVIDER="cortecs",
                REACHY_LLM_URL=str(server.make_url("/v1")),
                REACHY_LLM_MODEL="mistral-medium",
            )
            monkeypatch.setenv("CORTECS_API_KEY", "cortecs-key")
            app = bridge_module.create_app(api_key="bridge-key", hermes_url="http://127.0.0.1:9", llm=llm)
            async with TestClient(TestServer(app)) as client:
                bridge = next(r.handler.__self__ for r in app.router.routes() if r.resource.canonical == "/health")
                match = await bridge._judge_ispy_guess(
                    "mug", {"object_name": "cup"}, language="en", openai_key="sk-openai-unused"
                )
                health = await (await client.get("/health")).json()
                return match, health
        finally:
            await server.close()

    match, health = asyncio.run(main())

    assert match is True
    authorization, body = seen[0]
    # The router gets its own key; the OpenAI key never leaves for the EU provider.
    assert authorization == "Bearer cortecs-key"
    assert body["model"] == "mistral-medium" and body["eu_native"] is True
    assert "store" not in body and body["max_tokens"] == 40
    assert health["text_provider"]["name"] == "cortecs" and health["text_provider"]["region"] == "EU"
    assert health["agent_model_available"] is True
