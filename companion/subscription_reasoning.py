"""Restricted text inference using Hermes-managed ChatGPT subscription authentication.

No agent loop, built-in tools, project context, API-key fallback or OAuth-token export.
The caller supplies only the Everyday messages and allowlisted tool schemas.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlparse

try:
    from companion.reachy_agent_broker import BrokerUnavailableError
except ImportError:
    from reachy_agent_broker import BrokerUnavailableError


def _resolve_client(model: str) -> Any:
    if "hermes_bootstrap" not in sys.modules:
        raise BrokerUnavailableError("Subscription reasoning requires the bootstrapped Hermes bridge entry")
    # Use the installed Hermes provider router, including its credential refresh and
    # Responses protocol adapter. Never use the auto/auxiliary fallback router.
    from agent.auxiliary_client import resolve_provider_client

    client, resolved = resolve_provider_client("openai-codex", model=model)
    if client is None or resolved != model:
        raise BrokerUnavailableError("ChatGPT subscription authentication is unavailable; no paid text API fallback")
    url = urlparse(str(client.base_url))
    if url.scheme != "https" or url.hostname != "chatgpt.com" or not url.path.startswith("/backend-api/codex"):
        raise BrokerUnavailableError("Refusing a non-subscription reasoning endpoint")
    return client


class SubscriptionTextProvider:
    name = "openai-codex"
    label = "ChatGPT subscription (Hermes OAuth; no paid text API fallback)"
    region = "OpenAI cloud"

    def __init__(self, model: str, *, models: dict[str, str] | None = None):
        if not model or len(model) > 128:
            raise ValueError("An explicit subscription model is required")
        self.models = {"everyday": model, **(models or {})}
        self.reasoning_effort = "none"

    chat_url = "subscription://chatgpt/chat/completions"
    is_openai = False

    def public_status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "region": self.region,
            "models": dict(self.models),
            "billing": "subscription",
            "reasoning_effort": "none",
        }

    def model(self, role: str) -> str:
        return self.models[role]

    def api_key(self, resolver: Any) -> str:
        # Existing bridge readiness interface. This is a non-secret marker, not an
        # OAuth token and never sent in an HTTP Authorization header.
        try:
            _resolve_client(self.model("everyday"))
        except Exception:
            return ""
        return "subscription-authenticated"

    def request(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(body)

    async def complete(self, body: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            requested_model = body.get("model")
            if not isinstance(requested_model, str) or requested_model not in self.models.values():
                raise BrokerUnavailableError("Unregistered subscription model")
            client = _resolve_client(requested_model)
            kwargs = {
                **body,
                "timeout": timeout_seconds,
                "extra_body": {"reasoning": {"enabled": False, "effort": "none"}},
            }
            if body.get("response_format", {}).get("type") == "json_schema":
                # The host still strictly validates each I Spy schema and safety rule.
                # Do not pretend that a chat response_format ignored by Codex enforces it.
                instruction = "Return only JSON matching this schema, without markdown: " + json.dumps(
                    body["response_format"]["json_schema"]["schema"]
                )
                messages = body.get("messages", [])
                systems = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
                kwargs["messages"] = [
                    {"role": "system", "content": "\n".join([*systems, instruction])},
                    *(m for m in messages if m.get("role") != "system"),
                ]
            adapter = client.chat.completions
            wire, _, _ = adapter._build_responses_kwargs(kwargs)
            if wire.get("reasoning", {}).get("effort") != "none":
                raise BrokerUnavailableError("This model cannot run with reasoning off; no reasoning-on fallback")
            # The installed Codex adapter has a hard total streaming deadline,
            # not merely a per-chunk socket timeout. It translates chat tool history.
            response = client.chat.completions.create(
                **kwargs,
            )
            message = response.choices[0].message
            calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in (message.tool_calls or [])
            ]
            return {"choices": [{"message": {"role": "assistant", "content": message.content, "tool_calls": calls}}]}

        try:
            return await asyncio.wait_for(asyncio.to_thread(run), timeout_seconds)
        except TimeoutError:
            raise
        except Exception as exc:
            # Never expose raw provider responses, credentials or silently change billing.
            raise BrokerUnavailableError(
                "ChatGPT subscription unavailable (authentication, quota or provider failure). "
                "No paid text API fallback was used."
            ) from exc


class _SubscriptionResponse:
    def __init__(self, provider, body, timeout):
        self.provider, self.body, self.timeout = provider, body, timeout
        self.status = 200
        self.payload = None

    async def __aenter__(self):
        from aiohttp import web

        try:
            self.payload = await self.provider.complete(self.body, timeout_seconds=self.timeout)
        except BrokerUnavailableError as exc:
            raise web.HTTPServiceUnavailable(text=str(exc)) from exc
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.payload


class SubscriptionHttp:
    """Route the bridge's existing bounded text/vision calls to subscription inference.

    Moderation, speech, HA and native-session HTTP retain their real HTTP transport.
    This does not add tools or change any Kids validators or lifecycle locks.
    """

    def __init__(self, http, provider):
        self.http, self.provider = http, provider

    def __getattr__(self, name):
        return getattr(self.http, name)

    def post(self, url, **kwargs):
        if url != self.provider.chat_url:
            return self.http.post(url, **kwargs)
        body = kwargs.get("json")
        if not isinstance(body, dict):
            raise BrokerUnavailableError("Subscription requests require a bounded JSON request")
        timeout = kwargs.get("timeout")
        total = getattr(timeout, "total", timeout) or 20.0
        return _SubscriptionResponse(self.provider, body, min(float(total), 20.0))
