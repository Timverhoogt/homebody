"""Explicitly enabled, bounded adult conversation display; never a durable log."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable

_SECRET = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/-]+|(?:sk|key|token)[-_][a-z0-9_-]{8,}"
    r"|(?:password|api[_ -]?key|secret|token)\s*[:=]\s*\S+)"
)


class VoiceWorkspace:
    """A one-hour RAM lease. Clear invalidates all in-flight producers."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._events: deque[dict[str, object]] = deque(maxlen=120)
        self._generation = 0
        self._sequence = 0
        self._enabled = False
        self._expires = 0.0

    def _expire(self) -> None:
        if self._enabled and self._clock() >= self._expires:
            self.clear()

    def start(self) -> dict[str, object]:
        with self._lock:
            self._expire()
            if not self._enabled:
                self.clear()
                self._enabled = True
                self._expires = self._clock() + 3600
            return self.snapshot()

    def clear(self) -> dict[str, object]:
        with self._lock:
            self._generation += 1
            self._events.clear()
            self._enabled = False
            self._expires = 0.0
            return self.snapshot()

    def lease(self) -> int | None:
        with self._lock:
            self._expire()
            return self._generation if self._enabled else None

    def append(self, generation: int | None, role: str, text: str, *, key: str = "") -> None:
        with self._lock:
            self._expire()
            if not self._enabled or generation != self._generation:
                return
            if role not in {"user", "assistant", "activity"} or not text.strip():
                return
            # Redact before truncation, so a cropped token cannot leak a partial secret.
            text = _SECRET.sub("[redacted]", text[:16000]).strip()
            truncated = len(text) > 4000
            text = text[:4000]
            if key and any(event["key"] == key and event["role"] == role for event in self._events):
                return
            if role == "activity" and self._events:
                last = self._events[-1]
                if last["role"] == role and last["text"] == text:
                    return
            self._sequence += 1
            self._events.append({
                "id": self._sequence, "role": role, "text": text, "key": key,
                "timestamp": round(time.time(), 3), "truncated": truncated,
            })

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._expire()
            return {
                "enabled": self._enabled,
                "generation": self._generation,
                "events": [{k: v for k, v in event.items() if k != "key"} for event in self._events],
                "seconds_remaining": max(0, int(self._expires - self._clock())) if self._enabled else 0,
            }


class VoiceWorkspaceMixin:
    """Keep capture behind the same serialized privacy/Kids boundary as robot controls."""

    _kids_active: bool
    _kids_locked: bool
    _kids_lock: threading.RLock
    stop_event: threading.Event
    _privacy_requested: threading.Event
    _motor_transition_lock: threading.RLock
    _voice_workspace: VoiceWorkspace

    _effective_power_mode: Callable[[], str]

    def _workspace_allowed(self) -> bool:
        return not (
            self.stop_event.is_set() or self._kids_active or self._kids_locked or self._privacy_requested.is_set()
            or self._effective_power_mode() in {"meeting", "sleep"}
        )

    def workspace(self, action: str = "read") -> dict[str, object]:
        with self._motor_transition_lock, self._kids_lock:
            if not self._workspace_allowed():
                self._voice_workspace.clear()
                raise RuntimeError("Conversation display is unavailable in Kids or privacy modes")
            if action == "start":
                return self._voice_workspace.start()
            if action == "clear":
                return self._voice_workspace.clear()
            return self._voice_workspace.snapshot()

    def clear_native_workspace(self) -> None:
        """Revocation does not wait for network I/O under robot safety locks."""
        config = self.config_loader()
        if not config.api_key:
            return

        def revoke():
            client = self._new_bridge_client(config)
            try:
                client.native_workspace_action("stop")
            except Exception:
                import logging
                logging.getLogger(__name__).warning("Native Workspace stop could not be confirmed")
            finally:
                client.close()

        threading.Thread(target=revoke, name="native-workspace-clear", daemon=True).start()

    def _workspace_lease(self) -> int | None:
        with self._motor_transition_lock, self._kids_lock:
            if not self._workspace_allowed():
                self._voice_workspace.clear()
                return None
            return self._voice_workspace.lease()

    def _workspace_event(self, generation: int | None, role: str, text: str, *, key: str = "") -> None:
        with self._motor_transition_lock, self._kids_lock:
            if not self._workspace_allowed():
                self._voice_workspace.clear()
                return
            self._voice_workspace.append(generation, role, text, key=key)
