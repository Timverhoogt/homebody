"""Expected I Spy cancellation is quiet; genuine current-session failures still report.

Reproduces the deployed race: caregiver End while the voice dispatcher's I Spy continuation is
blocked in ``_prepare_ispy_round -> _capture_camera_jpeg``. Synthetic fakes only.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from test_kids_interaction_fixes import profile_for
from test_kids_stt_acknowledgements import Harness

from homebody.hermes_client import HermesBridgeError
from homebody.ispy import ISpyCancelled
from homebody.runtime import HermesVoiceRuntime

_REAL_PREPARE = HermesVoiceRuntime._prepare_ispy_round


class Rig:
    """A Kids I Spy voice turn whose bridge reply asks Reachy to search for the next round."""

    def __init__(self, monkeypatch, tmp_path, *, frame=lambda: b"jpeg") -> None:
        # Start's own round is not under test; the continuation uses the real search below.
        monkeypatch.setattr(HermesVoiceRuntime, "_prepare_ispy_round", lambda self, *_a, **_k: SimpleNamespace())
        harness = Harness(monkeypatch, tmp_path, activity="ispy", stt_seconds=0.01, catalog=False)
        monkeypatch.setattr(HermesVoiceRuntime, "_prepare_ispy_round", _REAL_PREPARE)
        self.harness = harness
        runtime = self.runtime = harness.runtime
        self.config = harness.config
        self.session_id = runtime._kids_session_id
        self.events: list[str] = []
        self.stops: list[dict[str, object]] = []
        self.errors: list[bool] = []
        self.cancelled_sessions: list[str] = []
        self.frame = frame
        self.clue_gate: threading.Event | None = None
        self.clue_entered = threading.Event()
        rig = self

        def capture() -> bytes:
            rig.events.append("camera")
            return rig.frame()

        harness.media.get_frame_jpeg = capture
        runtime.robot.goto_target = lambda **_kw: rig.events.append("motion")

        def confirm_awake(*_args, **_kwargs):
            runtime._power_mode = "awake"
            runtime._motors_enabled = True
            runtime._head_safely_folded = False

        runtime.set_power_mode = confirm_awake  # type: ignore[method-assign]
        runtime._signal_error = lambda: rig.errors.append(True)  # type: ignore[method-assign]
        original_stop = runtime.stop_kids_mode

        def record_stop(**kwargs):
            rig.stops.append(kwargs)
            return original_stop(**{**kwargs, "fold": False})

        runtime.stop_kids_mode = record_stop  # type: ignore[method-assign]

        def speak(*_args, **_kwargs):
            rig.events.append("tts")
            return False

        runtime._play_kids_stream = speak  # type: ignore[method-assign]
        self.selection_error: Exception | None = None

        class Client:
            last_stt_provider = "local"
            last_kids_ispy_role = ""
            last_kids_ispy_phase = ""
            last_kids_next_action = ""

            def __init__(self, cfg):
                self.config = cfg

            def health(self):
                return {"status": "ok", "stt_locality": "local"}

            def transcribe(self, _audio):
                return "is it the chair"

            def chat(self, _text):
                self.last_kids_next_action = "prepare_robot_round"
                return "Yes! My turn."

            def select_ispy_target(self, frames, **_kw):
                rig.events.append("provider")
                if rig.selection_error is not None:
                    raise rig.selection_error
                return SimpleNamespace(clue=lambda language: "clue")

            def cancel_ispy_session(self, session_id):
                rig.cancelled_sessions.append(session_id)

            def ispy_clue(self, _session_id):
                rig.events.append("clue")
                if rig.clue_gate is not None:
                    rig.clue_entered.set()
                    rig.clue_gate.wait(3)
                    raise HermesBridgeError("Hermes bridge request failed")
                return "I spy something blue."

            def close(self):
                pass

        runtime._new_bridge_client = Client  # type: ignore[method-assign]

    def run(self) -> None:
        self.runtime._accept_wake_turn()
        self.runtime._run_selected_voice_conversation(self.config, "kids_start")

    def run_in_thread(self) -> tuple[threading.Thread, list[BaseException]]:
        raised: list[BaseException] = []

        def target() -> None:
            try:
                self.run()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test thread
                raised.append(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, raised

    def end_and_replace(self) -> str:
        """Caregiver End, then a fresh session owns Kids Mode before the old work resumes."""
        self.runtime.stop_kids_mode(reason="parent", fold=False)
        self.runtime.start_kids_mode(profile_for("quiz", "nl"), greet=False)
        return self.runtime._kids_session_id

    def assert_quiet_and_replacement_untouched(self, replacement: str, raised) -> None:
        runtime = self.runtime
        assert raised == []  # nothing reaches the wake loop's broad error handler
        assert self.errors == []
        assert runtime._status.state != "error"
        assert runtime._status.last_error == ""
        assert [stop["reason"] for stop in self.stops] == ["parent"]  # no ispy_round_failed
        assert runtime.status()["kids_mode"]["active"] is True
        assert runtime._kids_session_id == replacement
        assert runtime._kids_profile.activity == "quiz"
        assert runtime._kids_camera_active is False
        # Only End's own cleanup may delete a bridge I Spy session, and only the ended one.
        assert set(self.cancelled_sessions) <= {self.session_id}


def test_end_during_continuation_camera_wait_is_quiet_and_spares_replacement(monkeypatch, tmp_path) -> None:
    entered, release = threading.Event(), threading.Event()

    def held_frame() -> bytes:
        entered.set()
        release.wait(3)
        return b"jpeg"

    rig = Rig(monkeypatch, tmp_path, frame=held_frame)
    thread, raised = rig.run_in_thread()
    assert entered.wait(3), "continuation never reached the camera"
    replacement = rig.end_and_replace()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    # Exactly the held readiness frame: no further capture, motion, provider, clue or clue TTS.
    assert rig.events == ["tts", "camera"]
    rig.assert_quiet_and_replacement_untouched(replacement, raised)


def test_end_while_awaiting_clue_discards_late_bridge_failure(monkeypatch, tmp_path) -> None:
    rig = Rig(monkeypatch, tmp_path)
    rig.clue_gate = threading.Event()
    thread, raised = rig.run_in_thread()
    assert rig.clue_entered.wait(3)
    replacement = rig.end_and_replace()
    rig.clue_gate.set()
    thread.join(5)
    assert not thread.is_alive()
    assert rig.events[-1] == "clue" and rig.events.count("tts") == 1  # no clue TTS admitted
    rig.assert_quiet_and_replacement_untouched(replacement, raised)


def test_stale_continuation_preflight_is_typed_cancellation(monkeypatch, tmp_path) -> None:
    rig = Rig(monkeypatch, tmp_path)
    client = SimpleNamespace(config=SimpleNamespace(kids_session_id="kids-" + "0" * 32))
    with pytest.raises(ISpyCancelled):
        rig.runtime._continue_ispy_reachy_turn(client, barge_in=False)  # type: ignore[arg-type]
    assert rig.stops == [] and rig.events == []


def test_healthy_continuation_speaks_the_next_clue(monkeypatch, tmp_path) -> None:
    rig = Rig(monkeypatch, tmp_path)
    rig.run()
    assert rig.events.count("camera") == 6 and "provider" in rig.events
    assert rig.events[-2:] == ["clue", "tts"]
    assert rig.errors == [] and rig.stops == []
    assert rig.runtime._kids_session_id == rig.session_id
    assert rig.runtime.status()["kids_mode"]["active"] is True


def test_active_camera_failure_still_stops_and_reports(monkeypatch, tmp_path) -> None:
    def broken() -> bytes:
        raise RuntimeError("camera hardware fault")

    rig = Rig(monkeypatch, tmp_path, frame=broken)
    with pytest.raises(RuntimeError, match="camera hardware fault") as caught:
        rig.run()
    assert not isinstance(caught.value, ISpyCancelled)
    assert [stop["reason"] for stop in rig.stops] == ["ispy_round_failed"]
    assert rig.runtime.status()["kids_mode"]["last_end_reason"] == "ispy_round_failed"
    assert "provider" not in rig.events and rig.events.count("tts") == 1


def test_active_provider_failure_still_reports_bridge_error(monkeypatch, tmp_path) -> None:
    rig = Rig(monkeypatch, tmp_path)
    rig.selection_error = HermesBridgeError("Hermes bridge request failed")
    with pytest.raises(HermesBridgeError):
        rig.run()
    assert [stop["reason"] for stop in rig.stops] == ["ispy_round_failed"]
    assert rig.runtime._status.state == "error"
    assert "clue" not in rig.events and rig.events.count("tts") == 1


def test_kids_camera_fence_raises_typed_cancellation_but_generic_capture_does_not(monkeypatch, tmp_path) -> None:
    rig = Rig(monkeypatch, tmp_path)
    runtime = rig.runtime
    generation = runtime._kids_generation
    with pytest.raises(ISpyCancelled):  # camera not armed for this generation
        runtime._capture_camera_jpeg(kids_generation=generation)
    runtime.stop_event.set()
    try:
        with pytest.raises(RuntimeError) as caught:
            runtime._capture_camera_jpeg()
        assert not isinstance(caught.value, ISpyCancelled)
    finally:
        runtime.stop_event.clear()
