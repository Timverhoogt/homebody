import asyncio
from types import SimpleNamespace

import pytest
from test_subscription_reasoning import fake_client

from companion import subscription_reasoning as sub
from companion.llm_providers import TextModelProvider
from companion.native_subscription_policy import locked_workspace_options
from companion.reachy_agent_broker import BrokerUnavailableError


def test_all_roles_resolve_subscription_models():
    env = {
        "REACHY_LLM_PROVIDER": "openai-codex",
        "REACHY_LLM_MODEL": "sol",
        "REACHY_KIDS_MODEL": "luna",
        "REACHY_ISPY_MODEL": "luna",
    }
    provider = TextModelProvider.from_env(lambda key: env.get(key, ""))
    assert provider.name == "openai-codex"
    assert provider.models == {"everyday": "sol", "agent": "sol", "kids": "luna", "ispy": "luna"}
    assert provider.public_status()["reasoning_effort"] == "none"


def test_kids_and_ispy_model_posts_use_subscription_not_paid_http(monkeypatch):
    requests = fake_client(monkeypatch)
    p = sub.SubscriptionTextProvider("sol", models={"kids": "luna", "ispy": "luna"})
    forwarded = []

    class Http:
        def post(self, url, **kwargs):
            forwarded.append(url)
            if url.endswith("chat/completions"):
                pytest.fail("paid text HTTP attempted")
            return "real-moderation-transport"

    proxy = sub.SubscriptionHttp(Http(), p)

    async def run():
        for role in ("kids", "ispy"):
            async with proxy.post(
                p.chat_url, json={"model": p.model(role), "messages": [], "store": False}
            ) as response:
                assert response.status == 200
                assert "choices" in await response.json()

    asyncio.run(run())
    assert forwarded == []
    assert all(r["model"] == "luna" and r["extra_body"]["reasoning"]["enabled"] is False for r in requests)
    assert proxy.post("https://api.openai.com/v1/moderations", json={}) == "real-moderation-transport"
    assert forwarded == ["https://api.openai.com/v1/moderations"]


def test_reasoning_off_fails_before_request_if_adapter_cannot_encode_none(monkeypatch):
    def never(**kwargs):
        pytest.fail("reasoning-on request sent")

    adapter = SimpleNamespace(
        create=never, _build_responses_kwargs=lambda kw: ({"reasoning": {"effort": "low"}}, "test", 20)
    )
    monkeypatch.setattr(sub, "_resolve_client", lambda _: SimpleNamespace(chat=SimpleNamespace(completions=adapter)))
    with pytest.raises(BrokerUnavailableError):
        asyncio.run(
            sub.SubscriptionTextProvider("test").complete({"model": "test", "messages": []}, timeout_seconds=1)
        )


def test_native_workspace_is_session_model_provider_and_reasoning_locked():
    options = locked_workspace_options(
        {"session_id": "homebody", "model_options": {"reasoning_effort": "high"}},
        session_id="homebody",
        model="gpt-6-sol",
    )
    assert options["confirmed_runtime_lock"] is True  # Hermes disables fallback on this contract
    assert options["requested_model"] == "gpt-6-sol"
    assert options["requested_provider"] == "openai-codex"
    assert options["model_options"]["reasoning"] == {"enabled": False, "effort": "none"}
    for bad in [
        {"session_id": "other"},
        {"session_id": "homebody", "requested_provider": "openai"},
        {"session_id": "homebody", "requested_model": "other"},
    ]:
        with pytest.raises(ValueError):
            locked_workspace_options(bad, session_id="homebody", model="gpt-6-sol")
