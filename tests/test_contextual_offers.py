from __future__ import annotations

from dataclasses import replace

import pytest

from reachy_mini_hermes.contextual_offers import (
    ContextualOffer,
    ContextualOfferState,
    parse_offer_response,
)
from reachy_mini_hermes.initiative import InitiativeSettings


def offer(**changes: object) -> ContextualOffer:
    base = ContextualOffer(
        source="calendar",
        topic="next_calendar_event",
        confidence=0.92,
        fingerprint="calendar-event-42",
        text="Your next calendar event starts soon; would you like the details?",
        accepted_text="It starts in fifteen minutes.",
    )
    return replace(base, **changes)


def test_offer_contract_is_allowlisted_bounded_and_one_question() -> None:
    assert offer().explanation == "High-confidence calendar context"
    with pytest.raises(ValueError, match="source"):
        offer(source="email")
    with pytest.raises(ValueError, match="one concise question"):
        offer(text="A statement. Would you like help?")
    with pytest.raises(ValueError, match="one concise question"):
        offer(text="Would you like help? Extra")
    with pytest.raises(ValueError, match="URL"):
        offer(text="Would you like https://example.com?")
    with pytest.raises(ValueError, match="accepted text"):
        offer(accepted_text="x" * 241)


def test_intentional_presentation_is_an_allowlisted_sanitized_source() -> None:
    presented = offer(source="presentation", topic="presented_context")
    assert presented.explanation == "High-confidence presentation context"


def test_offer_state_exposes_explanation_and_response_without_fingerprint() -> None:
    now = [100.0]
    state = ContextualOfferState(monotonic_clock=lambda: now[0])
    token = state.queue(offer(), response_window_seconds=12)
    queued = state.public_status(enabled=True)
    assert queued["state"] == "queued"
    assert queued["token"] == token
    assert queued["explanation"] == "High-confidence calendar context"
    assert "calendar-event-42" not in repr(queued)
    assert offer().text not in repr(queued)
    assert offer().accepted_text not in repr(queued)

    assert state.mark_spoken(token) is True
    awaiting = state.public_status(enabled=True)
    assert awaiting["state"] == "awaiting_response"
    assert awaiting["response_seconds_remaining"] == 12

    now[0] += 13
    expired = state.public_status(enabled=True)
    assert expired["state"] == "expired"
    assert expired["response_seconds_remaining"] == 0


def test_status_poll_cannot_expire_a_voice_response_while_recording() -> None:
    now = [100.0]
    state = ContextualOfferState(monotonic_clock=lambda: now[0])
    token = state.queue(offer(), response_window_seconds=5)
    state.mark_spoken(token)
    assert state.begin_listening(token) is True
    now[0] += 6
    assert state.public_status(enabled=True)["state"] == "awaiting_response"
    state.finish_listening(token)
    assert state.public_status(enabled=True)["state"] == "expired"


def test_offer_response_is_single_use_and_never_describes_an_action() -> None:
    state = ContextualOfferState(monotonic_clock=lambda: 100.0)
    token = state.queue(offer(), response_window_seconds=20)
    state.mark_spoken(token)
    accepted = state.respond(token, "yes")
    assert accepted == {"accepted": True, "response": "yes", "accepted_text": "It starts in fifteen minutes."}
    assert state.public_status(enabled=True)["state"] == "accepted"
    with pytest.raises(RuntimeError, match="not awaiting"):
        state.respond(token, "yes")


def test_dismissal_and_cancellation_are_explicit() -> None:
    state = ContextualOfferState(monotonic_clock=lambda: 100.0)
    token = state.queue(offer(), response_window_seconds=20)
    state.mark_spoken(token)
    assert state.respond(token, "no") == {"accepted": True, "response": "no", "accepted_text": ""}
    assert state.public_status(enabled=True)["state"] == "dismissed"

    token = state.queue(offer(topic="weather", fingerprint="weather-1"), response_window_seconds=20)
    state.cancel(token, "privacy")
    status = state.public_status(enabled=True)
    assert status["state"] == "cancelled"
    assert status["reason"] == "privacy"


def test_only_one_offer_can_be_active_and_stale_worker_callbacks_are_safe() -> None:
    state = ContextualOfferState(monotonic_clock=lambda: 100.0)
    first = state.queue(offer(), response_window_seconds=20)
    with pytest.raises(RuntimeError, match="already active"):
        state.queue(offer(topic="weather", fingerprint="weather-1"), response_window_seconds=20)
    assert state.mark_spoken(first + 1) is False
    assert state.cancel(first + 1, "stale") is False
    assert state.is_queued(first) is True


@pytest.mark.parametrize("text", ["yes", "yeah", "sure", "please do", "ja", "graag"])
def test_yes_response_parser(text: str) -> None:
    assert parse_offer_response(text) == "yes"


@pytest.mark.parametrize("text", ["no", "no thanks", "nee", "liever niet"])
def test_no_response_parser(text: str) -> None:
    assert parse_offer_response(text) == "no"


@pytest.mark.parametrize("text", ["not now", "later", "maybe later", "nu niet", "straks"])
def test_later_response_parser_snoozes_instead_of_declining(text: str) -> None:
    assert parse_offer_response(text) == "later"


@pytest.mark.parametrize("text", ["maybe", "what is it", "yesterday", "nobody"])
def test_ambiguous_response_parser(text: str) -> None:
    assert parse_offer_response(text) == "unknown"


def test_settings_remain_goal_2_compatible() -> None:
    settings = InitiativeSettings(enabled=True, mode="balanced", quiet_hours_enabled=False)
    assert settings.enabled is True
