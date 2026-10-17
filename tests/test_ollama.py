"""Ollama wire contracts; synthetic loopback server, not native model acceptance."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.llm_providers import LLMProviderConfigError, TextModelProvider, everyday_provider_from_env
from homebody.local_vision import LocalVisionClient


def everyday(**extra):
    return everyday_provider_from_env({
        "REACHY_EVERYDAY_PROVIDER": "ollama",
        "REACHY_EVERYDAY_MODEL": "llama3.2:3b",
        **extra,
    }.get)


def test_ollama_needs_no_cloud_key_and_never_resolves_one():
    provider = everyday()
    assert provider.chat_url == "http://127.0.0.1:11434/v1/chat/completions"
    requested = []

    def resolve(name):
        requested.append(name)
        return {"OPENAI_API_KEY": "must-not-leak"}.get(name, "")

    assert provider.api_key(resolve) == "ollama"  # ignored by the local server
    assert requested == ["OLLAMA_API_KEY"]
    assert provider.api_key({"OLLAMA_API_KEY": "proxy-token"}.get) == "proxy-token"
    assert provider.public_status()["name"] == "ollama"
    assert "base_url" not in provider.public_status()


@pytest.mark.parametrize("url", [
    "http://192.168.1.40:11434/v1", "http://mac.local:11434/v1/", "http://[::1]:11434/v1",
])
def test_ollama_accepts_explicit_pc_mac_and_ipv6_endpoints(url):
    assert everyday(REACHY_EVERYDAY_URL=url).base_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "ftp://host/v1", "http://user:secret@host/v1", "http://host/v1?key=secret", "http://host/v1#x",
])
def test_ollama_rejects_unsafe_endpoints(url):
    with pytest.raises(LLMProviderConfigError):
        everyday(REACHY_EVERYDAY_URL=url)


def test_ollama_requires_explicit_downloaded_models_and_supports_purpose_overrides():
    with pytest.raises(LLMProviderConfigError, match="REACHY_EVERYDAY_MODEL"):
        everyday(REACHY_EVERYDAY_MODEL="")
    with pytest.raises(LLMProviderConfigError, match="REACHY_LLM_MODEL"):
        TextModelProvider.from_env({"REACHY_LLM_PROVIDER": "ollama"}.get)
    provider = TextModelProvider.from_env({
        "REACHY_LLM_PROVIDER": "ollama", "REACHY_LLM_MODEL": "llama3.2:3b",
        "REACHY_ISPY_MODEL": "qwen2.5vl:3b", "REACHY_LLM_URL": "http://pc.local:11434/v1/",
    }.get)
    assert provider.models == {"agent": "llama3.2:3b", "kids": "llama3.2:3b", "ispy": "qwen2.5vl:3b"}
    assert provider.base_url == "http://pc.local:11434/v1"
    assert everyday(REACHY_LLM_PROVIDER="custom", REACHY_LLM_URL="https://cloud.example/v1").base_url == (
        "http://127.0.0.1:11434/v1"
    )
    inherited = everyday(REACHY_LLM_PROVIDER="ollama", REACHY_LLM_URL="http://pc.local:11434/v1")
    assert inherited.base_url == "http://pc.local:11434/v1"


def test_ollama_adapts_only_its_unsupported_fields_without_mutating_input():
    body = {"max_completion_tokens": 400, "store": False, "reasoning_effort": "minimal",
            "tools": [{"type": "function"}], "tool_choice": "auto", "parallel_tool_calls": True,
            "response_format": {"type": "json_object"}}
    original = dict(body)
    adapted = everyday().request(body)
    assert adapted == {"max_tokens": 400, "tools": body["tools"], "response_format": body["response_format"]}
    assert body == original
    assert TextModelProvider.from_env().request(body)["tool_choice"] == "auto"


def test_ollama_everyday_real_http_tool_roundtrip():
    from test_everyday_tools import EVERYDAY_TOOL_NAMES, make_tools

    async def run():
        seen = []

        async def model(request):
            body = await request.json()
            seen.append(body)
            assert request.headers["Authorization"] == "Bearer ollama"
            assert not {"store", "max_completion_tokens", "tool_choice", "parallel_tool_calls"} & body.keys()
            if len(seen) == 1:
                return web.json_response({"choices": [{"message": {"content": "", "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "get_current_time", "arguments": "{}"}},
                    {"id": "t2", "type": "function", "function": {"name": "ask_hermes", "arguments": "{}"}},
                ]}}]})
            return web.json_response({"choices": [{"message": {"content": "Synthetic answer."}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", model)
        async with TestServer(app) as server, ClientSession() as http:
            provider = everyday(REACHY_EVERYDAY_URL=str(server.make_url("/v1")))
            result = await make_tools("http://127.0.0.1:1").answer(
                "what time is it", http=http, provider=provider, key=provider.api_key(lambda _: ""),
                device_id="fixture", generation=1,
            )
        assert result["text"] == "Synthetic answer."
        assert result["tools"][1] == {"name": "ask_hermes", "ok": False, "verified": False}
        assert {t["function"]["name"] for t in seen[0]["tools"]} == EVERYDAY_TOOL_NAMES
        assert json.loads(seen[1]["messages"][-2]["content"])["local_time"] == "11:30"
        assert "not available in Everyday" in seen[1]["messages"][-1]["content"]

    asyncio.run(run())


def test_ollama_vision_wire_uses_inline_images_without_cloud_credentials():
    seen = []

    def handler(request):
        assert "authorization" not in request.headers
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "qwen2.5vl:3b"}]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        seen.append(body)
        assert body["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Synthetic image description."}}]})

    transport = httpx.MockTransport(handler)
    with LocalVisionClient("http://pc.local:11434/v1", "qwen2.5vl:3b", transport=transport) as client:
        assert client.health()["model_listed"] is True
        result = client.describe(b"synthetic-fixture", "Describe this fixture")
        assert result["answer"] == "Synthetic image description."
    assert len(seen) == 1


@pytest.mark.parametrize("status", [404, 503])
def test_ollama_failure_does_not_retry_another_provider(status):
    from test_everyday_tools import make_tools

    from companion.reachy_agent_broker import BrokerUnavailableError

    async def run():
        requests = []

        async def model(request):
            requests.append(await request.json())
            return web.json_response({"error": {"message": "synthetic model unavailable"}}, status=status)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", model)
        async with TestServer(app) as server, ClientSession() as http:
            provider = everyday(REACHY_EVERYDAY_URL=str(server.make_url("/v1")))
            with pytest.raises(BrokerUnavailableError, match=f"HTTP {status}"):
                await make_tools("http://127.0.0.1:1").answer(
                    "hello", http=http, provider=provider, key=provider.api_key(lambda _: ""),
                    device_id="fixture", generation=1,
                )
        assert len(requests) == 1

    asyncio.run(run())
