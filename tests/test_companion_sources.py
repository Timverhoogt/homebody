"""Host-side enrolled sources: disabled until enrolled, exact identifiers, private-by-default calendar, dedupe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from companion.companion_sources import (
    EnrollmentError,
    OfferDedupe,
    calendar_offer,
    home_assistant_offer,
    load_enrollment,
    parse_enrollment,
    save_enrollment,
)

HA = {
    "id": "washer",
    "label": "Washing machine",
    "entity_id": "binary_sensor.washer_done",
    "condition": "state_equals",
    "value": "on",
    "offer_text": "The washing machine seems finished; would you like to know when it stopped?",
    "accepted_text": "Okay. The washer reported done just now; nothing else was changed.",
    "min_interval_seconds": 1800,
}
CAL = {"id": "family", "label": "Family calendar", "calendar_entity_id": "calendar.family", "lead_minutes": 10}


def test_nothing_enrolled_without_a_file_and_enum_is_not_enrollment(tmp_path) -> None:
    assert load_enrollment(None).enabled is False
    assert load_enrollment("").enabled is False
    assert load_enrollment(tmp_path / "missing.json").enabled is False  # configured location, not enrolled yet
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"home_assistant": [HA], "calendar": [CAL]}), encoding="utf-8")
    enrollment = load_enrollment(path)
    assert enrollment.enabled and enrollment.home_assistant[0].entity_id == "binary_sensor.washer_done"
    summary = enrollment.public_summary()
    assert summary["calendar"][0]["disclosure"] == "private"
    save_enrollment(tmp_path / "nested" / "sources.json", enrollment)
    assert load_enrollment(tmp_path / "nested" / "sources.json") == enrollment
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(EnrollmentError, match="not readable JSON"):
        load_enrollment(tmp_path / "broken.json")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"entity_id": "http://evil"}, "exact Home Assistant entity id"),
        ({"condition": "regex"}, "condition"),
        ({"offer_text": "Do it now."}, "short question"),
        ({"min_interval_seconds": 5}, "300-86400"),
        ({"id": "Bad Id"}, "lowercase label"),
        ({"accepted_text": "Okay, I will remind you in ten minutes."}, "read-only help"),
        ({"offer_text": "Shall I turn off the washer; I'll switch it off for you?"}, "read-only help"),
    ],
)
def test_home_assistant_enrollment_rejects_unsafe_values_with_guidance(mutation, message) -> None:
    with pytest.raises(EnrollmentError, match=message):
        parse_enrollment({"home_assistant": [{**HA, **mutation}]})


def test_calendar_enrollment_bounds_and_single_source_limit() -> None:
    with pytest.raises(EnrollmentError, match="calendar.\\* entity"):
        parse_enrollment({"calendar": [{**CAL, "calendar_entity_id": "sensor.x"}]})
    with pytest.raises(EnrollmentError, match="1-120"):
        parse_enrollment({"calendar": [{**CAL, "lead_minutes": 500}]})
    with pytest.raises(EnrollmentError, match="at most one"):
        parse_enrollment({"calendar": [CAL, {**CAL, "id": "two"}]})
    with pytest.raises(EnrollmentError, match="Unknown enrollment keys"):
        parse_enrollment({"webhooks": []})


def test_home_assistant_condition_is_delivered_once_per_change_and_stays_silent_when_unavailable() -> None:
    source = parse_enrollment({"home_assistant": [HA]}).home_assistant[0]
    assert home_assistant_offer(source, {"state": "off"}, now=0.0) is None
    assert home_assistant_offer(source, {"state": "unavailable"}, now=0.0) is None
    draft = home_assistant_offer(source, {"state": "on", "last_changed": "2026-10-16T14:00:00+02:00"}, now=0.0)
    assert draft is not None and draft.payload()["source"] == "home_assistant"
    assert draft.fingerprint.startswith("ha:") and len(draft.fingerprint) <= 64
    assert "+" not in draft.fingerprint and "washer_done" not in draft.fingerprint  # hashed, not raw ids
    dedupe = OfferDedupe(retry_seconds=30.0)
    assert dedupe.admit(draft, now=0.0) is True
    dedupe.deferred(draft, now=0.0)  # robot said queued=false: keep the opportunity, back off briefly
    assert dedupe.admit(draft, now=10.0) is False
    assert dedupe.admit(draft, now=31.0) is True
    dedupe.delivered(draft, now=31.0)
    same_moment_later = home_assistant_offer(
        source,
        {"state": "on", "last_changed": "2026-10-16T14:00:00+02:00"},
        now=5000.0,
    )
    assert same_moment_later is not None and dedupe.admit(same_moment_later, now=5000.0) is False  # never repeats
    later = home_assistant_offer(source, {"state": "on", "last_changed": "2026-10-16T16:00:00+02:00"}, now=100.0)
    assert later is not None and dedupe.admit(later, now=100.0) is True
    expired = home_assistant_offer(source, {"state": "on", "last_changed": "x"}, now=0.0)
    assert expired is not None and dedupe.admit(expired, now=5000.0) is False  # backlog never replays
    dedupe.forget_all()  # enrollment changed: host memory is cleared; a still-fresh moment may be offered again
    assert dedupe.admit(same_moment_later, now=6000.0) is True
    assert dedupe.admit(same_moment_later, now=7000.0) is False  # past its own freshness: never replayed


def test_calendar_lead_time_is_private_by_default_spoken_only_when_enrolled_and_uses_owner_zone() -> None:
    now = datetime(2026, 10, 16, 12, 50, tzinfo=UTC)
    events = [
        {"summary": "Dentist. Main Street 5!", "uid": "abc", "start": {"dateTime": "2026-10-16T13:00:00+00:00"}},
        {"summary": "Later thing", "start": {"dateTime": "2026-10-16T16:00:00+00:00"}},
        {"summary": "All day", "start": {"date": "2026-10-16"}},
    ]
    amsterdam = ZoneInfo("Europe/Amsterdam")
    private = parse_enrollment({"calendar": [CAL]}).calendar[0]
    draft = calendar_offer(private, events, now=now, monotonic_now=0.0, zone=amsterdam)
    assert draft is not None
    assert "Dentist" not in draft.text and "Dentist" not in draft.accepted_text
    assert draft.private_detail == "Dentist Main Street 5 at 15:00" and "10 minutes" in draft.text  # owner's clock
    assert draft.fingerprint.startswith("cal:") and "abc" not in draft.fingerprint
    assert draft.expires_at == 600.0  # fresh only until the event starts
    spoken = parse_enrollment({"calendar": [{**CAL, "disclosure": "spoken"}]}).calendar[0]
    said = calendar_offer(spoken, events, now=now, monotonic_now=0.0, zone=amsterdam)
    assert said is not None and said.text.startswith("Dentist Main Street 5 starts in 10 minutes")
    assert said.accepted_text == "Dentist Main Street 5 starts at 15:00." and said.private_detail == ""
    assert calendar_offer(private, events, now=now + timedelta(minutes=11), monotonic_now=0.0) is None
    assert calendar_offer(private, "not a list", now=now, monotonic_now=0.0) is None
    assert OfferDedupe().admit(draft, now=draft.expires_at + 1) is False
