"""Canonical household catalogue: adapters change wire shape, never tool authority.

Robot schemas remain in the stdlib-only manifest; host extensions retain their
existing definitions. No Hermes runtime, provider, credentials or device access.
"""
from __future__ import annotations

from typing import Any

try:
    from .everyday_host import host_specs
    from .everyday_manifest import local_tool_specs
except ImportError:
    from everyday_host import host_specs
    from everyday_manifest import local_tool_specs

def _function(name: str, description: str, properties: dict[str, object], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def realtime_tool_specs(*, home: bool = True) -> list[dict[str, Any]]:
    """Realtime/Responses-shaped function tools for Everyday."""
    names = {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 8}
    tools = [
        _function(
            "get_current_time",
            "Return the actual current local date and time from the household host clock.",
            {},
            [],
        ),
        _function(
            "get_weather",
            "Return the configured local weather source for now, today or tomorrow, with its observation time.",
            {"when": {"type": "string", "enum": ["now", "today", "tomorrow"]}},
            ["when"],
        ),
    ]
    if home:
        tools += [
            _function(
                "get_home_temperature",
                "Read configured indoor room temperatures and compute their current average in Celsius. "
                "Empty names selects the configured room sensors. Excludes unavailable/stale readings; "
                "disclose partial coverage. Not a historical average, outdoor weather, or device temperature.",
                {"names": names},
                ["names"],
            ),
            _function(
                "home_status",
                "Read the current state of allowlisted home devices by spoken name. Empty list reads all.",
                {"names": names},
                ["names"],
            ),
            _function(
                "home_control",
                "Turn one allowlisted light or switch on or off by its spoken name and verify the new state.",
                {
                    "name": {"type": "string", "maxLength": 60},
                    "action": {"type": "string", "enum": ["turn_on", "turn_off"]},
                },
                ["name", "action"],
            ),
            _function("home_undo", "Undo the last verified device change from this conversation.", {}, []),
        ]
    return tools + host_specs(_function, home=home)


def chat_tool_specs(*, home: bool = True) -> list[dict[str, Any]]:
    """The same tools in chat-completions shape (OpenAI, OpenRouter and compatible routers)."""
    return [
        {"type": "function", "function": {k: v for k, v in spec.items() if k != "type"}}
        for spec in realtime_tool_specs(home=home)
    ]


def household_specs(*, local_names=(), home: bool = True, host: bool = True) -> list[dict[str, Any]]:
    """Fresh canonical definitions, filtered by the adapter's current authority."""
    return (realtime_tool_specs(home=home) if host else []) + local_tool_specs(local_names)


def mcp_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": s["name"], "description": s["description"], "inputSchema": s["parameters"]} for s in specs]
