from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import cast

import pytest

from homebody.config import AppConfig
from homebody.contextual_offers import ContextualOffer
from homebody.initiative import InitiativeCandidate, InitiativePolicy
from homebody.presence import PresenceObservation
from homebody.runtime import HermesVoiceRuntime


class Motion:
    enabled = True

    def __init__(self) -> None:
        self.resume_calls = 0

    def resume(self) -> None:
        self.resume_calls += 1


class Actions:
    busy = False
    pending_count = 0

    def __init__(self) -> None:
        self.queued: list[tuple[str, dict[str, object], bool, bool]] = []

    def enqueue(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        hold_pose: bool = False,
        reject_if_busy: bool = False,
    ) -> dict[str, object]:
        self.queued.append((name, arguments, hold_pose, reject_if_busy))
        return {"accepted": True, "queued": name}


class Robot:
    media = SimpleNamespace()


def make_runtime(**config_overrides: object) -> tuple[HermesVoiceRuntime, Motion, Actions]:
    config = AppConfig(
        proactive_presence_enabled=True,
        presence_acknowledgement_enabled=True,
    )
    for name, value in config_overrides.items():
        setattr(config, name, value)
    config.validate()
    runtime = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    motion = Motion()
    actions = Actions()
    runtime._motion = motion  # type: ignore[assignment]
    runtime._actions = actions  # type: ignore[assignment]
    runtime._audio_ready = True
    runtime._control_ready.set()
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._status.state = "waiting_for_wake_word"
    return runtime, motion, actions


def signal() -> PresenceObservation:
    return PresenceObservation(
        source="home_assistant",
        occupied=True,
        attentive=False,
        direction_degrees=18.0,
        confidence=0.9,
    )


def test_safe_awake_presence_queues_cancellable_action_then_obeys_cooldown() -> None:
    runtime, motion, actions = make_runtime()

    queued = runtime.observe_presence(signal())

    assert actions.queued == [
        ("acknowledge_presence", {"direction_degrees": 18.0}, True, True)
    ]
    assert queued["last_outcome"] == "acknowledgement_queued"
    assert queued["acknowledgements"] == 0
    assert runtime._presence_action_active.is_set()

    runtime._after_robot_action()
    runtime._on_robot_action_result(
        "acknowledge_presence",
        {"ok": True, "action": "acknowledge_presence"},
    )
    completed = cast(dict[str, object], runtime.status()["presence"])
    second = runtime.observe_presence(signal())

    assert motion.resume_calls == 1
    assert completed["last_outcome"] == "acknowledged_silently"
    assert completed["acknowledgements"] == 1
    assert second["last_outcome"] == "cooldown"
    assert second["speech_enabled"] is False
    assert len(actions.queued) == 1


def test_balanced_initiative_policy_commits_only_after_motion_is_queued() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
    )

    runtime.observe_presence(signal())

    initiative = cast(dict[str, object], runtime.status()["initiative"])
    assert len(actions.queued) == 1
    assert initiative["latest_outcome"] == "physical_acknowledgement"
    assert initiative["latest_reason"] == "committed"
    assert initiative["initiatives_this_hour"] == 1
    assert initiative["speech_enabled"] is False


def test_quiet_initiative_policy_suppresses_nonattentive_occupancy() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="quiet",
        initiative_quiet_hours_enabled=False,
    )

    presence = runtime.observe_presence(signal())
    initiative = cast(dict[str, object], runtime.status()["initiative"])

    assert actions.queued == []
    assert presence["last_outcome"] == "quiet_mode"
    assert initiative["latest_outcome"] == "remain_silent"
    assert initiative["latest_reason"] == "quiet_mode"


def test_initiative_policy_projects_runtime_safety_suppression() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
    )
    runtime._initiative = InitiativePolicy(wall_clock=lambda: datetime(2026, 7, 23, 12, 0))
    runtime._power_mode = "standby"
    runtime._motors_enabled = False

    runtime.observe_presence(signal())
    initiative = cast(dict[str, object], runtime.status()["initiative"])

    assert actions.queued == []
    assert initiative["latest_outcome"] == "remain_silent"
    assert initiative["latest_reason"] == "not_awake"


def test_future_offer_candidate_is_evaluated_without_speech_or_robot_action() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
    )
    offer = InitiativeCandidate(
        topic="weather",
        requested_outcome="offer_candidate",
        confidence=0.9,
        fingerprint="weather-v1",
    )

    decision = runtime.evaluate_initiative_candidate(offer)
    status = cast(dict[str, object], runtime.status()["initiative"])

    assert decision.outcome == "offer_candidate"
    assert decision.reason == "eligible"
    assert actions.queued == []
    assert status["initiatives_this_hour"] == 0
    assert status["speech_enabled"] is False

    runtime._privacy_requested.set()
    suppressed = runtime.evaluate_initiative_candidate(offer)
    assert (suppressed.outcome, suppressed.reason) == ("remain_silent", "privacy")


def contextual_offer() -> ContextualOffer:
    return ContextualOffer(
        source="weather",
        topic="rain_soon",
        confidence=0.9,
        fingerprint="weather-rain-1",
        text="Rain is expected soon; would you like the short forecast?",
        accepted_text="Light rain is expected within the next hour.",
    )


def test_goal_3_queues_one_offer_and_commits_budget_without_action() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "agent"
    runtime._announcement_worker = object()  # type: ignore[assignment]

    result = runtime.submit_contextual_offer(contextual_offer())
    announcement = runtime._announcement_queue.get_nowait()
    status = runtime.status()

    assert result["queued"] is True
    assert result["decision"] == "offer_candidate"
    assert announcement.text == contextual_offer().text
    assert announcement.behavior == "voice_only"
    assert announcement.contextual_offer_token == result["token"]
    assert actions.queued == []
    assert cast(dict[str, object], status["initiative"])["initiatives_this_hour"] == 1
    assert cast(dict[str, object], status["initiative"])["speech_enabled"] is True
    assert cast(dict[str, object], status["contextual_offer"])["state"] == "queued"


def test_goal_3_phone_yes_is_single_use_and_only_queues_read_only_speech() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "agent"
    runtime._announcement_worker = object()  # type: ignore[assignment]
    submitted = runtime.submit_contextual_offer(contextual_offer())
    runtime._announcement_queue.get_nowait()
    token = submitted["token"]
    assert isinstance(token, int)
    runtime._contextual_offers.mark_spoken(token)

    response = runtime.respond_to_contextual_offer(token, "yes")
    follow_up = runtime._announcement_queue.get_nowait()

    assert response["action_executed"] is False
    assert follow_up.text == contextual_offer().accepted_text
    assert follow_up.contextual_offer_token == 0
    assert actions.queued == []
    with pytest.raises(RuntimeError, match="not awaiting"):
        runtime.respond_to_contextual_offer(token, "yes")


def test_goal_3_rechecks_hard_safety_before_queueing() -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="engaged",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "agent"
    runtime._announcement_worker = object()  # type: ignore[assignment]
    runtime._privacy_requested.set()

    result = runtime.submit_contextual_offer(contextual_offer())

    assert result == {"ok": True, "queued": False, "decision": "remain_silent", "reason": "privacy"}
    assert runtime._announcement_queue.empty()
    assert actions.queued == []


def test_disabling_goal_3_cancels_pending_offer_without_clearing_other_announcements() -> None:
    runtime, _motion, _actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "agent"
    runtime._announcement_worker = object()  # type: ignore[assignment]
    submitted = runtime.submit_contextual_offer(contextual_offer())
    runtime.queue_announcement("Keep this normal announcement", behavior="voice_only")

    assert runtime.cancel_contextual_offer("contextual_offers_disabled") is True
    assert cast(dict[str, object], runtime.status()["contextual_offer"])["state"] == "cancelled"
    queued = [runtime._announcement_queue.get_nowait() for _ in range(2)]
    assert [item.text for item in queued] == [contextual_offer().text, "Keep this normal announcement"]
    assert submitted["queued"] is True


@pytest.mark.parametrize("power_mode", ["meeting", "sleep"])
def test_initiative_explains_private_power_mode_suppression(power_mode: str) -> None:
    runtime, _motion, actions = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="engaged",
        initiative_quiet_hours_enabled=False,
    )
    runtime._power_mode = power_mode
    runtime._meeting_until = float("inf") if power_mode == "meeting" else 0.0
    runtime._motors_enabled = False

    decision = runtime.evaluate_initiative_candidate(
        InitiativeCandidate(topic="calendar", requested_outcome="offer_candidate", confidence=1.0)
    )

    assert (decision.outcome, decision.reason) == ("remain_silent", power_mode)
    assert actions.queued == []


def test_cancelled_presence_action_is_not_counted_and_releases_motion_owner() -> None:
    runtime, motion, _actions = make_runtime()
    runtime.observe_presence(signal())

    runtime._on_robot_action_result(
        "acknowledge_presence",
        {
            "ok": False,
            "error": "Robot action was cancelled",
            "action": "acknowledge_presence",
        },
    )
    payload = cast(dict[str, object], runtime.status()["presence"])

    assert runtime._presence_action_active.is_set() is False
    assert motion.resume_calls == 1
    assert payload["last_outcome"] == "acknowledgement_cancelled"
    assert payload["acknowledgements"] == 0


def test_kids_transition_rechecks_at_presence_action_execution_time() -> None:
    runtime, _motion, _actions = make_runtime()
    queued = runtime.observe_presence(signal())
    assert queued["last_outcome"] == "acknowledgement_queued"
    runtime._kids_active = True

    with pytest.raises(RuntimeError, match="Kids Mode"):
        runtime._before_robot_action()


def test_presence_never_wakes_standby_or_enables_motors() -> None:
    runtime, _motion, actions = make_runtime()
    runtime._power_mode = "standby"
    runtime._motors_enabled = False
    motor_changes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_changes.append(enabled)  # type: ignore[method-assign]

    payload = runtime.observe_presence(signal())

    assert actions.queued == []
    assert motor_changes == []
    assert runtime._power_mode == "standby"
    assert runtime._motors_enabled is False
    assert payload["level"] == "present"
    assert payload["last_outcome"] == "not_awake"


def test_disabled_presence_discards_the_signal_and_never_queues() -> None:
    config = AppConfig(proactive_presence_enabled=False)
    runtime = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    actions = Actions()
    runtime._actions = actions  # type: ignore[assignment]

    payload = runtime.observe_presence(signal())

    assert actions.queued == []
    assert payload["enabled"] is False
    assert payload["level"] == "away"
    assert payload["source"] is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda runtime: runtime._privacy_requested.set(), "privacy"),
        (lambda runtime: setattr(runtime, "_motors_enabled", False), "motors_not_enabled"),
        (lambda runtime: setattr(runtime, "_kids_active", True), "kids_mode"),
        (lambda runtime: runtime._announcement_active.set(), "announcement_active"),
        (lambda runtime: runtime._voice_activity_lock.acquire(), "voice_active"),
        (lambda runtime: setattr(runtime._status, "state", "listening"), "voice_active"),
        (lambda runtime: setattr(runtime, "_audio_ready", False), "runtime_not_ready"),
        (lambda runtime: setattr(runtime._status, "last_error", "failed"), "runtime_error"),
        (
            lambda runtime: (
                setattr(runtime, "_camera_control_session_id", "camera-test"),
                setattr(runtime, "_camera_control_last_activity", time.monotonic()),
            ),
            "camera_control_active",
        ),
        (lambda runtime: setattr(runtime, "_face_tracking_active", True), "face_tracking_active"),
        (
            lambda runtime: setattr(runtime, "_actions", SimpleNamespace(busy=True, pending_count=1)),
            "robot_action_active",
        ),
    ],
)
def test_presence_motion_is_suppressed_by_every_active_owner(
    mutate: Callable[[HermesVoiceRuntime], None], reason: str
) -> None:
    runtime, _motion, actions = make_runtime()
    mutate(runtime)

    payload = runtime.observe_presence(signal())

    assert actions.queued == []
    assert payload["last_outcome"] == reason
    assert payload["acknowledgements"] == 0


def test_presence_motion_does_not_wait_behind_active_voice_owner() -> None:
    runtime, _motion, actions = make_runtime()
    runtime._voice_activity_lock.acquire()
    try:
        payload = runtime.observe_presence(signal())
    finally:
        runtime._voice_activity_lock.release()

    assert actions.queued == []
    assert payload["last_outcome"] == "voice_active"


def test_internal_voice_observation_updates_attention_without_second_motion() -> None:
    runtime, _motion, actions = make_runtime()

    payload = runtime.observe_presence(
        PresenceObservation(source="voice", occupied=True, attentive=True),
        allow_acknowledgement=False,
    )

    assert actions.queued == []
    assert payload["level"] == "attentive"
    assert payload["source"] == "voice"
    assert payload["last_outcome"] == "observed_voice"


def test_kids_locked_status_does_not_expose_presence_state() -> None:
    runtime, _motion, _actions = make_runtime()
    runtime.observe_presence(signal())
    runtime._kids_locked = True

    payload = runtime.status()

    assert "presence" not in payload
    assert set(payload) == {
        "state",
        "power_mode",
        "motors_enabled",
        "head_safely_folded",
        "kids_mode",
    }
