"""Enrolled unsolicited-suggestion producers for the Hermes host bridge.

Two producers only, both disabled until the owner enrolls exact, stable identifiers from the
host-owned catalog: one Home Assistant calendar lead-time source and one allowlisted Home
Assistant condition. Credentials stay on the host; the robot receives rendered read-only offers.

The functions here are pure (deterministic, testable); the bridge supplies HTTP and timing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_CALENDAR_RE = re.compile(r"^calendar\.[a-z0-9_]+$")
_SPEECH_CLEAN_RE = re.compile(r"[.?!\n\r\t]+")
_SPACE_RE = re.compile(r"\s+")
# A pre-rendered accepted text is read-only help; it must not promise an action Reachy will not take.
_ACTION_PROMISE_RE = re.compile(
    r"(?i)\b(i(?:'ll| will| am going to| can) (?:remind|set|turn|switch|send|start|stop|open|close|order|book|"
    r"schedule|call|text|message|create)|ik (?:zal|ga) (?:je )?(?:herinneren|zetten|sturen|starten|bellen))\b"
)

HOME_ASSISTANT_CONDITIONS = ("state_equals",)
CALENDAR_DISCLOSURES = ("private", "spoken")
MAX_DELIVERED_MEMORY = 64


class EnrollmentError(ValueError):
    """The enrollment is missing something; the message is setup guidance, not a secret."""


@dataclass(frozen=True, slots=True)
class HomeAssistantSource:
    id: str
    label: str
    entity_id: str
    condition: Literal["state_equals"]
    value: str
    offer_text: str
    accepted_text: str
    min_interval_seconds: int


@dataclass(frozen=True, slots=True)
class CalendarSource:
    id: str
    label: str
    calendar_entity_id: str
    lead_minutes: int
    disclosure: Literal["private", "spoken"]


@dataclass(frozen=True, slots=True)
class Enrollment:
    home_assistant: tuple[HomeAssistantSource, ...] = ()
    calendar: tuple[CalendarSource, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.home_assistant or self.calendar)

    def public_summary(self) -> dict[str, object]:
        """Friendly labels and exact identifiers only; never event details."""
        return {
            "enabled": self.enabled,
            "home_assistant": [
                {
                    "id": s.id,
                    "label": s.label,
                    "entity_id": s.entity_id,
                    "condition": s.condition,
                    "value": s.value,
                    "offer_text": s.offer_text,
                    "accepted_text": s.accepted_text,
                    "min_interval_seconds": s.min_interval_seconds,
                }
                for s in self.home_assistant
            ],
            "calendar": [
                {
                    "id": s.id,
                    "label": s.label,
                    "calendar_entity_id": s.calendar_entity_id,
                    "lead_minutes": s.lead_minutes,
                    "disclosure": s.disclosure,
                }
                for s in self.calendar
            ],
        }

    def document(self) -> dict[str, object]:
        """Exactly what is written to the host-owned enrollment file."""
        summary = self.public_summary()
        return {"home_assistant": summary["home_assistant"], "calendar": summary["calendar"]}


@dataclass(frozen=True, slots=True)
class OfferDraft:
    """Exactly the robot's ``/api/initiative/offers`` contract plus host-side lifecycle keys."""

    source: str
    topic: str
    confidence: float
    fingerprint: str
    text: str
    accepted_text: str
    private_detail: str
    expires_at: float
    # Stable identity of the *moment* (entity change / event instance); delivered once, ever.
    moment: str

    def payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "topic": self.topic,
            "confidence": self.confidence,
            "fingerprint": self.fingerprint,
            "text": self.text,
            "accepted_text": self.accepted_text,
            "private_detail": self.private_detail,
        }


def _text(raw: dict[str, Any], key: str, *, maximum: int, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise EnrollmentError(f"'{key}' must be text")
    clean = _SPACE_RE.sub(" ", value).strip()
    if required and not clean:
        raise EnrollmentError(f"'{key}' is required")
    if len(clean) > maximum:
        raise EnrollmentError(f"'{key}' must be at most {maximum} characters")
    return clean


def _parse_home_assistant(raw: dict[str, Any]) -> HomeAssistantSource:
    source_id = _text(raw, "id", maximum=32)
    if not _ID_RE.fullmatch(source_id):
        raise EnrollmentError("Home Assistant source 'id' must be a short lowercase label")
    entity_id = _text(raw, "entity_id", maximum=96)
    if not _ENTITY_RE.fullmatch(entity_id):
        raise EnrollmentError(f"Source '{source_id}': 'entity_id' must be an exact Home Assistant entity id")
    condition = _text(raw, "condition", maximum=32)
    if condition not in HOME_ASSISTANT_CONDITIONS:
        allowed = ", ".join(HOME_ASSISTANT_CONDITIONS)
        raise EnrollmentError(f"Source '{source_id}': 'condition' must be one of {allowed}")
    offer_text = _text(raw, "offer_text", maximum=180)
    if not offer_text.endswith("?") or not 4 <= len(offer_text.split()) <= 30:
        raise EnrollmentError(f"Source '{source_id}': 'offer_text' must be one short question ending in '?'")
    accepted_text = _text(raw, "accepted_text", maximum=240)
    if _ACTION_PROMISE_RE.search(accepted_text) or _ACTION_PROMISE_RE.search(offer_text):
        raise EnrollmentError(
            f"Source '{source_id}': offer and accepted text must describe read-only help; Reachy does not set "
            "reminders, send messages or change devices from a suggestion"
        )
    interval = raw.get("min_interval_seconds", 3600)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 300 <= interval <= 86400:
        raise EnrollmentError(f"Source '{source_id}': 'min_interval_seconds' must be 300-86400")
    return HomeAssistantSource(
        id=source_id,
        label=_text(raw, "label", maximum=48),
        entity_id=entity_id,
        condition="state_equals",
        value=_text(raw, "value", maximum=32),
        offer_text=offer_text,
        accepted_text=accepted_text,
        min_interval_seconds=int(interval),
    )


def _parse_calendar(raw: dict[str, Any]) -> CalendarSource:
    source_id = _text(raw, "id", maximum=32)
    if not _ID_RE.fullmatch(source_id):
        raise EnrollmentError("Calendar source 'id' must be a short lowercase label")
    entity = _text(raw, "calendar_entity_id", maximum=96)
    if not _CALENDAR_RE.fullmatch(entity):
        raise EnrollmentError(f"Source '{source_id}': 'calendar_entity_id' must be an exact calendar.* entity id")
    lead = raw.get("lead_minutes", 10)
    if isinstance(lead, bool) or not isinstance(lead, int) or not 1 <= lead <= 120:
        raise EnrollmentError(f"Source '{source_id}': 'lead_minutes' must be 1-120")
    disclosure = _text(raw, "disclosure", maximum=16, required=False) or "private"
    if disclosure not in CALENDAR_DISCLOSURES:
        raise EnrollmentError(f"Source '{source_id}': 'disclosure' must be 'private' (dashboard) or 'spoken'")
    return CalendarSource(
        id=source_id,
        label=_text(raw, "label", maximum=48),
        calendar_entity_id=entity,
        lead_minutes=int(lead),
        disclosure=disclosure,  # type: ignore[arg-type]
    )


def parse_enrollment(document: object) -> Enrollment:
    if not isinstance(document, dict):
        raise EnrollmentError("Enrollment must be a JSON object with 'home_assistant' and/or 'calendar' lists")
    unknown = set(document) - {"home_assistant", "calendar"}
    if unknown:
        raise EnrollmentError(f"Unknown enrollment keys: {', '.join(sorted(unknown))}")
    ha_raw = document.get("home_assistant", [])
    cal_raw = document.get("calendar", [])
    if not isinstance(ha_raw, list) or not isinstance(cal_raw, list):
        raise EnrollmentError("'home_assistant' and 'calendar' must be lists")
    if len(ha_raw) > 1 or len(cal_raw) > 1:
        raise EnrollmentError("The first coherent release enrolls at most one source of each kind")
    home_assistant = tuple(_parse_home_assistant(item) for item in ha_raw if isinstance(item, dict))
    calendar = tuple(_parse_calendar(item) for item in cal_raw if isinstance(item, dict))
    if len(home_assistant) != len(ha_raw) or len(calendar) != len(cal_raw):
        raise EnrollmentError("Each enrolled source must be a JSON object")
    return Enrollment(home_assistant=home_assistant, calendar=calendar)


def load_enrollment(path: str | Path | None) -> Enrollment:
    """No file configured -> nothing enrolled (disabled). A configured but broken file raises guidance."""
    if not path:
        return Enrollment()
    file = Path(path)
    if not file.is_file():
        return Enrollment()  # configured location, nothing enrolled yet
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EnrollmentError(f"Enrollment file {file} is not readable JSON: {exc}") from exc
    return parse_enrollment(document)


def save_enrollment(path: str | Path, enrollment: Enrollment) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(enrollment.document(), indent=2) + "\n", encoding="utf-8")


def resolve_timezone(name: str) -> tzinfo:
    """The owner's wall-clock zone: an explicit IANA name, else the host's local zone (never bare UTC)."""
    clean = (name or "").strip()
    if clean:
        try:
            return ZoneInfo(clean)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or UTC


def _fingerprint(*parts: str) -> str:
    """Collision-resistant, bounded, allowlisted characters only (never raw identifiers or text)."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    prefix = re.sub(r"[^A-Za-z0-9_-]", "-", parts[0])[:12]
    return f"{prefix}:{digest}"


def _speakable(value: str, maximum: int) -> str:
    return _SPACE_RE.sub(" ", _SPEECH_CLEAN_RE.sub(" ", value)).strip()[:maximum]


def home_assistant_offer(source: HomeAssistantSource, state_payload: object, *, now: float) -> OfferDraft | None:
    """One offer when the exact entity meets its enrolled condition; unavailable/unknown stays silent."""
    if not isinstance(state_payload, dict):
        return None
    state = str(state_payload.get("state") or "").strip().lower()
    if state in {"", "unavailable", "unknown"} or state != source.value.lower():
        return None
    last_changed = str(state_payload.get("last_changed") or "")
    moment = _fingerprint("ha", source.id, source.entity_id, source.value, last_changed or "nochange")
    return OfferDraft(
        source="home_assistant",
        topic=f"ha_{source.id}"[:48],
        confidence=1.0,
        fingerprint=moment,
        text=source.offer_text,
        accepted_text=source.accepted_text,
        private_detail="",
        expires_at=now + float(source.min_interval_seconds),
        moment=moment,
    )


def _event_start(event: object) -> datetime | None:
    if not isinstance(event, dict):
        return None
    start = event.get("start")
    value = start.get("dateTime") if isinstance(start, dict) else start
    if not isinstance(value, str) or not value:
        return None  # all-day events ("date" only) are not lead-time prompts
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def calendar_offer(
    source: CalendarSource,
    events: object,
    *,
    now: datetime,
    monotonic_now: float,
    zone: tzinfo | None = None,
) -> OfferDraft | None:
    """The earliest event starting within the lead window; private by default, spoken only when enrolled so.

    Clock times are rendered in the owner's zone (``zone``), never in UTC by accident.
    """
    if not isinstance(events, list):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    zone = zone or now.tzinfo
    window_end = now + timedelta(minutes=source.lead_minutes)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        start = _event_start(event)
        if start is None or not now < start <= window_end:
            continue
        candidates.append((start, event))
    if not candidates:
        return None
    start, event = min(candidates, key=lambda item: item[0])
    minutes = max(1, int(round((start - now).total_seconds() / 60)))
    summary = _speakable(str(event.get("summary") or "an event"), 80)
    clock = start.astimezone(zone).strftime("%H:%M")
    moment = _fingerprint("cal", source.id, source.calendar_entity_id, str(event.get("uid") or ""), start.isoformat())
    expires_at = monotonic_now + max(60.0, (start - now).total_seconds())
    if source.disclosure == "spoken":
        return OfferDraft(
            source="calendar",
            topic="calendar_lead_time",
            confidence=0.95,
            fingerprint=moment,
            text=f"{summary} starts in {minutes} minutes; would you like the details?",
            accepted_text=f"{summary} starts at {clock}.",
            private_detail="",
            expires_at=expires_at,
            moment=moment,
        )
    return OfferDraft(
        source="calendar",
        topic="calendar_lead_time",
        confidence=0.95,
        fingerprint=moment,
        text=f"Something on your calendar starts in {minutes} minutes; want the details on your phone?",
        accepted_text="Okay, the details are on your dashboard.",
        private_detail=f"{summary} at {clock}",
        expires_at=expires_at,
        moment=moment,
    )


class OfferDedupe:
    """Delivery-aware host-side lifecycle for offers.

    A moment is *delivered* only after the robot accepted it (``queued: true``); it then never repeats,
    even after the draft's expiry, until the source moment changes. Suppressed or failed submissions
    are retried on the next fresh read, bounded by a short per-moment backoff, so a transient block
    does not lose the opportunity. Drafts past their expiry are dropped, never replayed.
    """

    def __init__(self, *, retry_seconds: float = 30.0) -> None:
        self._delivered: dict[str, float] = {}
        self._next_attempt: dict[str, float] = {}
        self._retry_seconds = float(retry_seconds)

    def admit(self, draft: OfferDraft, *, now: float) -> bool:
        if draft.expires_at <= now or draft.moment in self._delivered:
            return False
        return self._next_attempt.get(draft.moment, 0.0) <= now

    def delivered(self, draft: OfferDraft, *, now: float) -> None:
        self._delivered[draft.moment] = now
        self._next_attempt.pop(draft.moment, None)
        if len(self._delivered) > MAX_DELIVERED_MEMORY:
            excess = len(self._delivered) - MAX_DELIVERED_MEMORY
            oldest = sorted(self._delivered, key=self._delivered.__getitem__)[:excess]
            for key in oldest:
                self._delivered.pop(key, None)

    def deferred(self, draft: OfferDraft, *, now: float) -> None:
        self._next_attempt[draft.moment] = now + self._retry_seconds

    def forget_all(self) -> None:
        """Enrollment changed: no old queued condition may speak under the new configuration."""
        self._delivered.clear()
        self._next_attempt.clear()
