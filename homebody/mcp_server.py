"""Let any MCP-capable agent use Reachy as a body, through Homebody's own safety rules.

Homebody serves a small Model Context Protocol endpoint (Streamable HTTP, stateless JSON
responses) at ``/mcp`` on its settings server. Hermes Agent, OpenClaw, Claude and other MCP
clients can then ask Reachy to say something, show an emotion, describe what it sees, or report
whether it can do so right now.

The endpoint runs *inside* the always-on companion, because the Reachy daemon runs only one app
at a time. Every tool goes through the same checks as the phone UI and voice: Meeting, Sleep,
privacy, Kids Mode, motor torque and the announcement queue. Tools are deliberately high level:
there is no raw joint control, no image download and no way to wake Reachy for a gesture.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .config import AppConfig
from .robot_tools import EMOTIONS

_LOGGER = logging.getLogger(__name__)

SERVER_NAME = "homebody"
SERVER_VERSION = "1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
MAX_ANNOUNCEMENT_CHARS = 500
MAX_QUESTION_CHARS = 300
# Generous for an agent, tight enough that a runaway loop cannot flood the room.
CALLS_PER_MINUTE = 30
ANNOUNCEMENTS_PER_TEN_MINUTES = 6

INSTRUCTIONS = (
    "Homebody is a Reachy Mini robot companion in someone's home. Use it as a gentle physical "
    "presence: speak short messages aloud, show an emotion, or describe what its camera sees. "
    "Call get_status first when unsure. If a tool reports that Reachy is busy, resting, private or "
    "in a child session, respect that and do not retry in a loop."
)


def new_token() -> str:
    """A fresh bearer token for MCP clients; only its SHA-256 is stored."""
    return "hb_" + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(provided: str, digest: str) -> bool:
    if not provided or not digest:
        return False
    return hmac.compare_digest(token_digest(provided), digest)


class _ToolRefused(Exception):
    """A safety rule or the robot's state blocks this call; reported to the agent, not raised."""


def _text_result(text: str, *, structured: dict[str, Any] | None = None, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


class McpServer:
    """JSON-RPC handling for the MCP endpoint; transport and auth live in the web route."""

    def __init__(
        self,
        runtime: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runtime = runtime
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()
        self._announcements: deque[float] = deque()
        self._stats: dict[str, Any] = {"calls": 0, "refused": 0, "last_tool": "", "last_result": ""}

    # -- protocol -------------------------------------------------------------------------------

    def handle(self, message: object, config: AppConfig) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; notifications return None."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid JSON-RPC request")
        method = message.get("method")
        request_id = message.get("id")
        if "id" not in message:
            return None  # notifications such as notifications/initialized need no answer
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Missing method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            return self._ok(request_id, self._initialize(params))
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": self.tools(config)})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name not in {tool["name"] for tool in self.tools(config)}:
                return self._error(request_id, -32602, f"Unknown tool: {name}")
            return self._ok(request_id, self.call(str(name), arguments, config))
        return self._error(request_id, -32601, f"Method not found: {method}")

    @staticmethod
    def negotiate_version(requested: object) -> str:
        return requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": self.negotiate_version(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Homebody (Reachy Mini)", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    @staticmethod
    def _ok(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # -- tools ----------------------------------------------------------------------------------

    @staticmethod
    def tools(config: AppConfig) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "name": "get_status",
                "title": "Check Reachy",
                "description": "Whether Reachy is awake, resting or private, and which actions it can take right now.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "announce",
                "title": "Say something aloud",
                "description": (
                    "Speak a short message aloud through Reachy, for example a reminder. It waits for any "
                    "conversation in progress. Refused in Meeting, Sleep, privacy mode and child sessions."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": MAX_ANNOUNCEMENT_CHARS},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
            },
            {
                "name": "express_emotion",
                "title": "Show an emotion",
                "description": (
                    "Play one short, bounded emotion with Reachy's head and antennas. Only while Reachy is "
                    "already Awake; it never wakes Reachy for this."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"emotion": {"type": "string", "enum": list(EMOTIONS)}},
                    "required": ["emotion"],
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
            },
        ]
        if config.mcp_vision_enabled:
            tools.append(
                {
                    "name": "look_and_describe",
                    "title": "Describe what Reachy sees",
                    "description": (
                        "Answer a question about Reachy's current camera view. A local vision model answers on "
                        "the owner's network; only the text answer is returned, never the image."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "minLength": 1, "maxLength": MAX_QUESTION_CHARS},
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
                }
            )
        return tools

    def call(self, name: str, arguments: dict[str, Any], config: AppConfig) -> dict[str, Any]:
        try:
            self._admit(name)
            runtime = self._runtime()
            if runtime is None or not getattr(runtime, "control_ready", False):
                raise _ToolRefused("Reachy is still starting up. Try again in a minute.")
            if name == "get_status":
                result = self._get_status(runtime, config)
            elif name == "announce":
                result = self._announce(runtime, arguments)
            elif name == "express_emotion":
                result = self._express_emotion(runtime, arguments)
            else:
                result = self._look_and_describe(runtime, arguments, config)
            self._record(name, "ok")
            return result
        except _ToolRefused as exc:
            self._record(name, "refused")
            return _text_result(str(exc), error=True)
        except (RuntimeError, ValueError) as exc:
            # The runtime's own safety gates explain themselves; pass that on.
            self._record(name, "refused")
            return _text_result(str(exc), error=True)
        except Exception:
            _LOGGER.exception("MCP tool %s failed", name)
            self._record(name, "error")
            return _text_result("Reachy could not complete that request.", error=True)

    def _admit(self, name: str) -> None:
        now = self._clock()
        with self._lock:
            while self._calls and now - self._calls[0] > 60.0:
                self._calls.popleft()
            if len(self._calls) >= CALLS_PER_MINUTE:
                raise _ToolRefused("Too many requests. Wait a minute before asking Reachy again.")
            if name == "announce":
                while self._announcements and now - self._announcements[0] > 600.0:
                    self._announcements.popleft()
                if len(self._announcements) >= ANNOUNCEMENTS_PER_TEN_MINUTES:
                    raise _ToolRefused("Reachy has spoken several messages recently. Try again in a few minutes.")
                self._announcements.append(now)
            self._calls.append(now)

    def _record(self, name: str, outcome: str) -> None:
        with self._lock:
            self._stats["calls"] += 1
            if outcome != "ok":
                self._stats["refused"] += 1
            self._stats["last_tool"] = name
            self._stats["last_result"] = outcome
        _LOGGER.info("MCP tool %s: %s", name, outcome)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    # -- tool implementations --------------------------------------------------------------------

    @staticmethod
    def _state(runtime: Any) -> dict[str, Any]:
        status = runtime.status()
        kids = status.get("kids_mode") if isinstance(status.get("kids_mode"), dict) else {}
        presence = status.get("presence") if isinstance(status.get("presence"), dict) else {}
        privacy_active = getattr(runtime, "privacy_active", None)
        return {
            "power_mode": str(status.get("power_mode") or "unknown"),
            "privacy": bool(privacy_active()) if callable(privacy_active) else False,
            "activity": str(status.get("state") or "unknown"),
            "child_session": bool(kids.get("active") or kids.get("locked")),
            # None when presence sensing is off: the agent should not read "nobody home" into it.
            "someone_present": presence.get("level") in {"present", "attentive"} if presence.get("enabled") else None,
        }

    @staticmethod
    def _vision_unavailable_reason(config: AppConfig) -> str:
        if not config.mcp_vision_enabled:
            return "The owner has not allowed agents to ask what Reachy sees."
        if not config.local_vision_enabled or not config.camera_enabled:
            return "Looking needs the local vision model and On-demand camera turned on in Homebody settings."
        return ""

    @staticmethod
    def _private_reason(state: dict[str, Any]) -> str:
        """Why Reachy is not listening, speaking or looking right now; empty when it is available."""
        if state["child_session"]:
            return "Reachy is in a supervised child session and does not take agent requests now."
        if state["privacy"]:
            return "Reachy is in privacy mode: it is not listening, speaking or looking."
        if state["power_mode"] in {"meeting", "sleep"}:
            return f"Reachy is in {state['power_mode'].title()} mode: it is not listening, speaking or looking."
        return ""

    def _get_status(self, runtime: Any, config: AppConfig) -> dict[str, Any]:
        state = self._state(runtime)
        mode = state["power_mode"]
        blocked = self._private_reason(state)
        why_not: dict[str, str] = {}
        if blocked:
            why_not = {"announce": blocked, "express_emotion": blocked, "look": blocked}
        else:
            if mode != "awake":
                why_not["express_emotion"] = "Emotions work only while Reachy is Awake; it won't wake for one."
            vision = self._vision_unavailable_reason(config)
            if vision:
                why_not["look"] = vision
        summary = {
            "power_mode": mode,
            "privacy_mode": state["privacy"],
            "activity": state["activity"].replace("_", " "),
            "someone_present": state["someone_present"],
            "can_announce": "announce" not in why_not,
            "can_express_emotion": "express_emotion" not in why_not,
            "can_look": "look" not in why_not,
            # Lets the agent explain an unavailable action instead of guessing.
            "why_not": why_not,
        }
        if blocked:
            text = blocked
        else:
            text = f"Reachy is {mode} and {summary['activity']}."
            text += "".join(f" {reason}" for key, reason in why_not.items())
        return _text_result(text, structured=summary)

    def _refuse_if_unavailable(self, runtime: Any) -> None:
        reason = self._private_reason(self._state(runtime))
        if reason:
            raise _ToolRefused(reason)

    def _announce(self, runtime: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(arguments.get("text") or "").split())
        if not text:
            raise _ToolRefused("Give Reachy something to say.")
        if len(text) > MAX_ANNOUNCEMENT_CHARS:
            raise _ToolRefused(f"Keep messages under {MAX_ANNOUNCEMENT_CHARS} characters.")
        self._refuse_if_unavailable(runtime)
        result = runtime.queue_announcement(text)
        depth = int(result.get("queue_depth") or 1)
        note = "Reachy will say it now." if depth <= 1 else f"Reachy will say it after {depth - 1} earlier message(s)."
        return _text_result(note, structured={"queued": True, "queue_depth": depth})

    def _express_emotion(self, runtime: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        emotion = str(arguments.get("emotion") or "").strip().lower()
        if emotion not in EMOTIONS:
            raise _ToolRefused(f"Unknown emotion. Choose one of: {', '.join(EMOTIONS)}.")
        self._refuse_if_unavailable(runtime)
        mode = self._state(runtime)["power_mode"]
        if mode != "awake":
            raise _ToolRefused(
                f"Reachy is in {mode.title()}; it only shows emotions while Awake and won't wake up for one."
            )
        runtime.queue_manual_robot_action("emotion", emotion, wake_if_standby=False)
        return _text_result(f"Reachy is showing {emotion}.", structured={"emotion": emotion})

    def _look_and_describe(self, runtime: Any, arguments: dict[str, Any], config: AppConfig) -> dict[str, Any]:
        question = " ".join(str(arguments.get("question") or "").split())[:MAX_QUESTION_CHARS]
        if not question:
            raise _ToolRefused("Ask Reachy a question about what it sees.")
        self._refuse_if_unavailable(runtime)  # Kids Mode and privacy explain themselves first
        vision = self._vision_unavailable_reason(config)
        if vision:
            raise _ToolRefused(vision)
        seen = runtime.describe_camera_view(question, config=config)
        answer = str(seen.get("answer") or "")
        return _text_result(answer, structured={"answer": answer, "model": seen.get("model")})
