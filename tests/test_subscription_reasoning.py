"""Subscription inference keeps Everyday's narrow authority and fails closed on billing/auth."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from test_everyday_tools import make_tools

from companion import subscription_reasoning as sub
from companion.everyday_tools import EVERYDAY_TOOL_NAMES
from companion.llm_providers import LLMProviderConfigError, everyday_provider_from_env
from companion.reachy_agent_broker import BrokerUnavailableError, BrokerValidationError


class NoPaidHttp:
    def post(self, *args, **kwargs):
        pytest.fail("subscription turn attempted an API-key HTTP request")


def fake_client(monkeypatch, name="get_current_time"):
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        calls = (
            [SimpleNamespace(id="clock1", function=SimpleNamespace(name=name, arguments="{}"))]
            if len(requests) == 1
            else []
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None if calls else "The result is ready.", tool_calls=calls)
                )
            ]
        )

    monkeypatch.setattr(
        sub,
        "_resolve_client",
        lambda _: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=create, _build_responses_kwargs=lambda kw: ({"reasoning": {"effort": "none"}}, "test", 20)
                )
            )
        ),
    )
    return requests


@pytest.mark.parametrize("name", ["get_current_time", "terminal", "read_file"])
def test_subscription_tool_loop_has_only_home_tools_and_no_paid_http(monkeypatch, name):
    requests = fake_client(monkeypatch, name)
    p = sub.SubscriptionTextProvider("subscription-test")
    tools = make_tools("http://127.0.0.1:1")
    result = asyncio.run(
        tools.answer(
            "What time is it?", http=NoPaidHttp(), provider=p, key=p.api_key(None), device_id="fixture", generation=1
        )
    )
    assert {t["function"]["name"] for t in requests[0]["tools"]} == EVERYDAY_TOOL_NAMES
    assert requests[0]["store"] is False
    assert requests[0]["timeout"] <= 20
    assert requests[0]["extra_body"]["reasoning"] == {"effort": "none", "enabled": False}
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert result["source"] == {"kind": "everyday", "provider": "openai-codex", "model": "subscription-test"}
    assert result["tools"][0]["ok"] is (name == "get_current_time")


def test_subscription_failure_is_explicit_no_fallback(monkeypatch):
    def unavailable(_):
        raise RuntimeError("provider response with private details")

    monkeypatch.setattr(sub, "_resolve_client", unavailable)
    p = sub.SubscriptionTextProvider("subscription-test")
    assert p.api_key(None) == ""
    with pytest.raises(BrokerUnavailableError, match="No paid text API fallback") as error:
        asyncio.run(p.complete({"messages": []}, timeout_seconds=1))
    assert "private details" not in str(error.value)


def test_subscription_refuses_paid_or_custom_endpoint(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_bootstrap", SimpleNamespace())
    resolver = SimpleNamespace(
        resolve_provider_client=lambda *a, **k: (SimpleNamespace(base_url="https://api.openai.com/v1"), "test")
    )
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", resolver)
    with pytest.raises(BrokerUnavailableError, match="non-subscription"):
        sub._resolve_client("test")


@pytest.mark.parametrize(
    "extra", [{}, {"REACHY_EVERYDAY_URL": "https://api.openai.com/v1", "REACHY_EVERYDAY_MODEL": "test"}]
)
def test_subscription_configuration_requires_explicit_model_and_no_url(extra):
    env = {"REACHY_EVERYDAY_PROVIDER": "openai-codex", **extra}
    with pytest.raises(LLMProviderConfigError):
        everyday_provider_from_env(env.get)


def test_stale_subscription_result_cannot_call_tools(monkeypatch):
    fake_client(monkeypatch)
    p = sub.SubscriptionTextProvider("test")
    current = [True]

    async def late(*args, **kwargs):
        current[0] = False
        return {
            "choices": [
                {"message": {"tool_calls": [{"id": "x", "function": {"name": "home_control", "arguments": "{}"}}]}}
            ]
        }

    monkeypatch.setattr(p, "complete", late)
    with pytest.raises(BrokerValidationError, match="stale_session"):
        asyncio.run(
            make_tools("http://127.0.0.1:1").answer(
                "turn on",
                http=NoPaidHttp(),
                provider=p,
                key="marker",
                device_id="fixture",
                generation=1,
                is_current=lambda: current[0],
            )
        )
