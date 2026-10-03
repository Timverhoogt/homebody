from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

_SPEC = importlib.util.spec_from_file_location(
    "llm_provider_check", Path(__file__).parents[1] / "tools" / "llm_provider_check.py"
)
provider_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provider_check)  # type: ignore[union-attr]


def fake_router(seen: list[dict[str, Any]], *, reasoning: bool = False) -> web.Application:
    """An OpenAI-compatible stand-in that answers the bridge's requests like a capable router."""

    async def models(request: web.Request) -> web.Response:
        tags = ["Instruct", "Tools"] + (["Reasoning"] if reasoning else [])
        return web.json_response(
            {
                "data": [
                    {
                        "id": "eu-model",
                        "supported_features": ["tools", "json_mode"],
                        "tags": tags,
                        "providers": ["scaleway"],
                    }
                ]
            }
        )

    def reply(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> web.Response:
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return web.json_response({"choices": [{"message": message, "finish_reason": "stop"}]})

    async def chat(request: web.Request) -> web.Response:
        if request.headers["Authorization"] != "Bearer eu-key":
            # LLMrouter.eu reports a bad key as HTTP 500 with an auth_error type.
            error = {"message": "Virtual Key expected", "type": "auth_error", "code": "500"}
            return web.json_response({"error": error}, status=500)
        body = await request.json()
        seen.append(body)
        schema = (body.get("response_format") or {}).get("json_schema", {}).get("name")
        if reasoning and body.get("max_tokens", 10_000) <= 60:
            return web.json_response(
                {
                    "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 40}},
                }
            )
        prompt = json.dumps(body["messages"])
        if schema == "ispy_match":
            return reply(json.dumps({"match": "mug" in prompt}))
        if schema == "ispy_player_object_guess":
            return reply(json.dumps({"guess": "cup"}))
        capabilities = {
            "id": "c1",
            "type": "function",
            "function": {"name": "get_agent_capabilities", "arguments": "{}"},
        }
        if body.get("tool_choice") == "required":
            return reply(None, [capabilities])
        if schema == "reachy_agent_answer":
            if not any(message.get("role") == "tool" for message in body["messages"]):
                return reply(None, [capabilities])
            answer = {
                "text": "I can check my status and help at home.",
                "status": "answered",
                "used_capabilities": ["get_agent_capabilities"],
            }
            return reply(json.dumps(answer))
        return reply("Owls can turn their heads very far!")

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    return app


def run_check(
    monkeypatch: pytest.MonkeyPatch, *, reasoning: bool = False, key: str = "eu-key", argv: list[str] | None = None
):  # type: ignore[no-untyped-def]
    seen: list[dict[str, Any]] = []

    async def main() -> int:
        server = TestServer(fake_router(seen, reasoning=reasoning))
        await server.start_server()
        try:
            monkeypatch.setenv("REACHY_LLM_PROVIDER", "cortecs")
            monkeypatch.setenv("REACHY_LLM_URL", str(server.make_url("/v1")))
            monkeypatch.setenv("REACHY_LLM_MODEL", "eu-model")
            monkeypatch.setenv("CORTECS_API_KEY", key)
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            return await provider_check.run_checks(
                provider_check.argparse.Namespace(
                    profile=None, photo=None, only=None, **dict(arg.split("=") for arg in argv or [])
                )
            )
        finally:
            await server.close()

    return asyncio.run(main()), seen


def test_every_bridge_request_shape_passes_against_a_capable_router(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, seen = run_check(monkeypatch)

    out = capsys.readouterr().out
    assert code == 0, out
    for line in ("✓ ispy judging", "✓ ispy guessing", "✓ agent planning", "✓ agent answer"):
        assert line in out
    assert "· kids chat: needs OPENAI_API_KEY" in out and "pass --photo" in out
    # The real bridge requests went out, with Cortecs' EU routing flag and no OpenAI-only fields.
    assert all(body["eu_native"] is True and "store" not in body for body in seen)
    assert any(body.get("tool_choice") == "required" for body in seen)
    assert any(body.get("tools") and body.get("response_format") for body in seen)


def test_reasoning_models_that_run_out_of_tokens_get_a_clear_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _seen = run_check(monkeypatch, reasoning=True)

    out = capsys.readouterr().out
    assert code == 1
    assert "! ispy model: eu-model is a reasoning model" in out
    assert "✗ ispy judging" in out and "cut off by the token limit after 40 reasoning tokens" in out
    assert "REACHY_ISPY_MODEL" in out
    assert "✓ agent answer" in out  # larger budgets still work


def test_missing_key_and_bad_settings_fail_fast(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REACHY_LLM_PROVIDER", "llmrouter")
    monkeypatch.delenv("REACHY_LLM_MODEL", raising=False)
    for name in ("REACHY_AGENT_MODEL", "REACHY_KIDS_MODEL", "REACHY_ISPY_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert provider_check.main([]) == 1
    assert "needs model names" in capsys.readouterr().out

    monkeypatch.setenv("REACHY_LLM_MODEL", "some-model")
    monkeypatch.delenv("LLMROUTER_API_KEY", raising=False)
    assert provider_check.main([]) == 1
    assert "no API key: set LLMROUTER_API_KEY" in capsys.readouterr().out


def test_a_refused_key_stops_with_the_providers_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, seen = run_check(monkeypatch, key="wrong-key")

    out = capsys.readouterr().out
    assert code == 1 and seen == []
    assert "✗ API key: Cortecs refused the key (HTTP 500) Virtual Key expected" in out
    assert "ispy judging" not in out
