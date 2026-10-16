"""Ephemeral, identity-free presence and attention state for Agent 0.6.

Occupancy transitions and source health are tracked separately: a sensor bridge that forwards
changes only cannot be aged out by timestamp alone, so heartbeats refresh ``source_health`` without
becoming a new arrival, and a silent bridge makes occupancy ``unknown`` rather than ``empty``.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

PresenceSource = Literal["home_assistant", "trusted_sensor", "voice", "gesture"]
PresenceLevel = Literal["away", "present", "attentive"]
PresenceTransition = Literal["arrived", "left", "updated", "heartbeat"]

_ALLOWED_SOURCES = frozenset({"home_assistant", "trusted_sensor", "voice", "gesture"})
# A forwarding bridge polls every few seconds; after this silence the room is "unknown", not empty.
DEFAULT_STALE_AFTER_SECONDS = 45.0


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

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self._clock = clock
        self._stale_after = max(5.0, float(stale_after_seconds))
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
        # Source health is independent of the last occupancy transition.
        self._source_seen_at: float | None = None
        self._source_unavailable_reason = ""

    def observe(self, observation: PresenceObservation, *, heartbeat: bool = False) -> PresenceTransition:
        """Record one signal and return what changed.

        ``heartbeat`` means "the source is alive and still reports this occupancy": it refreshes
        source health and never counts as an arrival unless the reported state actually differs.
        """
        now = self._clock()
        with self._lock:
            self._source_seen_at = now
            self._source_unavailable_reason = ""
            was_occupied = self._level != "away"
            if heartbeat and observation.occupied == was_occupied:
                self._source = observation.source
                return "heartbeat"
            if not observation.occupied:
                self._level = "away"
                self._source = observation.source
                self._direction_degrees = None
                self._confidence = float(observation.confidence)
                self._observed_at = now
                self._last_outcome = "away"
                return "left"
            self._level = "attentive" if observation.attentive else "present"
            self._source = observation.source
            self._direction_degrees = (
                round(float(observation.direction_degrees), 1) if observation.direction_degrees is not None else None
            )
            self._confidence = round(float(observation.confidence), 3)
            self._observed_at = now
            self._last_outcome = "observed"
            return "updated" if was_occupied else "arrived"

    def mark_unavailable(self, source: PresenceSource, reason: str) -> None:
        """The source (bridge/HA) reported it cannot read occupancy; the last level is kept but unknown."""
        with self._lock:
            self._source = source
            self._source_unavailable_reason = reason.strip()[:64] or "unavailable"

    def clear(self, reason: str) -> None:
        with self._lock:
            self._level = "away"
            self._source = None
            self._direction_degrees = None
            self._confidence = 0.0
            self._observed_at = 0.0
            self._source_seen_at = None
            self._source_unavailable_reason = ""
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

    def _source_health_unlocked(self, now: float) -> str:
        if self._source_seen_at is None:
            return "none"
        if self._source_unavailable_reason:
            return "unavailable"
        if now - self._source_seen_at > self._stale_after:
            return "stale"
        return "fresh"

    def public_status(self, *, enabled: bool) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            observed_seconds_ago = None
            if self._observed_at > 0.0:
                observed_seconds_ago = max(0, int(now - self._observed_at))
            acknowledged_seconds_ago = None
            if self._last_acknowledged_at > 0.0:
                acknowledged_seconds_ago = max(0, int(now - self._last_acknowledged_at))
            health = self._source_health_unlocked(now)
            if health == "fresh":
                occupancy = "empty" if self._level == "away" else "present"
            else:
                occupancy = "unknown"
            seen_seconds_ago = max(0, int(now - self._source_seen_at)) if self._source_seen_at is not None else None
            return {
                "enabled": bool(enabled),
                "level": self._level if enabled else "away",
                "source": self._source if enabled else None,
                "direction_degrees": self._direction_degrees if enabled else None,
                "confidence": self._confidence if enabled else 0.0,
                "observed_seconds_ago": observed_seconds_ago if enabled else None,
                "occupancy": occupancy if enabled else "unknown",
                "source_health": health if enabled else "none",
                "source_seen_seconds_ago": seen_seconds_ago if enabled else None,
                "source_unavailable_reason": self._source_unavailable_reason if enabled else "",
                "stale_after_seconds": int(self._stale_after),
                "last_outcome": self._last_outcome,
                "acknowledged_seconds_ago": acknowledged_seconds_ago,
                "acknowledgements": self._acknowledgements,
                "speech_enabled": False,
            }
