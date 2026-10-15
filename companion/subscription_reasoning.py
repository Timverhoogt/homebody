"""Restricted text inference using Hermes-managed ChatGPT subscription authentication.

No agent loop, built-in tools, project context, API-key fallback or OAuth-token export.
The caller supplies only the Everyday messages and allowlisted tool schemas.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from typing import Any
from urllib.parse import urlparse

try:
    from companion.reachy_agent_broker import BrokerUnavailableError
except ImportError:
    from reachy_agent_broker import BrokerUnavailableError

# Native Structured Outputs (Responses ``text.format``). The installed Hermes Codex adapter drops chat
# ``response_format`` and forwards only ``service_tier``/``reasoning`` from ``extra_body``, so the schema
# is added at a Homebody-owned, per-request adapter boundary and checked on the final wire.
_FORMAT_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Keywords in the documented strict subset that Homebody schemas use. ``minLength``/``maxLength`` are not
# in that subset for strings, so they are left off the wire; the bridge still enforces them on every reply.
_WIRE_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minimum", "maximum", "minItems", "maxItems", "description",
})
_HOST_ONLY_KEYWORDS = frozenset({"minLength", "maxLength"})


def _native_schema(node: object) -> dict[str, Any]:
    """Project a Homebody JSON schema onto the strict wire subset; raise on anything else."""
    if not isinstance(node, dict):
        raise ValueError("schema node must be an object")
    unknown = set(node) - _WIRE_KEYWORDS - _HOST_ONLY_KEYWORDS
    if unknown:
        raise ValueError(f"unsupported strict schema keyword(s): {sorted(unknown)}")
    wire: dict[str, Any] = {}
    for key, value in node.items():
        if key in _HOST_ONLY_KEYWORDS:
            continue
        if key == "properties":
            if not isinstance(value, dict):
                raise ValueError("properties must be an object")
            wire[key] = {name: _native_schema(child) for name, child in value.items()}
        elif key == "items":
            wire[key] = _native_schema(value)
        else:
            wire[key] = copy.deepcopy(value)
    if node.get("type") == "object":
        properties = node.get("properties")
        if (
            node.get("additionalProperties") is not False
            or not isinstance(properties, dict)
            or not isinstance(node.get("required"), list)
            or set(node["required"]) != set(properties)
        ):
            raise ValueError("strict objects need additionalProperties false and every property required")
    return wire


def native_text_format(response_format: object) -> dict[str, Any] | None:
    """Chat ``response_format`` json_schema -> Responses ``text.format``; None when no schema was asked."""
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        if isinstance(response_format, dict) and response_format:
            raise ValueError("only json_schema response formats are supported")
        return None
    spec = response_format.get("json_schema")
    if (
        not isinstance(spec, dict)
        or spec.get("strict") is not True
        or not isinstance(spec.get("name"), str)
        or not _FORMAT_NAME.fullmatch(spec["name"])
    ):
        raise ValueError("json_schema needs strict true and a valid name")
    return {"type": "json_schema", "name": spec["name"], "strict": True, "schema": _native_schema(spec.get("schema"))}


class _WireCheckedResponses:
    def __init__(self, responses: Any, text: dict[str, Any]) -> None:
        self._responses, self._text = responses, text

    def create(self, **kwargs: Any) -> Any:
        # The last point before the SDK serializes the request: the exact format must be on it.
        if kwargs.get("text") != self._text or "text" in (kwargs.get("extra_body") or {}):
            raise BrokerUnavailableError("Structured output could not be enforced on the subscription wire")
        return self._responses.create(**kwargs)


class _WireCheckedClient:
    """Per-request view of the shared OpenAI client; everything but ``responses.create`` delegates."""

    def __init__(self, client: Any, text: dict[str, Any]) -> None:
        self._homebody_client = client
        self.responses = _WireCheckedResponses(client.responses, text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._homebody_client, name)


def _structured_adapter(adapter: Any, text_format: dict[str, Any]) -> Any:
    """A fresh per-request subclass instance of the installed Codex adapter that adds ``text.format``.

    Fails closed unless the resolved adapter is the installed ``_CodexCompletionsAdapter``; the shared
    adapter, its class and the shared client are never modified.
    """
    try:
        from agent.auxiliary_client import _CodexCompletionsAdapter
    except ImportError as exc:
        raise BrokerUnavailableError("Structured output needs the installed Hermes Codex adapter") from exc
    if not isinstance(adapter, _CodexCompletionsAdapter):
        raise BrokerUnavailableError("Structured output cannot be enforced by this subscription adapter")
    text = {"format": copy.deepcopy(text_format)}

    class _StructuredCodexAdapter(type(adapter)):  # type: ignore[misc, valid-type]
        def _build_responses_kwargs(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], str, Any]:
            resp_kwargs, model, timeout = super()._build_responses_kwargs(kwargs)
            if "text" in resp_kwargs:
                raise BrokerUnavailableError("Unexpected adapter text configuration; refusing to merge formats")
            return {**resp_kwargs, "text": copy.deepcopy(text)}, model, timeout

    return _StructuredCodexAdapter(_WireCheckedClient(adapter._client, text), adapter._model)


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
            try:
                text_format = native_text_format(body.get("response_format"))
            except ValueError as exc:
                raise BrokerUnavailableError("Structured output schema is outside the enforceable subset") from exc
            adapter = client.chat.completions
            if text_format is not None:
                # Native strict Structured Outputs on the Responses wire, plus the schema as an
                # instruction. The host still strictly validates each reply and safety rule.
                instruction = "Return only JSON matching this schema, without markdown: " + json.dumps(
                    body["response_format"]["json_schema"]["schema"]
                )
                messages = body.get("messages", [])
                systems = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
                kwargs["messages"] = [
                    {"role": "system", "content": "\n".join([*systems, instruction])},
                    *(m for m in messages if m.get("role") != "system"),
                ]
                adapter = _structured_adapter(adapter, text_format)
            wire, _, _ = adapter._build_responses_kwargs(kwargs)
            if wire.get("reasoning", {}).get("effort") != "none":
                raise BrokerUnavailableError("This model cannot run with reasoning off; no reasoning-on fallback")
            if text_format is not None and wire.get("text") != {"format": text_format}:
                raise BrokerUnavailableError("Structured output could not be enforced on the subscription wire")
            # The installed Codex adapter has a hard total streaming deadline,
            # not merely a per-chunk socket timeout. It translates chat tool history.
            response = adapter.create(**kwargs)
            choice = response.choices[0]
            message = choice.message
            if text_format is not None:
                # A refusal, truncation, tool call or non-object reply is never a schema answer.
                content = message.content
                if (
                    getattr(choice, "finish_reason", None) not in (None, "stop")
                    or message.tool_calls
                    or not isinstance(content, str)
                    or not isinstance(json.loads(content), dict)
                ):
                    raise BrokerUnavailableError("Structured subscription reply was refused or invalid")
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
