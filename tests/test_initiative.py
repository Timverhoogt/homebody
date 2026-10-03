from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from homebody.initiative import (
    InitiativeCandidate,
    InitiativePolicy,
    InitiativeSettings,
)


class Clock:
    def __init__(self) -> None:
        self.monotonic = 100.0
        self.wall = datetime(2026, 7, 23, 12, 0)

    def mono(self) -> float:
        return self.monotonic

    def now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def policy(clock: Clock) -> InitiativePolicy:
    return InitiativePolicy(monotonic_clock=clock.mono, wall_clock=clock.now)


def candidate(
    *,
    topic: str = "office_presence",
    outcome: str = "physical_acknowledgement",
    confidence: float = 1.0,
    attentive: bool = True,
    fingerprint: str = "arrival-v1",
) -> InitiativeCandidate:
    return InitiativeCandidate(
        topic=topic,
        requested_outcome=outcome,  # type: ignore[arg-type]
        confidence=confidence,
        attentive=attentive,
        fingerprint=fingerprint,
    )


def settings(**changes: object) -> InitiativeSettings:
    base = InitiativeSettings(enabled=True, mode="balanced", quiet_hours_enabled=False)
    return replace(base, **changes)


def test_candidate_rejects_unbounded_or_invalid_context() -> None:
    with pytest.raises(ValueError):
        candidate(topic="contains private prose")
    with pytest.raises(ValueError):
        candidate(topic="x" * 49)
    with pytest.raises(ValueError):
        candidate(confidence=float("nan"))
    with pytest.raises(ValueError):
        candidate(fingerprint="x" * 65)
    with pytest.raises(ValueError):
        candidate(fingerprint="raw private sentence")


def test_disabled_policy_remains_silent() -> None:
    clock = Clock()
    engine = policy(clock)
    decision = engine.evaluate(candidate(), InitiativeSettings())
    assert (decision.outcome, decision.reason) == ("remain_silent", "disabled")
    assert engine.public_status(InitiativeSettings())["latest_reason"] == "disabled"


def test_hard_runtime_suppression_always_wins() -> None:
    clock = Clock()
    decision = policy(clock).evaluate(candidate(), settings(mode="engaged"), suppression_reason="kids_mode")
    assert (decision.outcome, decision.reason) == ("remain_silent", "kids_mode")


def test_quiet_mode_requires_attentive_high_confidence_physical_context() -> None:
    clock = Clock()
    engine = policy(clock)
    quiet = settings(mode="quiet")
    assert engine.evaluate(candidate(attentive=False), quiet).reason == "quiet_mode"
    assert engine.evaluate(candidate(confidence=0.94), quiet).reason == "low_confidence"
    allowed = engine.evaluate(candidate(), quiet)
    assert (allowed.outcome, allowed.reason) == ("physical_acknowledgement", "eligible")


def test_quiet_mode_never_produces_offer_candidate() -> None:
    clock = Clock()
    decision = policy(clock).evaluate(candidate(outcome="offer_candidate"), settings(mode="quiet"))
    assert (decision.outcome, decision.reason) == ("remain_silent", "quiet_mode")


def test_balanced_and_engaged_offer_thresholds_are_deterministic() -> None:
    clock = Clock()
    engine = policy(clock)
    assert (
        engine.evaluate(candidate(outcome="offer_candidate", confidence=0.79), settings()).reason
        == "low_confidence"
    )
    assert (
        engine.evaluate(candidate(outcome="offer_candidate", confidence=0.8), settings()).outcome
        == "offer_candidate"
    )
    assert (
        engine.evaluate(candidate(outcome="offer_candidate", confidence=0.6), settings(mode="engaged")).outcome
        == "offer_candidate"
    )


def test_quiet_hours_wrap_midnight_and_equal_times_disable_window() -> None:
    clock = Clock()
    clock.wall = datetime(2026, 7, 23, 23, 0)
    engine = policy(clock)
    quiet_hours = settings(quiet_hours_enabled=True, quiet_hours_start="22:00", quiet_hours_end="07:00")
    assert engine.evaluate(candidate(), quiet_hours).reason == "quiet_hours"
    clock.wall = datetime(2026, 7, 24, 8, 0)
    assert engine.evaluate(candidate(), quiet_hours).outcome == "physical_acknowledgement"
    all_day_disabled = replace(quiet_hours, quiet_hours_start="08:00", quiet_hours_end="08:00")
    assert engine.evaluate(candidate(), all_day_disabled).outcome == "physical_acknowledgement"


def test_uncommitted_candidate_does_not_consume_budget() -> None:
    clock = Clock()
    engine = policy(clock)
    constrained = settings(hourly_budget=1)
    first = engine.evaluate(candidate(), constrained)
    assert first.outcome == "physical_acknowledgement"
    second = engine.evaluate(candidate(topic="other_topic", fingerprint="other"), constrained)
    assert second.outcome == "physical_acknowledgement"


def test_commit_consumes_hourly_and_daily_budgets() -> None:
    clock = Clock()
    engine = policy(clock)
    hourly = settings(hourly_budget=1, daily_budget=5)
    first = engine.evaluate(candidate(), hourly)
    assert engine.commit(first) is True
    assert engine.commit(first) is False
    assert engine.evaluate(candidate(topic="weather", fingerprint="weather"), hourly).reason == "hourly_budget"

    clock.advance(3601)
    daily = settings(hourly_budget=10, daily_budget=1)
    assert engine.evaluate(candidate(topic="calendar", fingerprint="calendar"), daily).reason == "daily_budget"


def test_topic_cooldown_and_duplicate_suppression_use_committed_actions_only() -> None:
    clock = Clock()
    engine = policy(clock)
    configured = settings(topic_cooldown_seconds=600, duplicate_window_seconds=300)
    first = engine.evaluate(candidate(), configured)
    assert engine.commit(first)
    assert engine.evaluate(candidate(fingerprint="different"), configured).reason == "topic_cooldown"
    assert (
        engine.evaluate(candidate(topic="other_topic", fingerprint="arrival-v1"), configured).reason
        == "duplicate"
    )
    clock.advance(601)
    assert engine.evaluate(candidate(), configured).outcome == "physical_acknowledgement"


def test_dismissal_backoff_doubles_and_welcomed_resets_it() -> None:
    clock = Clock()
    engine = policy(clock)
    configured = settings(dismissal_backoff_seconds=60)
    engine.record_dismissal("office_presence", configured)
    assert engine.evaluate(candidate(), configured).reason == "dismissal_backoff"
    clock.advance(61)
    assert engine.evaluate(candidate(), configured).outcome == "physical_acknowledgement"
    engine.record_dismissal("office_presence", configured)
    clock.advance(61)
    assert engine.evaluate(candidate(), configured).reason == "dismissal_backoff"
    engine.record_welcomed("office_presence")
    assert engine.evaluate(candidate(), configured).outcome == "physical_acknowledgement"


def test_cancel_changes_latest_explanation_without_consuming_budget() -> None:
    clock = Clock()
    engine = policy(clock)
    configured = settings(hourly_budget=1)
    decision = engine.evaluate(candidate(), configured)
    engine.cancel(decision, "robot_action_active")
    status = engine.public_status(configured)
    assert status["latest_outcome"] == "remain_silent"
    assert status["latest_reason"] == "robot_action_active"
    assert status["initiatives_this_hour"] == 0


def test_public_status_is_sanitized_and_never_enables_speech() -> None:
    clock = Clock()
    engine = policy(clock)
    configured = settings()
    decision = engine.evaluate(candidate(fingerprint="private-detail"), configured)
    assert engine.commit(decision)
    status = engine.public_status(configured)
    preferences = status.pop("preferences")
    assert status.pop("latest_explanation").startswith("Reachy took this initiative")
    assert {row["category"] for row in preferences} >= {"presence", "calendar"}
    assert all(row["state"] == "normal" for row in preferences)
    assert status == {
        "enabled": True,
        "mode": "balanced",
        "quiet_hours_active": False,
        "latest_outcome": "physical_acknowledgement",
        "latest_reason": "committed",
        "latest_topic": "office_presence",
        "initiatives_this_hour": 1,
        "hourly_budget": 2,
        "initiatives_today": 1,
        "daily_budget": 6,
        "speech_enabled": False,
    }
    assert "private-detail" not in repr(status)
