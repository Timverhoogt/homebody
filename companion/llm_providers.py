"""Where the bridge's own text-model calls go: OpenAI, or an EU router such as Cortecs or LLMrouter.eu.

The bridge calls a chat model itself for Agent Mode (planning and the bounded tool loop), Kids
Mode chat, and I Spy (target selection, guess judging and player-turn guesses). By default those
calls go to OpenAI. ``REACHY_LLM_PROVIDER`` sends them to another OpenAI-compatible provider
instead, so a European deployment can keep that traffic with European providers.

Two things stay with OpenAI on purpose: the Realtime voice session (no router offers it) and
Kids Mode moderation (``omni-moderation``), which remains a hard safety boundary.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

PURPOSES = ("agent", "kids", "ispy")
_PURPOSE_ENV = {"agent": "REACHY_AGENT_MODEL", "kids": "REACHY_KIDS_MODEL", "ispy": "REACHY_ISPY_MODEL"}
_OPENAI_DEFAULTS = {"agent": "gpt-5-mini", "kids": "gpt-5-mini", "ispy": "gpt-4.1-mini"}
# OpenAI-only request fields that other providers may reject.
_OPENAI_ONLY_FIELDS = ("store", "reasoning_effort")


class LLMProviderConfigError(ValueError):
    """The text-model provider settings are incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class _Preset:
    label: str
    url: str
    key_names: tuple[str, ...]
    region: str
    token_field: str = "max_tokens"
    extras: dict[str, Any] = field(default_factory=dict)


PRESETS: dict[str, _Preset] = {
    "openai": _Preset("OpenAI", "https://api.openai.com/v1", ("OPENAI_API_KEY",), "US", "max_completion_tokens"),
    # eu_native keeps Cortecs routing to providers based and regulated in the EU.
    "cortecs": _Preset("Cortecs", "https://api.cortecs.ai/v1", ("CORTECS_API_KEY",), "EU", extras={"eu_native": True}),
    "llmrouter": _Preset("LLMrouter.eu", "https://proxy.llmrouter.eu/v1", ("LLMROUTER_API_KEY",), "EU"),
    "custom": _Preset("Custom provider", "", ("REACHY_LLM_API_KEY",), "custom"),
}


@dataclass(frozen=True, slots=True)
class TextModelProvider:
    name: str
    label: str
    base_url: str
    key_names: tuple[str, ...]
    region: str
    token_field: str
    extras: dict[str, Any]
    models: dict[str, str]

    @classmethod
    def from_env(cls, env: Callable[[str], str] | None = None) -> TextModelProvider:
        source = env or (lambda name: os.getenv(name, ""))

        def get(name: str) -> str:
            return str(source(name) or "")

        name = (get("REACHY_LLM_PROVIDER") or "openai").strip().lower()
        preset = PRESETS.get(name)
        if preset is None:
            raise LLMProviderConfigError(f"REACHY_LLM_PROVIDER must be one of {', '.join(PRESETS)}")
        base = (get("REACHY_LLM_URL").strip() or preset.url).rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LLMProviderConfigError("Set REACHY_LLM_URL to the provider's OpenAI-compatible /v1 URL")
        shared = get("REACHY_LLM_MODEL").strip()
        models: dict[str, str] = {}
        missing: list[str] = []
        for purpose in PURPOSES:
            model = get(_PURPOSE_ENV[purpose]).strip() or shared
            if not model and name == "openai":
                model = _OPENAI_DEFAULTS[purpose]
            if not model:
                missing.append(_PURPOSE_ENV[purpose])
            models[purpose] = model
        if missing:
            # Model names differ per router; never guess one.
            raise LLMProviderConfigError(
                f"{preset.label} needs model names: set REACHY_LLM_MODEL or {', '.join(missing)}"
            )
        extras = dict(preset.extras)
        if name == "cortecs" and get("REACHY_CORTECS_EU_NATIVE").strip() == "0":
            extras.pop("eu_native", None)
        return cls(
            name=name,
            label=preset.label,
            base_url=base,
            key_names=preset.key_names,
            region=preset.region,
            token_field=preset.token_field,
            extras=extras,
            models=models,
        )

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def is_openai(self) -> bool:
        return self.name == "openai"

    def model(self, purpose: str) -> str:
        return self.models[purpose]

    def api_key(self, resolve: Callable[[str], str]) -> str:
        for key_name in self.key_names:
            if value := resolve(key_name):
                return value
        return ""

    def request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Adapt an OpenAI-shaped request body for this provider."""
        adapted = dict(body)
        tokens = adapted.pop("max_completion_tokens", None)
        if tokens is not None:
            adapted[self.token_field] = tokens
        if not self.is_openai:
            for field_name in _OPENAI_ONLY_FIELDS:
                adapted.pop(field_name, None)
        adapted.update(self.extras)
        return adapted

    def public_status(self) -> dict[str, object]:
        return {"name": self.name, "label": self.label, "region": self.region, "models": dict(self.models)}
