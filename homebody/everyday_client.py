"""Authenticated Everyday text turn with bounded robot-local tool callbacks."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from urllib.parse import urlparse, urlunparse

from websockets.sync.client import connect

from .config import AppConfig
from .hermes_client import AgentBrokerContext, HermesBridgeError


def everyday_url(bridge_url: str) -> str:
    parsed = urlparse(bridge_url)
    return urlunparse(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path.rstrip("/") + "/v1/everyday/session",
            "",
            "",
            "",
        )
    )


def everyday_session(
    config: AppConfig,
    transcript: str,
    *,
    session_id: str,
    context: AgentBrokerContext,
    local_tools: list[str],
    executor: Callable[[str, dict, str], dict],
    is_current: Callable[[], bool],
) -> str:
    deadline = time.monotonic() + 30
    seen = set()
    try:
        with connect(
            everyday_url(config.household_endpoint),
            additional_headers={
                "Authorization": f"Bearer {config.household_credential}",
                "X-Reachy-Device-Id": config.instance_id,
            },
            open_timeout=5,
            close_timeout=1,
            max_size=64_000,
        ) as socket:
            if not is_current():
                raise HermesBridgeError("Everyday turn was cancelled")
            socket.send(
                json.dumps(
                    {
                        "type": "everyday.start",
                        "text": transcript,
                        "language": config.language,
                        "session_id": session_id,
                        "agent_context": asdict(context),
                        "local_tools": local_tools,
                    }
                )
            )
            while time.monotonic() < deadline:
                if not is_current():
                    raise HermesBridgeError("Everyday turn was cancelled")
                try:
                    raw = socket.recv(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise HermesBridgeError("Invalid Everyday event")
                kind = payload.get("type")
                if kind == "everyday.answer":
                    text = payload.get("text")
                    if not isinstance(text, str) or not text.strip() or not is_current():
                        raise HermesBridgeError("Invalid or cancelled Everyday answer")
                    return text[:12_000].strip()
                if kind == "everyday.error":
                    raise HermesBridgeError("Everyday request could not be completed")
                if kind != "everyday.tool_call":
                    raise HermesBridgeError("Unsupported Everyday event")
                identifier, name, arguments = payload.get("call_id"), payload.get("name"), payload.get("arguments")
                if (
                    not isinstance(identifier, str)
                    or not 1 <= len(identifier) <= 128
                    or identifier in seen
                    or len(seen) >= 6
                    or name not in local_tools
                    or not isinstance(arguments, dict)
                ):
                    raise HermesBridgeError("Invalid or replayed Everyday tool call")
                seen.add(identifier)
                if not is_current():
                    raise HermesBridgeError("Everyday tool was cancelled")
                result = executor(name, arguments, identifier)
                # Power changes intentionally terminate the conversation. Never re-enter a new
                # voice generation merely to obtain an acknowledgement after Standby/Sleep.
                if not is_current():
                    if result.get("ok") and result.get("conversation_ended"):
                        return ""
                    raise HermesBridgeError("Everyday tool was cancelled")
                encoded = json.dumps({"type": "everyday.tool_result", "call_id": identifier, "result": result})
                if len(encoded.encode()) > 16_000:
                    raise HermesBridgeError("Everyday tool result exceeded its bound")
                socket.send(encoded)
        raise HermesBridgeError("Everyday connection ended before an answer")
    except HermesBridgeError:
        raise
    except Exception as exc:
        raise HermesBridgeError("Everyday tool connection is unavailable") from exc
    raise HermesBridgeError("Everyday request timed out")
