"""Holistic companion: presence source health, coordinator fences, direct looking and connected sources.

Every transport is a fake; no camera, microphone, robot, provider or network is touched.
"""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest
from PIL import Image

from homebody.companion import CompanionCoordinator, LookContext
from homebody.config import AppConfig
from homebody.contextual_offers import ContextualOffer
from homebody.hermes_client import HermesBridgeError
from homebody.look_intent import looks_like_look_request
from homebody.presence import PresenceObservation, PresenceState
from homebody.runtime import HermesVoiceRuntime


def jpeg(level: int = 90) -> bytes:
    pixels = np.full((120, 160, 3), level, dtype=np.uint8)
    output = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(output, format="JPEG", quality=80)
    return output.getvalue()


class Robot:
    media = SimpleNamespace()


class Actions:
    busy = False
    pending_count = 0

    def __init__(self) -> None:
        self.queued: list[str] = []

    def enqueue(
        self, name: str, arguments: dict[str, object], *, hold_pose: bool = False, reject_if_busy: bool = False
    ) -> dict[str, object]:
        self.queued.append(name)
        return {"accepted": True, "queued": name}


class FakeLookClient:
    """Fake bridge transport: records calls, can be held open or fail late."""

    def __init__(self) -> None:
        self.looks: list[tuple[int, str]] = []
        self.follow_ups: list[tuple[str, str]] = []
        self.turns: list[str] = []
        self.release = threading.Event()
        self.release.set()
        self.fail_with: Exception | None = None
        self.answer = "It is a letter from the dentist about an appointment on Friday."
        self.closed = 0
        self.config = SimpleNamespace(kids_mode_enabled=False)

    def look(self, frame: bytes, question: str, *, session_id: str = "") -> dict[str, object]:
        self.looks.append((len(frame), question))
        self.release.wait(5)
        if self.fail_with is not None:
            raise self.fail_with
        return {"text": self.answer, "model": "fake-vision", "processing": "host_provider"}

    def look_follow_up(self, analysis: str, question: str) -> str:
        self.follow_ups.append((analysis, question))
        return "Friday at ten, according to what I read."

    def everyday_turn(self, transcript: str, *, context: object = None) -> str:
        self.turns.append(transcript)
        return "general everyday answer"

    def establish_agent_session(self, *_args: object, **_kwargs: object) -> None:
        return None

    def cancel_agent_request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


def make_runtime(**overrides: object) -> tuple[HermesVoiceRuntime, FakeLookClient, list[int]]:
    config = AppConfig(
        camera_enabled=True,
        proactive_presence_enabled=True,
        bridge_url="http://hermes.test:8643",
        api_key="fixture-key",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    config.validate()
    runtime = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    runtime._power_mode = "awake"
    runtime._motors_enabled = True
    runtime._control_ready.set()
    runtime._audio_ready = True
    runtime._status.state = "waiting_for_wake_word"
    runtime._announcement_worker = object()  # type: ignore[assignment]
    runtime._motion = SimpleNamespace(  # type: ignore[assignment]
        enabled=True,
        resume=lambda: None,
        idle=lambda: None,
        thinking=lambda: None,
        suspend=lambda: None,
    )
    client = FakeLookClient()
    runtime._new_bridge_client = lambda _config: client  # type: ignore[method-assign]
    captures: list[int] = []

    def capture(**_kwargs: object) -> bytes:
        captures.append(1)
        return jpeg()

    runtime._capture_camera_jpeg = capture  # type: ignore[method-assign]
    return runtime, client, captures


def someone_present(runtime: HermesVoiceRuntime) -> None:
    runtime.observe_presence(PresenceObservation(source="home_assistant", occupied=True), allow_acknowledgement=False)


# --- 1. Presence: source health vs occupancy transitions -----------------------------------------


def test_heartbeat_refreshes_source_health_without_manufacturing_an_arrival() -> None:
    now = [100.0]
    state = PresenceState(clock=lambda: now[0], stale_after_seconds=30.0)
    assert state.observe(PresenceObservation(source="home_assistant", occupied=True)) == "arrived"
    now[0] += 20
    assert state.observe(PresenceObservation(source="home_assistant", occupied=True), heartbeat=True) == "heartbeat"
    status = state.public_status(enabled=True)
    assert status["occupancy"] == "present" and status["source_health"] == "fresh"
    assert status["source_seen_seconds_ago"] == 0
    for _ in range(5):
        now[0] += 25
        state.observe(PresenceObservation(source="home_assistant", occupied=True), heartbeat=True)
        assert state.public_status(enabled=True)["occupancy"] == "present"


def test_bridge_loss_turns_occupancy_unknown_and_recovery_is_not_a_new_arrival() -> None:
    now = [0.0]
    state = PresenceState(clock=lambda: now[0], stale_after_seconds=30.0)
    state.observe(PresenceObservation(source="home_assistant", occupied=True))
    now[0] += 31
    stale = state.public_status(enabled=True)
    assert stale["occupancy"] == "unknown" and stale["source_health"] == "stale"
    state.mark_unavailable("home_assistant", "bridge_unreachable")
    assert state.public_status(enabled=True)["source_health"] == "unavailable"
    assert state.observe(PresenceObservation(source="home_assistant", occupied=True)) == "updated"
    recovered = state.public_status(enabled=True)
    assert recovered["occupancy"] == "present" and recovered["source_health"] == "fresh"
    assert state.observe(PresenceObservation(source="home_assistant", occupied=False)) == "left"
    assert state.public_status(enabled=True)["occupancy"] == "empty"
    assert state.observe(PresenceObservation(source="home_assistant", occupied=True)) == "arrived"


def test_runtime_acknowledges_once_per_arrival_and_never_wakes_standby() -> None:
    runtime, _client, _captures = make_runtime(presence_acknowledgement_enabled=True)
    actions = Actions()
    runtime._actions = actions  # type: ignore[assignment]
    arrival = PresenceObservation(source="home_assistant", occupied=True)
    first = runtime.observe_presence(arrival)
    assert actions.queued == ["acknowledge_presence"] and first["last_outcome"] == "acknowledgement_queued"
    runtime._on_robot_action_result("acknowledge_presence", {"ok": True})
    again = runtime.observe_presence(arrival)
    assert again["last_outcome"] == "already_present" and actions.queued == ["acknowledge_presence"]
    beat = runtime.observe_presence(arrival, heartbeat=True)
    assert beat["source_health"] == "fresh" and actions.queued == ["acknowledge_presence"]
    runtime, _client, _captures = make_runtime(presence_acknowledgement_enabled=True)
    actions = Actions()
    runtime._actions = actions  # type: ignore[assignment]
    actions.queued.append("acknowledge_presence")  # sentinel: must stay the only entry
    runtime._power_mode = "standby"
    runtime._motors_enabled = False
    motor_changes: list[bool] = []
    runtime._set_motor_mode = lambda enabled, wake=False: motor_changes.append(enabled)  # type: ignore[method-assign]
    standby = runtime.observe_presence(arrival)
    assert standby["occupancy"] == "present" and standby["last_outcome"] == "not_awake"
    assert motor_changes == [] and actions.queued == ["acknowledge_presence"]


# --- 2. Coordinator: compact state, held callbacks, positive control ----------------------------


def test_coordinator_invalidates_held_tickets_and_admits_a_fresh_one() -> None:
    now = [0.0]
    coordinator = CompanionCoordinator(clock=lambda: now[0])
    ticket = coordinator.admit("look", origin="phone", stop_generation=3, capability_profile="conversation")
    ok = dict(stop_generation=3, privacy=False, power_mode="awake", capability_profile="conversation")
    assert coordinator.is_current(ticket, **ok)
    assert coordinator.activity == "looking"
    assert not coordinator.is_current(ticket, **{**ok, "stop_generation": 4})
    assert not coordinator.is_current(ticket, **{**ok, "privacy": True})
    assert not coordinator.is_current(ticket, **{**ok, "power_mode": "sleep"})
    assert not coordinator.is_current(ticket, **{**ok, "capability_profile": "agent"})
    coordinator.invalidate("owner_lost")
    assert not coordinator.is_current(ticket, **ok)
    assert coordinator.activity == "quiet"
    fresh = coordinator.admit("look", origin="voice", stop_generation=4, capability_profile="conversation")
    assert coordinator.is_current(fresh, **{**ok, "stop_generation": 4})
    coordinator.finish(fresh, "answered")
    receipts = coordinator.public_status()["receipts"]
    assert [item["event"] for item in receipts][-3:] == ["invalidated", "admitted", "finished"]
    assert all("letter" not in str(item) for item in receipts)


def test_look_context_is_bounded_and_expires() -> None:
    now = [0.0]
    context = LookContext.create("read this", "a dentist letter", ticket_token=7, now=now[0], ttl_seconds=120.0)
    assert context.live(now=now[0], current_token=7)
    assert not context.live(now=now[0], current_token=8)
    now[0] += 121
    assert not context.live(now=now[0], current_token=7)


# --- 3. Direct looking: one explicit look = one capture, fenced to its generation ---------------


def test_phone_look_captures_once_answers_and_follow_up_is_tool_free_without_capture() -> None:
    runtime, client, captures = make_runtime()
    answer = runtime.look_at_this("What does this say?", origin="phone")
    assert answer["ok"] is True and "dentist" in str(answer["answer"])
    assert answer["captures"] == 1 and answer["processing"] == "host_provider"
    assert captures == [1] and len(client.looks) == 1
    assert runtime.status()["camera_captures"] == 1
    assert not runtime._voice_activity_lock.locked()  # the phone look released the voice slot
    companion = cast(dict[str, object], runtime.status()["companion"])
    assert companion["activity"] == "quiet"
    assert cast(dict[str, object], companion["look"])["context_live"] is True
    follow = runtime.follow_up_look("What day is the appointment?", origin="phone")
    assert follow["ok"] is True and follow["captures"] == 0 and "Friday" in str(follow["answer"])
    assert captures == [1] and len(client.looks) == 1
    assert client.follow_ups == [(client.answer, "What day is the appointment?")]
    assert client.turns == []  # never the tool-capable Everyday turn
    refreshed = runtime.look_at_this("And this one?", origin="phone")
    assert refreshed["captures"] == 1 and captures == [1, 1]


def test_follow_up_without_a_live_look_asks_for_an_explicit_look_not_a_capture() -> None:
    runtime, client, captures = make_runtime()
    with pytest.raises(RuntimeError, match="look first"):
        runtime.follow_up_look("what day?", origin="phone")
    assert captures == [] and client.looks == [] and client.follow_ups == []


def held_look(runtime: HermesVoiceRuntime, client: FakeLookClient) -> tuple[threading.Thread, dict[str, object]]:
    client.release.clear()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = runtime.look_at_this("read this", origin="phone")
        except RuntimeError as exc:
            outcome["error"] = str(exc)

    worker = threading.Thread(target=run)
    worker.start()
    for _ in range(300):
        if client.looks:
            break
        threading.Event().wait(0.01)
    assert client.looks, "the fake provider never received the frame"
    return worker, outcome


def test_stop_during_held_analysis_discards_answer_and_context_then_fresh_look_works() -> None:
    runtime, client, captures = make_runtime()
    worker, outcome = held_look(runtime, client)
    assert captures == [1]
    runtime.stop_speaking()
    client.release.set()
    worker.join(5)
    assert "result" not in outcome and "cancelled" in str(outcome["error"]).lower()
    companion = cast(dict[str, object], runtime.status()["companion"])
    assert cast(dict[str, object], companion["look"])["context_live"] is False
    assert runtime._look_context is None
    fresh = runtime.look_at_this("read this again", origin="phone")
    assert fresh["ok"] is True and captures == [1, 1]


@pytest.mark.parametrize("teardown", ["privacy", "sleep", "kids", "profile"])
def test_lifecycle_change_during_held_analysis_prevents_late_result(teardown: str) -> None:
    runtime, client, captures = make_runtime()
    worker, outcome = held_look(runtime, client)
    if teardown == "privacy":
        runtime._privacy_requested.set()
    elif teardown == "sleep":
        runtime._power_mode = "sleep"
    elif teardown == "kids":
        runtime._kids_active = True
    else:
        runtime._capability_profile = "agent"
    client.release.set()
    worker.join(5)
    assert "result" not in outcome and captures == [1]
    assert runtime._look_context is None


def test_stop_before_capture_admission_yields_zero_frames(monkeypatch) -> None:
    runtime, client, captures = make_runtime()
    original = runtime._capture_camera_jpeg

    def capture(**kwargs: object) -> bytes:
        # Simulate the camera-wait loop: Stop lands while the capture is still waiting for a frame.
        runtime.stop_speaking()
        is_current = kwargs.get("is_current")
        assert callable(is_current) and is_current() is False
        raise RuntimeError("Camera capture was cancelled")

    monkeypatch.setattr(runtime, "_capture_camera_jpeg", capture)
    with pytest.raises(RuntimeError, match="cancelled"):
        runtime.look_at_this("read this", origin="phone")
    assert captures == [] and client.looks == []
    monkeypatch.setattr(runtime, "_capture_camera_jpeg", original)
    assert runtime.look_at_this("read this", origin="phone")["ok"] is True  # positive control


def test_provider_failure_is_categorical_and_leaves_no_context() -> None:
    runtime, client, captures = make_runtime()
    client.fail_with = HermesBridgeError("provider body with personal text")
    with pytest.raises(RuntimeError, match="personal text"):
        runtime.look_at_this("read this", origin="phone")
    assert captures == [1]
    companion = cast(dict[str, object], runtime.status()["companion"])
    look = cast(dict[str, object], companion["look"])
    assert look["context_live"] is False and look["last_failure"] == "bridge_unavailable"
    assert "personal text" not in repr(companion)


def test_uncertain_answer_is_passed_through_not_fabricated() -> None:
    runtime, client, _captures = make_runtime()
    client.answer = "The image is too blurry to read the text; please hold it closer and ask me to look again."
    result = runtime.look_at_this("read this", origin="phone")
    assert "blurry" in str(result["answer"])


def test_look_is_blocked_by_kids_privacy_disabled_camera_busy_voice_and_busy_look_without_capture() -> None:
    runtime, _client, captures = make_runtime(camera_enabled=False)
    with pytest.raises(RuntimeError, match="Camera access is disabled"):
        runtime.look_at_this("read this", origin="phone")
    runtime, _client, captures = make_runtime()
    runtime._kids_active = True
    with pytest.raises(RuntimeError, match="Kids"):
        runtime.look_at_this("read this", origin="phone")
    runtime._kids_active = False
    runtime._privacy_requested.set()
    with pytest.raises(RuntimeError, match="privacy"):
        runtime.look_at_this("read this", origin="phone")
    runtime._privacy_requested.clear()
    assert captures == []
    # A phone look never captures concurrently with a spoken turn that owns the voice slot.
    assert runtime._voice_activity_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="busy with a voice interaction"):
            runtime.look_at_this("read this", origin="phone")
        # ...and a "voice" origin is only honoured while a voice turn actually owns the slot.
        assert runtime.look_at_this("read this", origin="voice")["ok"] is True
    finally:
        runtime._voice_activity_lock.release()
    with pytest.raises(RuntimeError, match="active voice turn"):
        runtime.look_at_this("read this", origin="voice")
    assert captures == [1]
    assert runtime._look_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="already looking"):
            runtime.look_at_this("read this", origin="phone")
    finally:
        runtime._look_lock.release()
    assert captures == [1]


def test_local_vision_when_configured_keeps_frame_local_and_never_falls_back(monkeypatch) -> None:
    runtime, client, captures = make_runtime(local_vision_enabled=True)
    seen: list[str] = []

    class LocalClient:
        def __enter__(self) -> LocalClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def describe(self, frame: bytes, question: str) -> dict[str, object]:
            seen.append(question)
            raise __import__("homebody.local_vision", fromlist=["LocalVisionError"]).LocalVisionError("Jetson offline")

    monkeypatch.setattr(runtime, "_new_local_vision_client", lambda _config: LocalClient())
    with pytest.raises(RuntimeError, match="Jetson offline"):
        runtime.look_at_this("read this", origin="phone")
    assert seen == ["read this"] and client.looks == [] and captures == [1]


def test_look_context_ttl_is_torn_down_by_timer_and_stop_clears_private_ram() -> None:
    runtime, _client, _captures = make_runtime()
    runtime.look_at_this("read this", origin="phone")
    assert runtime._look_context is not None and runtime._companion_timers
    runtime._expire_look_context(runtime._look_context.ticket_token)
    assert runtime._look_context is None
    runtime.look_at_this("read this", origin="phone")
    runtime._remember_private_detail("calendar", "Dentist at eleven")
    runtime.stop_speaking()
    assert runtime._look_context is None and runtime._private_details == []
    assert all(not timer.is_alive() for timer in runtime._companion_timers)


@pytest.mark.parametrize(
    "text",
    [
        "Hey, what does this say?",
        "can you look at this letter",
        "wat staat hier?",
        "kijk eens naar dit",
        "look again",
        "Reachy, read this",
        "what am I holding",
    ],
)
def test_voice_look_intent_accepts_deliberate_requests(text: str) -> None:
    assert looks_like_look_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "what time is it",
        "turn on the desk light",
        "look up the weather for tomorrow",
        "and this one",
        "read the roadmap",
        "what is this project about",
        "lees dit projectplan uit de repository",
        "never look at this",
        'she said "look at this"',
    ],
)
def test_voice_look_intent_rejects_ordinary_requests(text: str) -> None:
    assert not looks_like_look_request(text)


def test_engaged_refresh_phrase_only_counts_while_a_look_is_live() -> None:
    assert not looks_like_look_request("and this one?")
    assert looks_like_look_request("and this one?", engaged=True)
    assert looks_like_look_request("en deze?", engaged=True)
    assert not looks_like_look_request("and this project?", engaged=True)


# --- 4. Suggestions: requested delivery vs unsolicited, disclosure, presentation help ---------


def test_requested_reminder_delivery_bypasses_initiative_budget_but_not_privacy() -> None:
    runtime, _client, _captures = make_runtime(initiative_policy_enabled=False, contextual_offers_enabled=False)
    queued = runtime.queue_announcement("Your tea timer is done.", behavior="voice_only")
    assert queued["queued"] is True
    assert cast(dict[str, object], runtime.status()["initiative"])["initiatives_this_hour"] == 0
    runtime._privacy_requested.set()
    with pytest.raises(RuntimeError, match="Meeting and Sleep"):
        runtime.queue_announcement("Your tea timer is done.", behavior="voice_only")


def calendar_offer(**overrides: object) -> ContextualOffer:
    values: dict[str, object] = dict(
        source="calendar",
        topic="calendar_lead_time",
        confidence=0.95,
        fingerprint="cal:abc:2026-10-16T15:00",
        text="Something on your calendar starts in ten minutes; want the details on your phone?",
        accepted_text="Okay, the details are on your dashboard.",
        private_detail="Dentist, Main Street 5, 15:00",
    )
    values.update(overrides)
    return ContextualOffer(**values)  # type: ignore[arg-type]


def offers_runtime(**overrides: object) -> tuple[HermesVoiceRuntime, FakeLookClient, list[int]]:
    runtime, client, captures = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
        **overrides,
    )
    someone_present(runtime)
    return runtime, client, captures


def test_anonymous_yes_never_speaks_private_calendar_detail_but_owner_dashboard_can_read_it() -> None:
    runtime, _client, _captures = offers_runtime()
    result = runtime.submit_contextual_offer(calendar_offer())
    assert result["queued"] is True
    spoken = runtime._announcement_queue.get_nowait()
    assert "Dentist" not in spoken.text and "Main Street" not in spoken.text
    token = cast(int, result["token"])
    runtime._contextual_offers.mark_spoken(token)
    runtime.respond_to_contextual_offer(token, "yes")
    follow_up = runtime._announcement_queue.get_nowait()
    assert follow_up.text == "Okay, the details are on your dashboard."
    assert "Dentist" not in follow_up.text
    assert "Dentist" not in repr(runtime.status())
    owner_only = runtime.companion_owner_status()
    assert owner_only["private_details"][0]["detail"] == "Dentist, Main Street 5, 15:00"
    duplicate = runtime.submit_contextual_offer(calendar_offer())
    assert duplicate["queued"] is False and duplicate["reason"] in {"duplicate", "topic_cooldown"}


def test_unsolicited_offers_need_present_occupancy_and_project_offers_keep_agent_authority() -> None:
    runtime, _client, _captures = make_runtime(
        initiative_policy_enabled=True,
        initiative_mode="balanced",
        initiative_quiet_hours_enabled=False,
        contextual_offers_enabled=True,
    )
    runtime._capability_profile = "conversation"
    unknown = runtime.submit_contextual_offer(calendar_offer(private_detail=""))
    assert unknown["queued"] is False and unknown["reason"] == "occupancy_unknown"
    runtime.observe_presence(PresenceObservation(source="home_assistant", occupied=False))
    empty = runtime.submit_contextual_offer(calendar_offer(private_detail=""))
    assert empty["queued"] is False and empty["reason"] == "room_empty"
    someone_present(runtime)
    assert runtime.submit_contextual_offer(calendar_offer(private_detail=""))["queued"] is True
    runtime._announcement_queue.get_nowait()
    project = ContextualOffer(
        source="project",
        topic="roadmap_next",
        confidence=0.95,
        fingerprint="proj-1",
        text="Your project has a step waiting; want the summary?",
        accepted_text="The next step is waiting for your approval.",
    )
    blocked = runtime.submit_contextual_offer(project)
    assert blocked["queued"] is False and blocked["reason"] == "agent_profile_inactive"
    runtime._capability_profile = "agent"
    runtime.cancel_contextual_offer("test_cleanup")  # one offer at a time; the calendar one is still pending
    assert runtime.submit_contextual_offer(project)["queued"] is True
    # A timer you asked for is requested delivery: no occupancy or initiative gate.
    runtime.observe_presence(PresenceObservation(source="home_assistant", occupied=False))
    assert runtime.queue_announcement("Your tea timer is done.", behavior="voice_only")["queued"] is True


def presentation_offer() -> ContextualOffer:
    return ContextualOffer(
        source="presentation",
        topic="presented_context",
        confidence=0.95,
        fingerprint="presentation-1",
        text="I can see you are showing me something; would you like me to take a look?",
        accepted_text="Okay, one look.",
    )


def accept_presentation(runtime: HermesVoiceRuntime, monkeypatch) -> threading.Event:
    result = runtime.submit_contextual_offer(presentation_offer())
    runtime._announcement_queue.get_nowait()
    token = cast(int, result["token"])
    runtime._contextual_offers.mark_spoken(token)
    done = threading.Event()
    original = runtime._finish_presentation_help

    def finish(*args: object, **kwargs: object) -> None:
        try:
            original(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(runtime, "_finish_presentation_help", finish)
    runtime.respond_to_contextual_offer(token, "yes")
    return done


def drain(runtime: HermesVoiceRuntime) -> list[str]:
    texts = []
    while not runtime._announcement_queue.empty():
        texts.append(runtime._announcement_queue.get_nowait().text)
    return texts


def test_presentation_yes_finishes_help_with_one_direct_look(monkeypatch) -> None:
    runtime, client, captures = offers_runtime()
    done = accept_presentation(runtime, monkeypatch)
    assert done.wait(5)
    texts = drain(runtime)
    assert texts[0] == "Okay, one look."
    assert any("dentist" in text for text in texts)
    assert captures == [1] and len(client.looks) == 1


def test_presentation_yes_cancelled_by_stop_stays_silent_even_on_failure(monkeypatch) -> None:
    runtime, client, captures = offers_runtime()
    client.release.clear()
    done = accept_presentation(runtime, monkeypatch)
    for _ in range(300):
        if client.looks:
            break
        threading.Event().wait(0.01)
    runtime.stop_speaking()
    client.fail_with = HermesBridgeError("late failure")
    client.release.set()
    assert done.wait(5)
    assert drain(runtime) == ["Okay, one look."]  # no answer and no failure speech after Stop
    assert captures == [1]
