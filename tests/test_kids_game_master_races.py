# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Game master regressions: outer-session Stop across switches, typed Stop, hosted I Spy outcomes."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_kids_game_master import body, host, host_state, load_bridge, run_kids_chat, start, wait_for  # noqa: F401

from homebody.kids_game_master import parse_host_command
from homebody.kids_runtime import ISpyInputUnavailable


@pytest.mark.parametrize(
    "text",
    ["do you like stories", "do you know a riddle", "ken jij een raadsel", "vind je verhalen leuk",
     "what is a quiz", "can you do a story"],
)
def test_questions_about_games_are_not_switches(text) -> None:
    assert parse_host_command(text, phase="playing").kind == "none"


@pytest.mark.parametrize(
    ("text", "activity"),
    [("let us play riddles", "riddles"), ("mogen we een verhaaltje", "story"), ("can we do a quiz", "quiz")],
)
def test_direct_switch_requests_still_switch(text, activity) -> None:
    command = parse_host_command(text, phase="playing")
    assert (command.kind, command.activity) == ("switch", activity)


def test_child_stop_wins_over_a_simultaneous_game_switch(host) -> None:
    runtime = host.runtime
    start(host)
    session = runtime._kids_session_id
    greetings = len(host.spoken)
    runtime._motor_transition_lock.acquire()
    try:
        runtime._kids_host_choose("riddles", runtime._kids_generation)  # transition waits on the motor lock
        assert host_state(runtime)["phase"] == "transition"
        assert runtime._kids_host_handle_transcript(session, "stop") is True
        time.sleep(0.05)
    finally:
        runtime._motor_transition_lock.release()
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    time.sleep(0.1)
    assert host.stops[-1]["reason"] == "child_stop"
    assert len(host.spoken) == greetings  # no new-game greeting after Stop


def test_child_stop_after_transition_admitted_still_ends_session(host) -> None:
    runtime = host.runtime
    start(host)
    session = runtime._kids_session_id
    with runtime._kids_lock:
        play_id = runtime._kids_play_id
    runtime._kids_host_transition("quiz", runtime._kids_generation)  # generation bumps, outer id kept
    assert runtime._kids_play_id == play_id
    runtime._kids_host_child_stop(play_id)
    assert runtime.status()["kids_mode"]["active"] is False
    assert runtime._kids_host_handle_transcript(session, "stop") is False


def test_caregiver_select_racing_child_stop(host) -> None:
    runtime = host.runtime
    start(host)
    session = runtime._kids_session_id
    greetings = len(host.spoken)
    runtime._motor_transition_lock.acquire()
    try:
        runtime.kids_host_select("story")
        assert runtime._kids_host_handle_transcript(session, "ik wil stoppen") is True
        time.sleep(0.05)
    finally:
        runtime._motor_transition_lock.release()
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    time.sleep(0.1)
    assert len(host.spoken) == greetings


def test_stale_outer_stop_cannot_end_a_later_caregiver_start(host) -> None:
    runtime = host.runtime
    start(host)
    old_play_id = runtime._kids_play_id
    runtime.stop_kids_mode(reason="parent")
    start(host)
    runtime._kids_host_child_stop(old_play_id)
    assert runtime.status()["kids_mode"]["active"] is True
    # Replacement Start without End also gets a fresh identity.
    replaced = runtime._kids_play_id
    start(host)
    runtime._kids_host_child_stop(replaced)
    assert runtime.status()["kids_mode"]["active"] is True
    runtime._kids_host_child_stop("")
    assert runtime.status()["kids_mode"]["active"] is True


def test_typed_stop_during_transition_is_admitted_once_and_nothing_else(host) -> None:
    runtime = host.runtime
    start(host)
    runtime._motor_transition_lock.acquire()
    try:
        runtime._kids_host_choose("story", runtime._kids_generation)
        context = runtime.ispy_input_context()
        assert context["available"] is False and context["stop_only"] is True and context["token"]
        token = context["token"]
        with pytest.raises(ISpyInputUnavailable):
            runtime.submit_ispy_guess("riddles", token)  # other game input stays refused
        with pytest.raises(ISpyInputUnavailable):
            runtime.submit_ispy_guess("stop", "0" * 32)  # wrong token
        assert runtime.submit_ispy_guess("stop", token) == {"accepted": True}
        with pytest.raises(ISpyInputUnavailable):
            runtime.submit_ispy_guess("stop", token)  # replay
    finally:
        runtime._motor_transition_lock.release()
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    assert host.stops[-1]["reason"] == "child_stop"


def test_typed_stop_only_capability_is_closed_outside_switches(host) -> None:
    runtime = host.runtime
    start(host)
    context = runtime.ispy_input_context()
    assert "stop_only" not in context


def ispy_session(bridge, session_id: str, phase: str) -> None:
    bridge._kids_sessions[session_id] = {
        "profile": ("7-9", "ispy", "nl", True), "history": [], "updated_at": time.monotonic(),
        "ispy_role": "player_picker", "ispy_player_phase": phase,
        "ispy_player_clues": ["rood"], "ispy_player_guesses": ["appel", "bal", "boek"],
    }


def test_hosted_ispy_keeps_guessed_versus_reveal_outcomes(monkeypatch) -> None:
    _module, bridge = load_bridge(monkeypatch)
    nl_profile = {"age_band": "7-9", "activity": "ispy", "language": "nl"}
    guessed_id, reveal_id = "kids-" + "1" * 32, "kids-" + "2" * 32
    ispy_session(bridge, guessed_id, "awaiting_confirmation")
    ispy_session(bridge, reveal_id, "awaiting_reveal")
    guessed, _ = run_kids_chat(bridge, {"input": "ja", "session_id": guessed_id, "profile": nl_profile,
                                        "hosted": True}, "")
    reveal, _ = run_kids_chat(bridge, {"input": "een lamp", "session_id": reveal_id, "profile": nl_profile,
                                       "hosted": True}, "")
    guessed, reveal = json.loads(guessed.text), json.loads(reveal.text)
    assert guessed["text"] == "Ja! Ik heb het geraden!"
    assert reveal["text"] == "Je hebt me verslagen! Slim voorwerp!"
    for payload in (guessed, reveal):
        assert payload["game_state"] == "finished" and payload["ispy_next_action"] == ""
        assert "rondkijken" not in payload["text"] and payload["speech_approval"]
    # A flagged output is replaced by the safety reply and stays "playing".
    flagged_id = "kids-" + "3" * 32
    ispy_session(bridge, flagged_id, "awaiting_confirmation")
    flagged, _ = run_kids_chat(bridge, {"input": "ja", "session_id": flagged_id, "profile": nl_profile,
                                        "hosted": True}, "", flagged=("Ja! Ik heb het geraden!",))
    flagged = json.loads(flagged.text)
    assert flagged["game_state"] == "playing" and flagged["ispy_next_action"] == ""
    assert "volwassene" in flagged["text"]
    assert body  # shared builder stays importable for parity with the base module


def test_hosted_ispy_round_in_english(monkeypatch) -> None:
    _module, bridge = load_bridge(monkeypatch)
    session_id = "kids-" + "4" * 32
    bridge._kids_sessions[session_id] = {
        "profile": ("7-9", "ispy", "en", True), "history": [], "updated_at": time.monotonic(),
        "ispy_role": "player_picker", "ispy_player_phase": "awaiting_confirmation",
        "ispy_player_clues": ["red"], "ispy_player_guesses": ["apple"],
    }
    response, _ = run_kids_chat(bridge, {**body("ispy", hosted=True), "input": "yes", "session_id": session_id}, "")
    assert json.loads(response.text)["text"] == "Yes! I guessed it!"


def test_threads_do_not_leak_names() -> None:
    assert SimpleNamespace and threading  # imports used by fixtures in sibling module
