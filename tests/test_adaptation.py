from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_initiative import Clock, settings
from test_presence_runtime import contextual_offer, make_runtime

from homebody.adaptation import HALF_LIFE_SECONDS, SNOOZE_SECONDS, PreferenceLedger
from homebody.initiative import InitiativeCandidate, InitiativePolicy


class LedgerClock:
    def __init__(self, hour: int = 12) -> None:
        self.epoch = 1_800_000_000.0
        self.wall = datetime(2026, 10, 3, hour, 0)

    def now(self) -> datetime:
        return self.wall

    def time(self) -> float:
        return self.epoch

    def advance(self, seconds: float) -> None:
        self.epoch += seconds
        self.wall += timedelta(seconds=seconds)


def ledger(clock: LedgerClock, path: Path | None = None) -> PreferenceLedger:
    return PreferenceLedger(path, wall_clock=clock.now, epoch_clock=clock.time)


def offer_candidate(category: str = "calendar", confidence: float = 0.95) -> InitiativeCandidate:
    return InitiativeCandidate(
        topic="calendar.next_event",
        category=category,
        requested_outcome="offer_candidate",
        confidence=confidence,
        fingerprint="calendar-1",
    )


def test_dismissals_slow_a_category_and_welcomes_speed_it_up_within_bounds() -> None:
    clock = LedgerClock()
    preferences = ledger(clock)
    assert preferences.cooldown_multiplier("calendar") == 1.0

    for _ in range(2):
        preferences.record("calendar", "dismissed")
    assert preferences.cooldown_multiplier("calendar") == pytest.approx(2.0)
    for _ in range(10):
        preferences.record("calendar", "dismissed")
    assert preferences.cooldown_multiplier("calendar") == 4.0

    for _ in range(20):
        preferences.record("weather", "welcomed")
    assert preferences.cooldown_multiplier("weather") == 0.5
    assert preferences.cooldown_multiplier("timer") == 1.0


def test_signals_fade_with_a_two_week_half_life() -> None:
    clock = LedgerClock()
    preferences = ledger(clock)
    for _ in range(4):
        preferences.record("calendar", "dismissed")

    clock.advance(HALF_LIFE_SECONDS)
    row = next(item for item in preferences.public_status() if item["category"] == "calendar")

    assert row["dismissed"] == pytest.approx(2.0)


def test_later_snoozes_only_that_category_and_a_welcome_clears_it() -> None:
    clock = LedgerClock()
    preferences = ledger(clock)
    preferences.record("weather", "snoozed")

    assert preferences.suppression_reason("weather") == "snoozed"
    assert preferences.suppression_reason("calendar") == ""
    assert preferences.cooldown_multiplier("weather") == 1.0
    clock.advance(SNOOZE_SECONDS + 1)
    assert preferences.suppression_reason("weather") == ""

    preferences.record("weather", "snoozed")
    preferences.record("weather", "welcomed")
    assert preferences.suppression_reason("weather") == ""


def test_owner_can_disable_a_category() -> None:
    preferences = ledger(LedgerClock())
    preferences.set_disabled("presence", True)

    assert preferences.suppression_reason("presence") == "category_disabled"
    row = next(item for item in preferences.public_status() if item["category"] == "presence")
    assert row["state"] == "disabled"
    preferences.set_disabled("presence", False)
    assert preferences.suppression_reason("presence") == ""


def test_repeated_evening_dismissals_create_a_learned_quiet_time_only_in_the_evening() -> None:
    clock = LedgerClock(hour=20)
    preferences = ledger(clock)
    for _ in range(3):
        preferences.record("project", "dismissed")

    assert preferences.suppression_reason("project") == "learned_quiet_time"
    clock.wall = clock.wall.replace(hour=9)
    assert preferences.suppression_reason("project") == ""
    row = next(item for item in preferences.public_status() if item["category"] == "project")
    assert row["quiet_bands"] == ["evening"]

    clock.wall = clock.wall.replace(hour=20)
    for _ in range(4):
        preferences.record("project", "welcomed")
    assert preferences.suppression_reason("project") == ""


def test_policy_applies_learned_timing_but_never_lowers_confidence_or_budgets() -> None:
    clock = Clock()
    preferences = PreferenceLedger(wall_clock=clock.now)
    engine = InitiativePolicy(monotonic_clock=clock.mono, wall_clock=clock.now, preferences=preferences)
    configured = settings(topic_cooldown_seconds=600.0, hourly_budget=10, daily_budget=30)

    for _ in range(25):
        preferences.record("calendar", "welcomed")
    # Many welcomes never make a low-confidence moment eligible.
    assert engine.evaluate(offer_candidate(confidence=0.5), configured).reason == "low_confidence"

    first = engine.evaluate(offer_candidate(), configured)
    assert engine.commit(first)
    clock.advance(301)
    # Welcomed: the 600 s topic cooldown is halved, so 301 s later it may return.
    assert engine.evaluate(offer_candidate(), configured).reason == "eligible"

    preferences.set_disabled("calendar", True)
    assert engine.evaluate(offer_candidate(), configured).reason == "category_disabled"
    # Candidates outside adaptation are unaffected.
    uncategorised = InitiativeCandidate(
        topic="calendar.next_event", requested_outcome="offer_candidate", confidence=0.95, fingerprint="x"
    )
    assert engine.evaluate(uncategorised, configured).reason != "category_disabled"


def test_status_explains_the_latest_decision() -> None:
    clock = Clock()
    preferences = PreferenceLedger(wall_clock=clock.now)
    engine = InitiativePolicy(monotonic_clock=clock.mono, wall_clock=clock.now, preferences=preferences)
    preferences.record("calendar", "snoozed")

    engine.evaluate(offer_candidate(), settings())

    status = engine.public_status(settings())
    assert status["latest_reason"] == "snoozed"
    assert "later" in str(status["latest_explanation"])


def test_ledger_persists_only_counters_and_survives_corruption(tmp_path: Path) -> None:
    path = tmp_path / "initiative-preferences.json"
    clock = LedgerClock(hour=20)
    preferences = ledger(clock, path)
    preferences.record("calendar", "dismissed")
    preferences.set_disabled("presence", True)

    stored = json.loads(path.read_text())
    assert set(stored) == {"calendar", "presence"}
    assert set(stored["calendar"]) == {
        "welcomed", "dismissed", "snoozed", "band_dismissals", "snoozed_until", "disabled", "updated_at"
    }
    reloaded = ledger(clock, path)
    assert reloaded.suppression_reason("presence") == "category_disabled"
    assert reloaded.cooldown_multiplier("calendar") > 1.0

    reloaded.reset()
    assert json.loads(path.read_text()) == {}

    path.write_text("{not json")
    assert ledger(clock, path).suppression_reason("presence") == ""


def test_runtime_offer_responses_feed_the_ledger_and_later_adds_no_dismissal_backoff() -> None:
    runtime, _motion, _actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "agent"
    runtime._announcement_worker = object()  # type: ignore[assignment]

    def offer_and_answer(answer: str) -> None:
        submitted = runtime.submit_contextual_offer(contextual_offer())
        runtime._announcement_queue.get_nowait()
        token = submitted["token"]
        assert isinstance(token, int)
        runtime._contextual_offers.mark_spoken(token)
        runtime.respond_to_contextual_offer(token, answer)

    offer_and_answer("later")
    category = contextual_offer().source
    rows = {row["category"]: row for row in runtime._initiative.preferences.public_status()}
    assert rows[category]["state"] == "snoozed"
    assert rows[category]["dismissed"] == 0
    assert runtime._initiative._dismissal_counts == {}

    blocked = runtime.submit_contextual_offer(contextual_offer())
    assert blocked["queued"] is False
    assert blocked["reason"] == "snoozed"


def test_dismissals_stretch_the_topic_cooldown() -> None:
    clock = Clock()
    preferences = PreferenceLedger(wall_clock=clock.now)
    engine = InitiativePolicy(monotonic_clock=clock.mono, wall_clock=clock.now, preferences=preferences)
    configured = settings(topic_cooldown_seconds=600.0, hourly_budget=10, daily_budget=30)
    # Two declines (below the learned-quiet-time threshold) double the cooldown to 1200 s.
    preferences.record("calendar", "dismissed")
    preferences.record("calendar", "dismissed")

    assert engine.commit(engine.evaluate(offer_candidate(), configured))
    clock.advance(900)
    assert engine.evaluate(offer_candidate(), configured).reason == "topic_cooldown"
    clock.advance(350)
    assert engine.evaluate(offer_candidate(), configured).reason == "eligible"
