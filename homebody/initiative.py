"""Deterministic, silent initiative eligibility policy for Agent 0.6."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .adaptation import CATEGORIES, PreferenceLedger

InitiativeMode = Literal["quiet", "balanced", "engaged"]
InitiativeOutcome = Literal["remain_silent", "physical_acknowledgement", "offer_candidate"]
RequestedInitiative = Literal["physical_acknowledgement", "offer_candidate"]

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
_MODES = frozenset({"quiet", "balanced", "engaged"})
_REQUESTED_OUTCOMES = frozenset({"physical_acknowledgement", "offer_candidate"})


@dataclass(frozen=True, slots=True)
class InitiativeCandidate:
    """One sanitized opportunity; it cannot contain generated speech or raw context."""

    topic: str
    requested_outcome: RequestedInitiative
    confidence: float
    attentive: bool = False
    fingerprint: str = ""
    # Personal-adaptation category; "" keeps the candidate outside learned preferences.
    category: str = ""

    def __post_init__(self) -> None:
        if self.category and self.category not in CATEGORIES:
            raise ValueError("Unsupported initiative category")
        if not _TOPIC_RE.fullmatch(self.topic):
            raise ValueError("Initiative topic must be a bounded machine label")
        if self.requested_outcome not in _REQUESTED_OUTCOMES:
            raise ValueError("Unsupported initiative outcome")
        if isinstance(self.confidence, bool) or not math.isfinite(float(self.confidence)):
            raise ValueError("Initiative confidence must be finite")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Initiative confidence must be between 0 and 1")
        if not _FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise ValueError("Initiative fingerprint must be an opaque bounded label")


@dataclass(frozen=True, slots=True)
class InitiativeSettings:
    enabled: bool = False
    mode: InitiativeMode = "quiet"
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    hourly_budget: int = 2
    daily_budget: int = 6
    topic_cooldown_seconds: float = 1800.0
    duplicate_window_seconds: float = 300.0
    dismissal_backoff_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise ValueError("Unsupported initiative mode")
        _parse_clock_time(self.quiet_hours_start)
        _parse_clock_time(self.quiet_hours_end)
        if not 1 <= int(self.hourly_budget) <= 10:
            raise ValueError("Initiative hourly budget must be between 1 and 10")
        if not 1 <= int(self.daily_budget) <= 30:
            raise ValueError("Initiative daily budget must be between 1 and 30")
        if not 60.0 <= float(self.topic_cooldown_seconds) <= 86400.0:
            raise ValueError("Initiative topic cooldown must be between 60 and 86400 seconds")
        if not 30.0 <= float(self.duplicate_window_seconds) <= 3600.0:
            raise ValueError("Initiative duplicate window must be between 30 and 3600 seconds")
        if not 60.0 <= float(self.dismissal_backoff_seconds) <= 86400.0:
            raise ValueError("Initiative dismissal backoff must be between 60 and 86400 seconds")


@dataclass(frozen=True, slots=True)
class InitiativeDecision:
    token: int
    outcome: InitiativeOutcome
    reason: str
    topic: str
    mode: InitiativeMode


@dataclass(frozen=True, slots=True)
class _CommittedInitiative:
    monotonic_at: float
    local_day: str
    topic: str
    fingerprint: str


# Plain-language answers to "Why did Reachy do that?" for the latest decision.
_REASON_EXPLANATIONS = {
    "eligible": "Reachy found a high-confidence moment that fits your settings.",
    "committed": "Reachy took this initiative because it fit your settings and learned preferences.",
    "disabled": "Initiative is turned off.",
    "quiet_hours": "It is within your quiet hours.",
    "quiet_mode": "Quiet mode only allows attentive, silent acknowledgements.",
    "low_confidence": "The moment was not clear enough to act on.",
    "hourly_budget": "This hour's initiative budget is used up.",
    "daily_budget": "Today's initiative budget is used up.",
    "dismissal_backoff": "You recently declined this topic, so Reachy is backing off.",
    "topic_cooldown": "Reachy already brought this up recently.",
    "duplicate": "This was the same moment as one Reachy already handled.",
    "category_disabled": "You turned this kind of initiative off.",
    "snoozed": "You said “later”, so Reachy is waiting before trying again.",
    "learned_quiet_time": "You often declined this at this time of day, so Reachy stays quiet now.",
    "none": "Nothing has happened yet.",
}


def explain_reason(reason: str) -> str:
    return _REASON_EXPLANATIONS.get(
        reason, "Something else needed Reachy's attention, so it stayed silent."
    )


def _parse_clock_time(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if match is None:
        raise ValueError("Quiet hours must use HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("Quiet hours must use a valid 24-hour time")
    return hour * 60 + minute


def _inside_quiet_hours(now: datetime, start: str, end: str) -> bool:
    start_minute = _parse_clock_time(start)
    end_minute = _parse_clock_time(end)
    if start_minute == end_minute:
        return False
    current = now.hour * 60 + now.minute
    if start_minute < end_minute:
        return start_minute <= current < end_minute
    return current >= start_minute or current < end_minute


class InitiativePolicy:
    """Thread-safe policy state with bounded budgets, cooldowns, and backoff."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = datetime.now,
        preferences: PreferenceLedger | None = None,
    ) -> None:
        self.preferences = preferences or PreferenceLedger(wall_clock=wall_clock)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._generation = 0
        self._latest = InitiativeDecision(0, "remain_silent", "none", "none", "quiet")
        self._pending: dict[int, tuple[InitiativeCandidate, InitiativeSettings]] = {}
        self._committed: deque[_CommittedInitiative] = deque(maxlen=64)
        self._dismissed_until: dict[str, float] = {}
        self._dismissal_counts: dict[str, int] = {}

    def evaluate(
        self,
        candidate: InitiativeCandidate,
        settings: InitiativeSettings,
        *,
        suppression_reason: str = "",
    ) -> InitiativeDecision:
        with self._lock:
            now_mono = self._monotonic_clock()
            now_wall = self._wall_clock()
            self._prune(now_mono, now_wall)
            reason = self._suppression_reason(candidate, settings, now_mono, now_wall, suppression_reason)
            outcome: InitiativeOutcome = "remain_silent" if reason else candidate.requested_outcome
            self._generation += 1
            decision = InitiativeDecision(
                self._generation,
                outcome,
                reason or "eligible",
                candidate.topic,
                settings.mode,
            )
            self._latest = decision
            self._pending.clear()
            if outcome != "remain_silent":
                self._pending[decision.token] = (candidate, settings)
            return decision

    def commit(self, decision: InitiativeDecision) -> bool:
        with self._lock:
            pending = self._pending.pop(decision.token, None)
            if pending is None or decision != self._latest or decision.outcome == "remain_silent":
                return False
            candidate, _settings = pending
            now_mono = self._monotonic_clock()
            now_wall = self._wall_clock()
            self._committed.append(
                _CommittedInitiative(
                    monotonic_at=now_mono,
                    local_day=now_wall.date().isoformat(),
                    topic=candidate.topic,
                    fingerprint=candidate.fingerprint,
                )
            )
            self._latest = InitiativeDecision(
                decision.token,
                decision.outcome,
                "committed",
                decision.topic,
                decision.mode,
            )
            return True

    def cancel(self, decision: InitiativeDecision, reason: str) -> None:
        with self._lock:
            self._pending.pop(decision.token, None)
            if decision == self._latest:
                self._latest = InitiativeDecision(
                    decision.token,
                    "remain_silent",
                    reason.strip()[:64] or "cancelled",
                    decision.topic,
                    decision.mode,
                )

    def record_dismissal(self, topic: str, settings: InitiativeSettings, *, category: str = "") -> None:
        if not _TOPIC_RE.fullmatch(topic):
            raise ValueError("Initiative topic must be a bounded machine label")
        if category:
            self.preferences.record(category, "dismissed")
        with self._lock:
            count = min(self._dismissal_counts.get(topic, 0) + 1, 6)
            self._dismissal_counts[topic] = count
            delay = min(float(settings.dismissal_backoff_seconds) * (2 ** (count - 1)), 86400.0)
            self._dismissed_until[topic] = self._monotonic_clock() + delay

    def record_snoozed(self, category: str) -> None:
        """'Later' only delays the category; it is not a dismissal and adds no topic backoff."""
        self.preferences.record(category, "snoozed")

    def record_welcomed(self, topic: str, *, category: str = "") -> None:
        if category:
            self.preferences.record(category, "welcomed")
        with self._lock:
            self._dismissal_counts.pop(topic, None)
            self._dismissed_until.pop(topic, None)

    def public_status(self, settings: InitiativeSettings) -> dict[str, object]:
        with self._lock:
            now_mono = self._monotonic_clock()
            now_wall = self._wall_clock()
            self._prune(now_mono, now_wall)
            current_day = now_wall.date().isoformat()
            hourly = sum(1 for item in self._committed if now_mono - item.monotonic_at < 3600.0)
            daily = sum(1 for item in self._committed if item.local_day == current_day)
            return {
                "enabled": settings.enabled,
                "mode": settings.mode,
                "quiet_hours_active": settings.quiet_hours_enabled
                and _inside_quiet_hours(now_wall, settings.quiet_hours_start, settings.quiet_hours_end),
                "latest_outcome": self._latest.outcome if settings.enabled else "remain_silent",
                "latest_reason": self._latest.reason if settings.enabled else "disabled",
                "latest_explanation": explain_reason(self._latest.reason if settings.enabled else "disabled"),
                "latest_topic": (
                    self._latest.topic if settings.enabled and self._latest.topic != "none" else None
                ),
                "initiatives_this_hour": hourly,
                "hourly_budget": settings.hourly_budget,
                "initiatives_today": daily,
                "daily_budget": settings.daily_budget,
                "speech_enabled": False,
                "preferences": self.preferences.public_status(),
            }

    def _suppression_reason(
        self,
        candidate: InitiativeCandidate,
        settings: InitiativeSettings,
        now_mono: float,
        now_wall: datetime,
        suppression_reason: str,
    ) -> str:
        if not settings.enabled:
            return "disabled"
        if suppression_reason:
            return suppression_reason.strip()[:64]
        if settings.quiet_hours_enabled and _inside_quiet_hours(
            now_wall, settings.quiet_hours_start, settings.quiet_hours_end
        ):
            return "quiet_hours"
        if candidate.category:
            learned = self.preferences.suppression_reason(candidate.category)
            if learned:
                return learned
        if settings.mode == "quiet" and candidate.requested_outcome == "offer_candidate":
            return "quiet_mode"
        if (
            settings.mode == "quiet"
            and candidate.requested_outcome == "physical_acknowledgement"
            and not candidate.attentive
        ):
            return "quiet_mode"
        minimum_confidence = {
            ("quiet", "physical_acknowledgement"): 0.95,
            ("balanced", "physical_acknowledgement"): 0.6,
            ("engaged", "physical_acknowledgement"): 0.4,
            ("balanced", "offer_candidate"): 0.8,
            ("engaged", "offer_candidate"): 0.6,
        }.get((settings.mode, candidate.requested_outcome), 1.1)
        if float(candidate.confidence) < minimum_confidence:
            return "low_confidence"
        current_day = now_wall.date().isoformat()
        if sum(1 for item in self._committed if now_mono - item.monotonic_at < 3600.0) >= int(
            settings.hourly_budget
        ):
            return "hourly_budget"
        if sum(1 for item in self._committed if item.local_day == current_day) >= int(settings.daily_budget):
            return "daily_budget"
        dismissed_until = self._dismissed_until.get(candidate.topic, 0.0)
        if dismissed_until > now_mono:
            return "dismissal_backoff"
        topic_cooldown = float(settings.topic_cooldown_seconds)
        if candidate.category:
            # Learned preference only stretches or shortens timing; budgets above still cap volume.
            topic_cooldown *= self.preferences.cooldown_multiplier(candidate.category)
        for item in reversed(self._committed):
            age = now_mono - item.monotonic_at
            if item.topic == candidate.topic and age < topic_cooldown:
                return "topic_cooldown"
            if candidate.fingerprint and item.fingerprint == candidate.fingerprint and age < float(
                settings.duplicate_window_seconds
            ):
                return "duplicate"
        return ""

    def _prune(self, now_mono: float, now_wall: datetime) -> None:
        current_day = now_wall.date().isoformat()
        while self._committed and (
            now_mono - self._committed[0].monotonic_at > 86400.0
            and self._committed[0].local_day != current_day
        ):
            self._committed.popleft()
        self._dismissed_until = {
            topic: until for topic, until in self._dismissed_until.items() if until > now_mono
        }
