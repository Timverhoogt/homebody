"""Synchronous Reachy client for the bridge's GPT-Live (preview) relay.

Reuses the Realtime session's bounded, audio-shedding event queue. Audio is sent only after
the bridge forwards ``session.started``; samples are whole little-endian PCM16 at 24 kHz.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from dataclasses import asdict
from urllib.parse import urlparse, urlunparse

import numpy as np
from websockets.sync.client import connect

from .everyday_runtime import configured_local_tools
from .realtime_client import RealtimeBridgeError, RealtimeBridgeSession, RealtimeEvent

_START_TIMEOUT_SECONDS = 15.0


def live_url(bridge_url: str) -> str:
    parsed = urlparse(bridge_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/") + "/v1/live", "", "", ""))


class LiveBridgeSession(RealtimeBridgeSession):
    """One GPT-Live session through the bridge; no OpenAI credential on the robot."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._carry = b""
        self.started = threading.Event()

    def start(self) -> None:
        try:
            self._socket = connect(
                live_url(self.config.bridge_url),
                additional_headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "X-Reachy-Device-Id": self.config.instance_id,
                },
                open_timeout=10,
                close_timeout=3,
                ping_interval=20,
                max_size=2 * 1024 * 1024,
            )
            self._socket.send(json.dumps({
                "type": "session.start",
                "model": self.config.live_model,
                "voice": self.config.live_voice,
                "timeout_seconds": self.config.live_session_timeout_seconds,
                "assistant_mode": self.assistant_mode,
                "local_tools": configured_local_tools(self.config) if self.assistant_mode == "everyday" else [],
                "language": self.config.language,
                "agent_context": asdict(self.agent_context) if self.agent_context is not None else {},
            }))
        except Exception as exc:
            self.close()
            raise RealtimeBridgeError(f"Could not open Live session: {exc}") from exc
        self._receiver = threading.Thread(target=self._receive_loop, name="reachy-live-events", daemon=True)
        self._receiver.start()

    def wait_started(self, timeout: float = _START_TIMEOUT_SECONDS) -> list[RealtimeEvent]:
        """Block until the bridge confirms ``session.started``; return any events seen meanwhile."""
        deadline = time.monotonic() + timeout
        seen: list[RealtimeEvent] = []
        while time.monotonic() < deadline:
            try:
                item = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            if item.type == "session.started":
                self.started.set()
                return seen
            if item.type in {"bridge.error", "session.closed"}:
                raise RealtimeBridgeError(str(item.payload.get("error") or "Live session was not accepted"))
            seen.append(item)
        raise RealtimeBridgeError("Live session did not start in time")

    @staticmethod
    def pcm16(samples_24k: np.ndarray) -> bytes:
        clipped = np.clip(np.asarray(samples_24k, dtype=np.float32), -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2", copy=False).tobytes()

    def send_audio(self, samples_24k: np.ndarray) -> None:
        if self._socket is None:
            raise RealtimeBridgeError("Live session is not connected")
        if not self.started.is_set():
            return  # never stream before the provider accepted the session
        pcm = self.pcm16(samples_24k)
        if not pcm:
            return
        try:
            self._socket.send(json.dumps({
                "type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode("ascii"),
            }))
        except Exception as exc:
            raise RealtimeBridgeError(f"Could not stream microphone audio: {exc}") from exc

    def audio_samples(self, event: RealtimeEvent) -> np.ndarray:  # type: ignore[override]
        """Decode one output delta, carrying an odd trailing byte so samples never shift."""
        encoded = str(event.payload.get("delta") or "")
        if not encoded:
            return np.empty(0, dtype=np.float32)
        data = self._carry + base64.b64decode(encoded)
        cut = len(data) - (len(data) % 2)
        self._carry = data[cut:]
        return np.frombuffer(data[:cut], dtype="<i2").astype(np.float32) / 32768.0

    def send_everyday_result(self, call_id: str, result: dict[str, object]) -> None:
        if self._socket is None or self._closed.is_set():
            return
        payload = json.dumps({"type": "everyday.tool_result", "call_id": call_id, "result": result})
        if len(payload.encode()) > 16_000:
            raise RealtimeBridgeError("Everyday tool result exceeded its bound")
        self._socket.send(payload)

    def close(self) -> None:
        self._closed.set()
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.send(json.dumps({"type": "session.close"}))
            except Exception:
                pass
            try:
                socket.close()
            except Exception:
                pass
        if self._receiver is not None and self._receiver is not threading.current_thread():
            self._receiver.join(timeout=1.0)
