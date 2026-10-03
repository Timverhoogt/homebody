"""Bounded contextual-offer contract and response lifecycle for Agent 0.6."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

OfferSource = Literal["calendar", "reminder", "timer", "home_assistant", "weather", "project", "presentation"]
OfferResponse = Literal["yes", "no", "later", "unknown"]

_SOURCES = frozenset(
    {"calendar", "reminder", "timer", "home_assistant", "weather", "project", "presentation"}
)
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_EXPLANATIONS = {
    "calendar": "High-confidence calendar context",
    "reminder": "Active reminder context",
    "timer": "Active timer context",
    "home_assistant": "Allowlisted Home Assistant state",
    "weather": "Current weather context",
    "project": "Explicitly scoped project context",
    "presentation": "High-confidence presentation context",
}
_YES = frozenset({"yes", "yeah", "yep", "sure", "please", "please do", "go ahead", "ja", "graag", "doe maar"})
_NO = frozenset({"no", "nope", "no thanks", "nee", "liever niet"})
# "Not now" is about timing, not interest: it snoozes the category instead of dismissing it.
_LATER = frozenset({"later", "not now", "not right now", "maybe later", "nu niet", "straks", "later misschien"})


def _clean_text(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    clean = _SPACE_RE.sub(" ", value.strip())
    if not clean or len(clean) > maximum:
        raise ValueError(f"{field_name} must contain between 1 and {maximum} characters")
    if _URL_RE.search(clean):
        raise ValueError(f"{field_name} cannot contain a URL")
    if any(ord(character) < 32 for character in clean):
        raise ValueError(f"{field_name} cannot contain control characters")
    return clean


@dataclass(frozen=True, slots=True)
class ContextualOffer:
    """One allowlisted, already-rendered read-only offer from trusted local context."""

    source: OfferSource
    topic: str
    confidence: float
    fingerprint: str
    text: str
    accepted_text: str

    def __post_init__(self) -> None:
        if self.source not in _SOURCES:
            raise ValueError("Unsupported contextual offer source")
        if not _TOPIC_RE.fullmatch(self.topic):
            raise ValueError("Contextual offer topic must be a bounded machine label")
        if isinstance(self.confidence, bool) or not math.isfinite(float(self.confidence)):
            raise ValueError("Contextual offer confidence must be finite")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Contextual offer confidence must be between 0 and 1")
        if not _FINGERPRINT_RE.fullmatch(self.fingerprint):
            raise ValueError("Contextual offer fingerprint must be an opaque bounded label")
        clean_text = _clean_text(self.text, field_name="Contextual offer", maximum=180)
        if not clean_text.endswith("?") or any(mark in clean_text[:-1] for mark in ".?!"):
            raise ValueError("Contextual offer must be one concise question")
        if not 4 <= len(clean_text.split()) <= 30:
            raise ValueError("Contextual offer must be one concise question")
        clean_accepted = _clean_text(
            self.accepted_text,
            field_name="Contextual offer accepted text",
            maximum=240,
        )
        object.__setattr__(self, "text", clean_text)
        object.__setattr__(self, "accepted_text", clean_accepted)

    @property
    def explanation(self) -> str:
        return _EXPLANATIONS[self.source]


@dataclass(slots=True)
class _OfferRecord:
    token: int
    offer: ContextualOffer
    state: str
    reason: str
    response_deadline: float = 0.0
    response_window_seconds: float = 0.0


class ContextualOfferState:
    """Thread-safe, process-local state for a single pending yes/no offer."""

    def __init__(self, *, monotonic_clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = monotonic_clock
        self._lock = threading.RLock()
        self._generation = 0
        self._latest: _OfferRecord | None = None
        self._listening_token = 0

    def queue(self, offer: ContextualOffer, *, response_window_seconds: float) -> int:
        if not 5.0 <= float(response_window_seconds) <= 30.0:
            raise ValueError("Contextual offer response window must be between 5 and 30 seconds")
        with self._lock:
            self._expire_unlocked()
            if self._latest is not None and self._latest.state in {"queued", "awaiting_response"}:
                raise RuntimeError("Another contextual offer is already active")
            self._generation += 1
            self._latest = _OfferRecord(
                token=self._generation,
                offer=offer,
                state="queued",
                reason="eligible",
                response_window_seconds=float(response_window_seconds),
            )
            return self._generation

    def mark_spoken(self, token: int) -> bool:
        with self._lock:
            if self._latest is None or self._latest.token != token:
                return False
            record = self._latest
            if record.state != "queued":
                return False
            record.state = "awaiting_response"
            record.reason = "spoken"
            record.response_deadline = self._clock() + record.response_window_seconds
            return True

    def begin_listening(self, token: int) -> bool:
        with self._lock:
            if self._latest is None or self._latest.token != token or self._latest.state != "awaiting_response":
                return False
            self._listening_token = token
            return True

    def finish_listening(self, token: int) -> None:
        with self._lock:
            if self._listening_token == token:
                self._listening_token = 0
            self._expire_unlocked()

    def respond(self, token: int, response: str) -> dict[str, object]:
        normalized = response.strip().lower()
        if normalized not in {"yes", "no", "later"}:
            raise ValueError("Contextual offer response must be yes, no, or later")
        with self._lock:
            self._expire_unlocked()
            record = self._require_current(token)
            if record.state != "awaiting_response":
                raise RuntimeError("Contextual offer is not awaiting a response")
            record.state = {"yes": "accepted", "no": "dismissed", "later": "snoozed"}[normalized]
            record.reason = normalized
            self._listening_token = 0
            return {
                "accepted": True,
                "response": normalized,
                "accepted_text": record.offer.accepted_text if normalized == "yes" else "",
            }

    def cancel(self, token: int, reason: str) -> bool:
        with self._lock:
            if self._latest is None or self._latest.token != token:
                return False
            record = self._latest
            if record.state in {"accepted", "dismissed", "snoozed", "expired", "cancelled"}:
                return False
            record.state = "cancelled"
            record.reason = reason.strip()[:64] or "cancelled"
            record.response_deadline = 0.0
            self._listening_token = 0
            return True

    def is_queued(self, token: int) -> bool:
        with self._lock:
            return self._latest is not None and self._latest.token == token and self._latest.state == "queued"

    def cancel_active(self, reason: str) -> int:
        with self._lock:
            if self._latest is None or self._latest.state not in {"queued", "awaiting_response"}:
                return 0
            token = self._latest.token
            self.cancel(token, reason)
            return token

    def current_offer(self, token: int) -> ContextualOffer:
        with self._lock:
            return self._require_current(token).offer

    def public_status(self, *, enabled: bool) -> dict[str, object]:
        with self._lock:
            self._expire_unlocked()
            record = self._latest
            if not enabled or record is None:
                return {
                    "enabled": enabled,
                    "state": "idle" if enabled else "disabled",
                    "reason": "none" if enabled else "disabled",
                    "token": None,
                    "source": None,
                    "topic": None,
                    "explanation": "",
                    "response_seconds_remaining": 0,
                }
            remaining = (
                max(0, math.ceil(record.response_deadline - self._clock()))
                if record.state == "awaiting_response"
                else 0
            )
            return {
                "enabled": True,
                "state": record.state,
                "reason": record.reason,
                "token": record.token if record.state in {"queued", "awaiting_response"} else None,
                "source": record.offer.source,
                "topic": record.offer.topic,
                "explanation": record.offer.explanation,
                "response_seconds_remaining": remaining,
            }

    def _require_current(self, token: int) -> _OfferRecord:
        if self._latest is None or self._latest.token != token:
            raise RuntimeError("Contextual offer token is stale")
        return self._latest

    def _expire_unlocked(self) -> None:
        record = self._latest
        if (
            record is not None
            and record.state == "awaiting_response"
            and record.token != self._listening_token
            and record.response_deadline <= self._clock()
        ):
            record.state = "expired"
            record.reason = "no_response"
            record.response_deadline = 0.0


def parse_offer_response(text: str) -> OfferResponse:
    """Classify a short English/Dutch reply without substring false positives."""
    clean = re.sub(r"[^a-zA-ZÀ-ÿ\s]", " ", text).strip().lower()
    clean = _SPACE_RE.sub(" ", clean)
    if clean in _YES:
        return "yes"
    if clean in _NO:
        return "no"
    if clean in _LATER:
        return "later"
    return "unknown"
