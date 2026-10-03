"""OpenClaw as an agent backend for the Reachy bridge, alongside or instead of Hermes Agent.

OpenClaw's Gateway serves an OpenAI-compatible ``/v1/chat/completions`` endpoint (disabled by
default; enable ``gateway.http.endpoints.chatCompletions``). The ``model`` field names an *agent*
(``openclaw/<agentId>``), and a Gateway token is an owner-level operator credential. Voice from
a room is untrusted input, so the bridge:

* only ever targets agents on an explicit allowlist (``reachy`` by default), never whatever
  agent id the app or a caller sends;
* refuses the owner's primary agent (``main``, ``default``, bare ``openclaw``) unless the operator
  opts in, because the bridge cannot inspect an OpenClaw agent's tool policy the way it checks
  Hermes' ``/v1/toolsets``. Restrict the ``reachy`` agent's tools in ``openclaw.json`` instead;
* never forwards ``x-openclaw-*`` override headers, so a caller cannot switch the backend model
  or session namespace;
* keeps the Gateway token on the bridge host. Reachy never sees it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession

OPENCLAW_DEFAULT_URL = "http://127.0.0.1:18789"
OPENCLAW_PRIMARY_AGENT_IDS = frozenset({"main", "default"})
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_RE = re.compile(r"[^A-Za-z0-9:._-]")
_FORWARDED_FIELDS = ("messages", "temperature", "max_tokens", "max_completion_tokens", "top_p", "stop")


class OpenClawConfigError(ValueError):
    """The OpenClaw backend settings are unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class OpenClawConfig:
    url: str
    token: str
    agents: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        url: str,
        token: str,
        agents: str | list[str] | tuple[str, ...],
        allow_primary_agent: bool = False,
    ) -> OpenClawConfig:
        base = url.strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OpenClawConfigError("The OpenClaw Gateway URL must be an absolute http(s) URL")
        if not token.strip():
            raise OpenClawConfigError("Set OPENCLAW_GATEWAY_TOKEN (or OPENCLAW_GATEWAY_PASSWORD) for the bridge")
        names = [part.strip() for part in (agents.split(",") if isinstance(agents, str) else agents)]
        names = [name for name in names if name]
        if not names:
            raise OpenClawConfigError("List at least one OpenClaw agent id for Reachy, e.g. reachy")
        for name in names:
            if not _AGENT_ID_RE.fullmatch(name):
                raise OpenClawConfigError(f"Invalid OpenClaw agent id: {name!r}")
            if name.lower() in OPENCLAW_PRIMARY_AGENT_IDS and not allow_primary_agent:
                raise OpenClawConfigError(
                    f"Refusing to route Reachy to the OpenClaw '{name}' agent. Create a dedicated agent with "
                    "restricted tools (see companion/README.md), or set REACHY_OPENCLAW_ALLOW_PRIMARY_AGENT=1."
                )
        return cls(url=base, token=token.strip(), agents=tuple(dict.fromkeys(names)))

    @classmethod
    def from_env(cls, *, url: str | None = None, agents: str | None = None, token: str = "") -> OpenClawConfig:
        return cls.build(
            url=url or os.getenv("OPENCLAW_GATEWAY_URL", OPENCLAW_DEFAULT_URL),
            token=token or os.getenv("OPENCLAW_GATEWAY_TOKEN", "") or os.getenv("OPENCLAW_GATEWAY_PASSWORD", ""),
            agents=agents if agents is not None else os.getenv("REACHY_OPENCLAW_AGENTS", "reachy"),
            allow_primary_agent=os.getenv("REACHY_OPENCLAW_ALLOW_PRIMARY_AGENT", "").strip() == "1",
        )


class OpenClawBackend:
    name = "openclaw"
    label = "OpenClaw"
    tool_name = "ask_openclaw"

    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config

    @property
    def targets(self) -> list[str]:
        return [f"openclaw/{agent}" for agent in self.config.agents]

    @staticmethod
    def claims(model: str) -> bool:
        """Whether an app-selected model id names an OpenClaw agent."""
        lowered = model.strip().lower()
        return lowered == "openclaw" or lowered.startswith(("openclaw/", "openclaw:"))

    def target_for(self, model: str) -> str:
        """Map any requested model onto an allowlisted agent target; unknown ids get the first agent."""
        requested = model.strip()
        for separator in ("/", ":"):
            prefix = f"openclaw{separator}"
            if requested.lower().startswith(prefix):
                agent = requested[len(prefix) :]
                if agent in self.config.agents:
                    return f"openclaw/{agent}"
        return self.targets[0]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json"}

    @staticmethod
    def session_user(session_id: str) -> str:
        """A stable OpenAI ``user`` value: OpenClaw derives one agent session per conversation from it."""
        cleaned = _SESSION_RE.sub("", session_id)[:120]
        return f"reachy:{cleaned or 'default'}"

    async def health(self, http: ClientSession) -> dict[str, Any]:
        """Check the Gateway answers and that the allowlisted agents exist."""
        try:
            async with http.get(f"{self.config.url}/v1/models", headers=self._headers()) as response:
                if response.status != 200:
                    return {"ok": False, "error": f"OpenClaw Gateway answered HTTP {response.status}"}
                payload = await response.json(content_type=None)
        except Exception as exc:
            return {"ok": False, "error": f"OpenClaw Gateway is not reachable: {type(exc).__name__}"}
        listed = {str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)}
        missing = [target for target in self.targets if target not in listed]
        if missing:
            return {"ok": False, "error": f"OpenClaw agent not found: {', '.join(missing)}"}
        return {"ok": True}

    def model_entries(self) -> list[dict[str, Any]]:
        return [{"id": target, "object": "model", "owned_by": "openclaw"} for target in self.targets]

    def chat_payload(self, payload: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        """Rebuild the request from known-safe fields only; the agent target is always allowlisted."""
        body = {key: payload[key] for key in _FORWARDED_FIELDS if key in payload}
        body["model"] = self.target_for(str(payload.get("model") or ""))
        body["stream"] = False
        body["user"] = self.session_user(session_id)
        return body

    async def chat(self, http: ClientSession, payload: dict[str, Any], *, session_id: str) -> tuple[int, bytes, str]:
        body = self.chat_payload(payload, session_id=session_id)
        async with http.post(f"{self.config.url}/v1/chat/completions", json=body, headers=self._headers()) as upstream:
            return upstream.status, await upstream.read(), upstream.content_type or "application/json"

    async def answer(
        self,
        http: ClientSession,
        text: str,
        *,
        model: str,
        system_prompt: str,
        session_id: str,
    ) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        }
        body = self.chat_payload(payload, session_id=session_id)
        async with http.post(f"{self.config.url}/v1/chat/completions", json=body, headers=self._headers()) as response:
            result = await response.json(content_type=None)
            if response.status != 200:
                error = result.get("error") if isinstance(result, dict) else result
                raise RuntimeError(f"OpenClaw request failed: {error}")
            return str(result["choices"][0]["message"]["content"] or "").strip()
