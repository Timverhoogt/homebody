# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""On-robot STT through the current game master: real switches, End, child Stop and typed dispatch.

Uses the real hosted Kids session (``kids_host_select`` / ``_kids_host_transition``) and the real
``_run_conversation`` loop. The recognizer is an explicit fake; a fake success is not device proof.
"""

from __future__ import annotations

import ast
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_kids_game_master import FakeKidsClient, host, host_state, start, wait_for  # noqa: F401
from test_kids_stt_acknowledgements import Harness
from test_robot_stt_pipeline import FakeRobotStt

from homebody.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]


class Turn:
    """Wires a hosted runtime to the fake recognizer and a host client that records every call."""

    def __init__(self, runtime, robot, *, fallback="none", replies=(), speech=(True,)):
        self.runtime = runtime
        self.robot = robot
        self.calls: list[str] = []
        self.host_calls: list[object] = []
        self.errors: list[int] = []
        self.speech = list(speech)
        self.fallback = fallback
        turn = self
        replies = list(replies)

        class Client(FakeKidsClient):
            last_stt_receipt = ""

            def health(self):
                return {"stt_locality": "local"}

            def transcribe(self, _wav, *, provider=None):
                turn.host_calls.append(provider)
                self.last_stt_receipt = "local"
                return "Raadsels!"

        runtime._robot_stt = robot
        runtime._new_bridge_client = lambda config: Client(config, replies, self.calls)  # type: ignore[method-assign]
        runtime._play_kids_stream = lambda client, text, **kw: False  # type: ignore[method-assign]
        runtime._start_kids_ack = lambda *a, **k: None  # type: ignore[method-assign]
        runtime._play_asset = lambda *_a, **_k: None  # type: ignore[method-assign]
        runtime._discard_audio = lambda *_a, **_k: None  # type: ignore[method-assign]
        runtime._signal_error = lambda: self.errors.append(1)  # type: ignore[method-assign]

        def record(_config, **_kwargs):
            heard = self.speech.pop(0) if self.speech else False
            samples = np.ones(1600, dtype=np.float32) if heard else np.empty(0, dtype=np.float32)
            return SimpleNamespace(speech_detected=heard, samples=samples, last_voiced_at=None)

        runtime._record_utterance = record  # type: ignore[method-assign]

    def config(self) -> AppConfig:
        config = self.runtime._kids_voice_config(AppConfig())
        return replace(config, stt_provider="robot", stt_robot_fallback=self.fallback)

    def run(self, config: AppConfig | None = None, **kwargs) -> None:
        self.runtime._accept_wake_turn()
        self.runtime._run_conversation(config or self.config(), **kwargs)


def outer(runtime) -> tuple[str, float]:
    with runtime._kids_lock:
        return runtime._kids_play_id, runtime._kids_ends_at


def provenance(runtime) -> tuple[str, bool, str]:
    status = runtime._status
    return status.stt_processing_location, status.stt_processing_verified, status.stt_locality


def switch_to_quiz(runtime) -> None:
    runtime.kids_host_select("quiz")  # the caregiver route; the transition runs on its own thread
    wait_for(lambda: host_state(runtime)["activity"] == "quiz" and host_state(runtime)["phase"] == "playing")


def test_stale_robot_final_after_real_game_switch_is_never_dispatched(host) -> None:
    runtime = host.runtime
    start(host, language="nl")
    old_session = runtime._kids_session_id
    play_id, ends_at = outer(runtime)
    robot = FakeRobotStt(texts=("Raadsels!",))  # a switch command, if it ever reached the host
    turn = Turn(runtime, robot, fallback="host")
    robot.on_call = lambda: switch_to_quiz(runtime)
    turn.run()
    # The held decode finished after the switch: discarded, never a riddles switch or model turn.
    assert host_state(runtime)["activity"] == "quiz"
    assert not any(text.startswith("Oké, raadsels!") for text, _ in host.spoken)
    assert turn.calls == [] and turn.host_calls == [] and turn.errors == []
    assert runtime._kids_session_id != old_session
    assert outer(runtime) == (play_id, ends_at)  # outer play id and deadline are unchanged
    assert provenance(runtime) == ("unknown", False, "unknown")


def test_next_turn_after_switch_uses_new_session_generation_and_profile_language(host) -> None:
    runtime = host.runtime
    start(host, language="nl")
    robot = FakeRobotStt(texts=("Raadsels!", "een antwoord"))
    turn = Turn(runtime, robot, replies=[("Goed zo!", "")])
    robot.on_call = lambda: switch_to_quiz(runtime) if len(robot.calls) == 1 else None
    turn.run()
    first = robot.calls[0]
    turn.speech = [True]
    turn.run()  # the next microphone turn belongs to the new game
    second = robot.calls[1]
    assert second["session"] == runtime._kids_session_id != first["session"]
    assert second["generation"] > first["generation"]
    assert first["language"] == second["language"] == "nl"  # parent-selected, never auto for Kids
    assert turn.calls == ["een antwoord"] and turn.host_calls == []


def test_held_robot_decode_then_end_cancels_without_fallback(host) -> None:
    runtime = host.runtime
    start(host)
    robot = FakeRobotStt(hang=True)
    turn = Turn(runtime, robot, fallback="host")
    robot.on_call = lambda: runtime.stop_kids_mode(reason="parent")
    started = time.monotonic()
    turn.run()
    assert time.monotonic() - started < 3
    assert runtime.status()["kids_mode"]["active"] is False
    assert turn.calls == [] and turn.host_calls == [] and turn.errors == []
    assert provenance(runtime) == ("unknown", False, "unknown")


@pytest.mark.parametrize("boundary", ["privacy", "sleep", "meeting"])
@pytest.mark.parametrize("error", [None, "decode_timeout"])
def test_privacy_or_power_during_decode_fences_final_failure_and_fallback(host, boundary, error) -> None:
    runtime = host.runtime
    start(host)
    robot = FakeRobotStt(texts=("Raadsels!",), error=error)
    turn = Turn(runtime, robot, fallback="host")

    def cross_boundary() -> None:
        if boundary == "privacy":
            runtime._privacy_requested.set()
        else:
            with runtime._power_lock:
                runtime._power_mode = boundary
                runtime._meeting_until = time.monotonic() + 60.0  # a meeting lapses at this deadline

    robot.on_call = cross_boundary
    try:
        turn.run()
    finally:
        runtime._privacy_requested.clear()
    assert turn.calls == [] and turn.host_calls == [] and turn.errors == []
    assert host_state(runtime)["activity"] != "riddles"
    assert provenance(runtime) == ("unknown", False, "unknown")


@pytest.mark.parametrize("fallback", ["none", "host"])
def test_stale_robot_error_after_switch_has_no_fallback_or_error_notice(host, fallback) -> None:
    runtime = host.runtime
    start(host)
    robot = FakeRobotStt(error="decode_timeout")
    turn = Turn(runtime, robot, fallback=fallback)
    robot.on_call = lambda: switch_to_quiz(runtime)
    turn.run()
    assert turn.host_calls == []  # cancelled turns never fall back
    assert turn.errors == [] and runtime._status.last_error == ""  # nothing surfaces in the new game
    assert host_state(runtime)["activity"] == "quiz"


def test_current_robot_error_is_infrastructure_not_silence_in_hosted_play(host) -> None:
    runtime = host.runtime
    start(host)
    turn = Turn(runtime, FakeRobotStt(error="load_failed"), speech=(True, True, True))
    turn.run()
    assert len(turn.robot.calls) == 1  # no empty-turn re-listen loop
    assert turn.errors == [1] and runtime._status.last_error == "Speech recognition on Reachy is unavailable"
    assert turn.calls == [] and turn.host_calls == []


def test_host_fallback_is_at_most_once_per_turn_and_local_only(host) -> None:
    runtime = host.runtime
    start(host, language="nl")
    turn = Turn(runtime, FakeRobotStt(error="decode_timeout"), fallback="host")
    turn.run()
    assert turn.host_calls == ["local"]  # one local request; the fallback text was a real switch
    wait_for(lambda: host_state(runtime)["activity"] == "riddles")


def test_child_stop_during_decode_wins_over_a_stale_switch_final(host) -> None:
    runtime = host.runtime
    start(host)
    session = runtime._kids_session_id
    greetings = len(host.spoken)
    robot = FakeRobotStt(texts=("let's do riddles",))
    turn = Turn(runtime, robot, fallback="host")
    robot.on_call = lambda: runtime._kids_host_handle_transcript(session, "stop", typed=True)
    turn.run()
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    assert host.stops[-1]["reason"] == "child_stop"
    assert len(host.spoken) == greetings and turn.calls == [] and turn.host_calls == []


def test_typed_host_command_never_touches_the_recognizer(host) -> None:
    runtime = host.runtime
    start(host)
    robot = FakeRobotStt()
    turn = Turn(runtime, robot)
    runtime._kids_host_typed_handled = False
    turn.run(typed_input="let's do a story")
    assert runtime._kids_host_typed_handled is True
    wait_for(lambda: host_state(runtime)["activity"] == "story")
    assert robot.calls == [] and turn.host_calls == []


def test_acknowledgement_filler_still_covers_a_slow_robot_decode(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.0)
    harness.config = replace(harness.config, stt_provider="robot")
    robot = FakeRobotStt(on_call=lambda: time.sleep(0.6))
    harness.runtime._robot_stt = robot
    harness.run()
    assert len(harness.media.pushes()) >= 1  # one cached clip while recognition was pending
    assert sum(1 for label, _ in harness.calls if label == "chat") == 1


def test_acknowledgement_producer_is_fenced_when_the_session_ends_during_decode(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.0)
    harness.config = replace(harness.config, stt_provider="robot")
    robot = FakeRobotStt(hang=True, on_call=lambda: harness.runtime.stop_kids_mode(fold=False))
    harness.runtime._robot_stt = robot
    harness.run()
    time.sleep(0.4)  # past the filler delay: a fenced producer never pushes
    assert harness.media.pushes() == []
    assert sum(1 for label, _ in harness.calls if label == "chat") == 0


def test_adult_pipeline_turn_autodetects_and_saved_engine_is_untouched(host) -> None:
    runtime = host.runtime
    robot = FakeRobotStt(texts=("what time is it",))
    turn = Turn(runtime, robot)
    config = AppConfig(api_key="k", conversation_mode="live", stt_provider="robot", language="nl",
                       continuous_conversation=False)
    runtime._new_bridge_client = lambda cfg: SimpleNamespace(  # type: ignore[method-assign]
        config=cfg, last_stt_provider="", health=lambda: {},
        everyday_turn=lambda text, *, context=None, **local_tools: "Noon.",
        synthesize=lambda text: (_ for _ in ()).throw(RuntimeError("stop after STT")), close=lambda: None,
    )
    try:
        turn.run(config)
    except RuntimeError:
        pass
    assert robot.calls[0]["language"] == "auto" and robot.calls[0]["session"] == ""
    assert config.conversation_mode == "live"  # the saved adult engine is not switched by routing


def test_teardown_closes_the_owned_recognizer_and_clears_it(host) -> None:
    runtime = host.runtime
    closed: list[int] = []
    runtime._robot_stt = SimpleNamespace(close=lambda: closed.append(1))
    runtime._teardown_runtime()
    assert closed == [1] and runtime._robot_stt is None


def test_robot_adapter_owns_no_media_sdk_or_network_client() -> None:
    allowed = {
        "asr_worker.py": {"__future__", "argparse", "ctypes", "fcntl", "hashlib", "json", "math", "os", "re",
                          "resource", "signal", "socket", "stat", "sys", "time", "pathlib"},
        "robot_stt.py": {"__future__", "itertools", "json", "os", "socket", "struct", "subprocess", "sys",
                         "tempfile", "threading", "time", "collections", "dataclasses", "pathlib", "numpy",
                         ".", ".asr_worker", ".config"},
        # Timing/diagnostic stdlib imports do not add a media or network owner.
        "stt_routing.py": {"__future__", "collections", "dataclasses", "logging", "time", "numpy", ".audio", ".config",
                           ".robot_stt"},
    }
    for name, modules in allowed.items():
        tree = ast.parse((ROOT / "homebody" / name).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {"." * node.level + (node.module or "") if node.level else (node.module or "").split(".")[0]
                     for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert imported <= modules, (name, imported - modules)  # no SDK, media or network client
