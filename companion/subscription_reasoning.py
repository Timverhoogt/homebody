"""Restricted text inference using Hermes-managed ChatGPT subscription authentication.

No agent loop, built-in tools, project context, API-key fallback or OAuth-token export.
The caller supplies only the Everyday messages and allowlisted tool schemas.
"""

from __future__ import annotations

import asyncio
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

    def __init__(self, model: str):
        if not model or len(model) > 128:
            raise ValueError("An explicit subscription model is required")
        self.models = {"everyday": model}

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
            client = _resolve_client(self.model("everyday"))
            # The installed Codex adapter has a hard total streaming deadline,
            # not merely a per-chunk socket timeout. It translates chat tool history.
            response = client.chat.completions.create(
                **body,
                timeout=timeout_seconds,
                extra_body={"reasoning": {"effort": "low"}},
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
