from __future__ import annotations

import pytest

from homebody.presence import PresenceObservation, PresenceState


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_presence_state_exposes_only_sanitized_bounded_fields() -> None:
    clock = Clock()
    state = PresenceState(clock=clock)

    state.observe(
        PresenceObservation(
            source="home_assistant",
            occupied=True,
            attentive=False,
            direction_degrees=22.25,
            confidence=0.87654,
        )
    )

    assert state.public_status(enabled=True) == {
        "enabled": True,
        "level": "present",
        "source": "home_assistant",
        "direction_degrees": 22.2,
        "confidence": 0.877,
        "observed_seconds_ago": 0,
        "last_outcome": "observed",
        "acknowledged_seconds_ago": None,
        "acknowledgements": 0,
        "speech_enabled": False,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source": "camera"}, "Unsupported presence source"),
        ({"source": "voice", "occupied": False, "attentive": True}, "Attention requires"),
        ({"source": "voice", "confidence": 1.1}, "confidence must be between"),
        ({"source": "voice", "direction_degrees": 61.0}, "direction must be between"),
        ({"source": "voice", "direction_degrees": float("nan")}, "direction must be a finite"),
    ],
)
def test_presence_observation_rejects_unbounded_or_inconsistent_values(
    kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {"source": "voice", "occupied": True}
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        PresenceObservation(**values)  # type: ignore[arg-type]


def test_acknowledgement_cooldown_is_process_local_and_deterministic() -> None:
    clock = Clock()
    state = PresenceState(clock=clock)
    state.observe(PresenceObservation(source="trusted_sensor", occupied=True))

    assert state.acknowledgement_due(120.0) is True
    state.record_acknowledgement()
    assert state.acknowledgement_due(120.0) is False

    clock.now += 119.0
    assert state.acknowledgement_due(120.0) is False
    clock.now += 1.0
    assert state.acknowledgement_due(120.0) is True


def test_cancelled_queue_attempt_starts_cooldown_without_claiming_acknowledgement() -> None:
    clock = Clock()
    state = PresenceState(clock=clock)
    state.observe(PresenceObservation(source="trusted_sensor", occupied=True))

    state.reserve_acknowledgement()
    state.complete_acknowledgement(succeeded=False, reason="acknowledgement_cancelled")
    payload = state.public_status(enabled=True)

    assert payload["last_outcome"] == "acknowledgement_cancelled"
    assert payload["acknowledgements"] == 0
    assert payload["acknowledged_seconds_ago"] is None
    assert state.acknowledgement_due(120.0) is False


def test_disabled_status_hides_latest_presence_observation() -> None:
    state = PresenceState(clock=Clock())
    state.observe(PresenceObservation(source="home_assistant", occupied=True, attentive=True))

    payload = state.public_status(enabled=False)

    assert payload["enabled"] is False
    assert payload["level"] == "away"
    assert payload["source"] is None
    assert payload["direction_degrees"] is None
    assert payload["observed_seconds_ago"] is None
    assert payload["speech_enabled"] is False


def test_away_signal_clears_direction_and_suppresses_acknowledgement() -> None:
    state = PresenceState(clock=Clock())
    state.observe(
        PresenceObservation(source="home_assistant", occupied=True, direction_degrees=-30.0)
    )
    state.observe(PresenceObservation(source="home_assistant", occupied=False))

    payload = state.public_status(enabled=True)
    assert payload["level"] == "away"
    assert payload["direction_degrees"] is None
    assert payload["last_outcome"] == "away"
    assert state.acknowledgement_due(120.0) is False
