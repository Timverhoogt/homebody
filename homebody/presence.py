"""Ephemeral, identity-free presence and attention state for Agent 0.6."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PresenceSource = Literal["home_assistant", "trusted_sensor", "voice", "gesture"]
PresenceLevel = Literal["away", "present", "attentive"]

_ALLOWED_SOURCES = frozenset({"home_assistant", "trusted_sensor", "voice", "gesture"})


@dataclass(frozen=True, slots=True)
class PresenceObservation:
    """One bounded observation. It deliberately cannot carry identity or raw sensor data."""

    source: PresenceSource
    occupied: bool
    attentive: bool = False
    direction_degrees: float | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.source not in _ALLOWED_SOURCES:
            raise ValueError("Unsupported presence source")
        if self.attentive and not self.occupied:
            raise ValueError("Attention requires occupied=true")
        if isinstance(self.confidence, bool) or not math.isfinite(float(self.confidence)):
            raise ValueError("Presence confidence must be a finite number")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Presence confidence must be between 0 and 1")
        if self.direction_degrees is not None:
            if isinstance(self.direction_degrees, bool) or not math.isfinite(float(self.direction_degrees)):
                raise ValueError("Presence direction must be a finite number")
            if not -60.0 <= float(self.direction_degrees) <= 60.0:
                raise ValueError("Presence direction must be between -60 and 60 degrees")


class PresenceState:
    """Thread-safe process-local state with acknowledgement cooldown accounting."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._level: PresenceLevel = "away"
        self._source: PresenceSource | None = None
        self._direction_degrees: float | None = None
        self._confidence = 0.0
        self._observed_at = 0.0
        self._cooldown_started_at = 0.0
        self._last_acknowledged_at = 0.0
        self._last_outcome = "none"
        self._acknowledgements = 0

    def observe(self, observation: PresenceObservation) -> None:
        now = self._clock()
        with self._lock:
            if not observation.occupied:
                self._level = "away"
                self._source = observation.source
                self._direction_degrees = None
                self._confidence = float(observation.confidence)
                self._observed_at = now
                self._last_outcome = "away"
                return
            self._level = "attentive" if observation.attentive else "present"
            self._source = observation.source
            self._direction_degrees = (
                round(float(observation.direction_degrees), 1)
                if observation.direction_degrees is not None
                else None
            )
            self._confidence = round(float(observation.confidence), 3)
            self._observed_at = now
            self._last_outcome = "observed"

    def clear(self, reason: str) -> None:
        with self._lock:
            self._level = "away"
            self._source = None
            self._direction_degrees = None
            self._confidence = 0.0
            self._observed_at = 0.0
            self._last_outcome = reason.strip()[:64] or "cleared"

    def acknowledgement_due(self, cooldown_seconds: float) -> bool:
        with self._lock:
            if self._level == "away":
                return False
            if self._cooldown_started_at <= 0.0:
                return True
            return self._clock() - self._cooldown_started_at >= float(cooldown_seconds)

    def reserve_acknowledgement(self) -> None:
        """Start cooldown when a cancellable acknowledgement is accepted by the action queue."""
        with self._lock:
            self._cooldown_started_at = self._clock()
            self._last_outcome = "acknowledgement_queued"

    def complete_acknowledgement(self, *, succeeded: bool, reason: str = "") -> None:
        with self._lock:
            if succeeded:
                self._last_acknowledged_at = self._clock()
                self._last_outcome = "acknowledged_silently"
                self._acknowledgements += 1
            else:
                self._last_outcome = reason.strip()[:64] or "acknowledgement_cancelled"

    def record_acknowledgement(self) -> None:
        """Record an acknowledgement completed outside the queue (used by deterministic tests)."""
        self.reserve_acknowledgement()
        self.complete_acknowledgement(succeeded=True)

    def record_suppression(self, reason: str) -> None:
        with self._lock:
            self._last_outcome = reason.strip()[:64] or "suppressed"

    def public_status(self, *, enabled: bool) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            observed_seconds_ago = None
            if self._observed_at > 0.0:
                observed_seconds_ago = max(0, int(now - self._observed_at))
            acknowledged_seconds_ago = None
            if self._last_acknowledged_at > 0.0:
                acknowledged_seconds_ago = max(0, int(now - self._last_acknowledged_at))
            return {
                "enabled": bool(enabled),
                "level": self._level if enabled else "away",
                "source": self._source if enabled else None,
                "direction_degrees": self._direction_degrees if enabled else None,
                "confidence": self._confidence if enabled else 0.0,
                "observed_seconds_ago": observed_seconds_ago if enabled else None,
                "last_outcome": self._last_outcome,
                "acknowledged_seconds_ago": acknowledged_seconds_ago,
                "acknowledgements": self._acknowledgements,
                "speech_enabled": False,
            }
