"""Synchronous Reachy client for the authenticated Realtime bridge."""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import numpy as np
from websockets.sync.client import ClientConnection, connect

from .config import AppConfig
from .hermes_client import AgentBrokerContext

_LOGGER = logging.getLogger(__name__)
_EVENT_QUEUE_LIMIT = 512


def _is_audio_delta(event: RealtimeEvent) -> bool:
    """Audio deltas are the only events safe to drop under backpressure."""
    return event.type.endswith(".delta") and "audio" in event.type and "transcript" not in event.type


class RealtimeBridgeError(RuntimeError):
    """Raised when the Realtime bridge cannot establish or maintain a session."""


@dataclass(slots=True)
class RealtimeEvent:
    type: str
    payload: dict[str, Any]


def realtime_url(bridge_url: str) -> str:
    parsed = urlparse(bridge_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    # Keep any reverse-proxy path prefix, exactly like the HTTP client's f"{bridge_url}/v1/...".
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/") + "/v1/realtime", "", "", ""))


class RealtimeBridgeSession:
    """Keep WebSocket receive work off the robot's audio loop."""

    def __init__(
        self,
        config: AppConfig,
        *,
        agent_context: AgentBrokerContext | None = None,
        agent_request_id: str = "",
    ) -> None:
        self.config = config
        self.agent_context = agent_context
        self.agent_request_id = agent_request_id
        self._socket: ClientConnection | None = None
        self._events: queue.Queue[RealtimeEvent] = queue.Queue(maxsize=_EVENT_QUEUE_LIMIT)
        self._receiver: threading.Thread | None = None
        self._closed = threading.Event()

    def start(self) -> None:
        try:
            self._socket = connect(
                realtime_url(self.config.bridge_url),
                additional_headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "X-Reachy-Device-Id": self.config.instance_id,
                },
                open_timeout=10,
                close_timeout=3,
                ping_interval=20,
                max_size=2 * 1024 * 1024,
            )
            self._socket.send(
                json.dumps(
                    {
                        "type": "session.start",
                        "model": self.config.realtime_model,
                        "voice": self.config.realtime_voice,
                        "reasoning_effort": self.config.realtime_reasoning_effort,
                        "camera_enabled": self.config.camera_enabled,
                        "robot_tools_enabled": self.config.robot_tools_enabled,
                        "agent_tools_enabled": self.config.agent_tools_enabled,
                        "power_tools_enabled": self.config.power_tools_enabled,
                        "agent_model": self.config.model,
                        "session_id": f"reachy-realtime-{self.config.instance_id}",
                        "system_prompt": self.config.system_prompt,
                        "agent_context": asdict(self.agent_context) if self.agent_context is not None else {},
                        "agent_request_id": self.agent_request_id,
                    }
                )
            )
        except Exception as exc:
            self.close()
            raise RealtimeBridgeError(f"Could not open Realtime session: {exc}") from exc
        self._receiver = threading.Thread(target=self._receive_loop, name="reachy-realtime-events", daemon=True)
        self._receiver.start()

    def _receive_loop(self) -> None:
        assert self._socket is not None
        try:
            for message in self._socket:
                if not isinstance(message, str):
                    continue
                try:
                    payload = json.loads(message)
                except ValueError:
                    _LOGGER.warning("Ignoring a malformed Realtime bridge message")
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning("Ignoring a non-object Realtime bridge message")
                    continue
                self._enqueue(RealtimeEvent(str(payload.get("type") or "unknown"), payload))
        except Exception as exc:
            if not self._closed.is_set():
                self._enqueue(RealtimeEvent("bridge.error", {"type": "bridge.error", "error": str(exc)}))

    def _enqueue(self, event: RealtimeEvent) -> None:
        """Queue an event; under backpressure shed audio deltas, never control or error events."""
        try:
            self._events.put(event, timeout=0.5)
            return
        except queue.Full:
            pass
        with self._events.mutex:
            pending = self._events.queue
            victim = next((item for item in pending if _is_audio_delta(item)), None)
            if victim is None and _is_audio_delta(event):
                return
            if victim is None:
                # Only control events are queued; keep the newest control state.
                victim = pending[0]
                _LOGGER.warning("Realtime event queue is full of control events; dropping the oldest")
            pending.remove(victim)
            pending.append(event)
            self._events.not_empty.notify()

    def send_audio(self, samples_24k: np.ndarray) -> None:
        if self._socket is None:
            raise RealtimeBridgeError("Realtime session is not connected")
        clipped = np.clip(samples_24k, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype("<i2", copy=False).tobytes()
        encoded = base64.b64encode(pcm).decode("ascii")
        try:
            self._socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": encoded}))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not stream microphone audio: {exc}") from exc

    def events(self) -> list[RealtimeEvent]:
        result: list[RealtimeEvent] = []
        while True:
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                return result

    @staticmethod
    def audio_samples(event: RealtimeEvent) -> np.ndarray:
        encoded = str(event.payload.get("delta") or "")
        if not encoded:
            return np.empty(0, dtype=np.float32)
        pcm = np.frombuffer(base64.b64decode(encoded), dtype="<i2")
        return pcm.astype(np.float32) / 32768.0

    def clear_output(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.send(json.dumps({"type": "output_audio_buffer.clear"}))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not clear Realtime output audio: {exc}") from exc

    def truncate_audio(self, item_id: str, audio_end_ms: int) -> None:
        """Tell a WebSocket Realtime session how much audio was actually played."""
        if self._socket is None or not item_id:
            return
        try:
            self._socket.send(
                json.dumps(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": item_id,
                        "content_index": 0,
                        "audio_end_ms": max(0, audio_end_ms),
                    }
                )
            )
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not truncate interrupted audio: {exc}") from exc

    def send_camera_frame(self, call_id: str, jpeg: bytes) -> None:
        """Attach one on-demand camera frame and complete its tool call."""
        if self._socket is None:
            raise RealtimeBridgeError("Realtime session is not connected")
        if not call_id:
            raise RealtimeBridgeError("Camera tool call did not include a call ID")
        if not jpeg or len(jpeg) > 1_000_000:
            raise RealtimeBridgeError("Camera JPEG must be between 1 byte and 1 MB")
        image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        events = [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": '{"ok":true,"image_attached":true,"capture_count":1}',
                },
            },
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "This is the single current Reachy camera frame requested by the user. "
                                "Use it to answer the latest visual question."
                            ),
                        },
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                },
            },
            {"type": "response.create"},
        ]
        try:
            for event in events:
                self._socket.send(json.dumps(event))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not send camera frame: {exc}") from exc

    def send_camera_error(self, call_id: str, message: str) -> None:
        """Complete a failed camera tool call so the model can explain it."""
        if self._socket is None or not call_id:
            return
        try:
            self._socket.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": f"Camera capture failed: {message}",
                        },
                    }
                )
            )
            self._socket.send(json.dumps({"type": "response.create"}))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not report camera failure: {exc}") from exc

    def send_tool_result(
        self,
        call_id: str,
        result: dict[str, object],
        *,
        continue_response: bool = True,
    ) -> None:
        """Complete one robot-local function call and optionally continue the response."""
        if self._socket is None:
            raise RealtimeBridgeError("Realtime session is not connected")
        if not call_id:
            raise RealtimeBridgeError("Robot tool call did not include a call ID")
        try:
            self._socket.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result),
                        },
                    }
                )
            )
            if continue_response:
                self._socket.send(json.dumps({"type": "response.create"}))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not send robot tool result: {exc}") from exc

    def close(self) -> None:
        self._closed.set()
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.send(json.dumps({"type": "session.stop"}))
            except Exception:
                pass
            try:
                socket.close()
            except Exception:
                pass
        if self._receiver is not None and self._receiver is not threading.current_thread():
            self._receiver.join(timeout=1.0)
