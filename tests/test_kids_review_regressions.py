"""Regressions for the independent child-safety review of the Kids interaction fixes.

Synthetic fixtures only: no audio, child speech, model weights or network.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from test_kids_interaction_fixes import (
    TARGET,
    IspyBridge,
    StartHarness,
    _Robot,
    _SpeechClient,
    profile_for,
    wait_for,
)

from companion import local_stt_command as stt
from homebody.audio import EndpointResult
from homebody.runtime import HermesVoiceRuntime

ROOT = Path(__file__).resolve().parents[1]


class _PipelineClient(_SpeechClient):
    def health(self) -> dict[str, str]:
        return {"status": "ok"}

    def establish_agent_session(self, *_args: object, **_kwargs: object) -> None:
        pass


def real_pipeline(harness: StartHarness, monkeypatch: pytest.MonkeyPatch, recordings: list[str]) -> None:
    """Route the real wake loop into the real selected Kids pipeline up to the microphone."""
    runtime = harness.runtime
    runtime._run_selected_voice_conversation = (  # type: ignore[method-assign]
        HermesVoiceRuntime._run_selected_voice_conversation.__get__(runtime)
    )
    monkeypatch.setattr(runtime, "_new_bridge_client", _PipelineClient)
    monkeypatch.setattr(runtime, "_play_asset", lambda _name: None)
    monkeypatch.setattr(runtime, "_discard_audio", lambda _seconds: None)

    def record(config):
        recordings.append(config.kids_session_id)
        return EndpointResult(np.empty(0, dtype=np.float32), False, "initial_timeout", 0.01)

    monkeypatch.setattr(runtime, "_record_utterance", record)


# --- Finding 1: Stop must defeat an armed first-listen ticket ----------------------------------


def test_stop_after_greeting_defeats_armed_first_listen_but_new_start_still_listens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = StartHarness(monkeypatch)
    recordings: list[str] = []
    real_pipeline(harness, monkeypatch, recordings)
    runtime = harness.runtime
    try:
        worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
        runtime._announcement_worker = worker
        harness.threads.append(worker)
        worker.start()
        runtime.start_kids_mode(profile_for("story", "nl"), greet=True)
        assert harness.playback_started.wait(2)
        harness.release_playback.set()
        assert wait_for(lambda: runtime._kids_first_listen is not None and not runtime._announcement_active.is_set())
        runtime.stop_speaking()  # Stop after arming, before the wake consumer runs
        wake = threading.Thread(target=runtime._listen_for_wake_word, daemon=True)
        harness.threads.append(wake)
        wake.start()
        harness.settle(0.3)
        assert recordings == []

        # A legitimate later Parent Start is unaffected and opens its own first turn.
        harness.playback_started.clear()
        runtime.start_kids_mode(profile_for("quiz", "en"), greet=True)
        session_id = runtime._kids_session_id
        assert wait_for(lambda: bool(recordings))
        assert recordings[0] == session_id
    finally:
        harness.close()


def test_stop_during_greeting_prevents_first_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch)
    recordings: list[str] = []
    real_pipeline(harness, monkeypatch, recordings)
    try:
        harness.start_threads()
        harness.runtime.start_kids_mode(profile_for("buddy", "en"), greet=True)
        assert harness.playback_started.wait(2)
        harness.runtime._request_conversation_stop()  # e.g. Stop/Standby while the greeting plays
        harness.release_playback.set()
        harness.settle(0.3)
        assert recordings == []
        assert harness.runtime._kids_first_listen is None
    finally:
        harness.close()


# --- Re-review: a completed Stop must beat an already-pending Start ----------------------------


@pytest.mark.parametrize("boundary", ["notification", "after_start_receipt", "none"])
def test_completed_stop_during_pending_start_admits_no_motor_or_microphone(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """Hold the real Start at each unlocked callback, complete a real Stop, then resume Start.

    Contract: Start takes its baseline as the receipt of its own cancellation, atomically under
    the stop-generation lock and inside Start's admission locks. A Stop that completes at any
    later point therefore leaves the baseline stale: zero motor wakes and zero microphone turns.
    """
    harness = StartHarness(monkeypatch)
    runtime = harness.runtime
    recordings: list[str] = []
    real_pipeline(harness, monkeypatch, recordings)
    monkeypatch.setattr(runtime, "_publish_remote_agent_session", lambda: None)
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []
    motor_calls: list[tuple[bool, dict[str, object]]] = []
    original_motor = runtime._set_motor_mode

    def motor(enabled: bool, **kwargs: object) -> None:
        motor_calls.append((enabled, kwargs))
        original_motor(enabled, **kwargs)

    monkeypatch.setattr(runtime, "_set_motor_mode", motor)
    if boundary == "notification":

        def notify(_session: str, *, active: bool) -> None:
            if active:
                held.set()
                assert release.wait(3)

        monkeypatch.setattr(runtime, "_notify_bridge_kids_session_async", notify)
    elif boundary == "after_start_receipt":
        original_stop = runtime._request_conversation_stop
        calls: list[int] = []

        def stop() -> int:
            receipt = original_stop()  # faithful hook: the real receipt is preserved
            if threading.current_thread().name == "pending-start":
                calls.append(receipt)
                if len(calls) == 2:  # Start's own receipt-taking cancellation
                    held.set()
                    assert release.wait(3)
            return receipt

        monkeypatch.setattr(runtime, "_request_conversation_stop", stop)

    def start() -> None:
        try:
            runtime.start_kids_mode(profile_for("story", "nl"), greet=True)
        except BaseException as exc:  # surfaced below
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=start, name="pending-start")
    try:
        harness.start_threads()
        thread.start()
        if boundary != "none":
            assert held.wait(2)
            runtime.stop_speaking()  # real, completed Stop while Start is held
            assert runtime._conversation_stop_requested.is_set()
            release.set()
        assert done.wait(3)
        assert errors == []
        harness.release_playback.set()
        if boundary == "none":
            # Positive control: without Stop the same path admits exactly one first turn.
            assert wait_for(lambda: bool(recordings))
            assert motor_calls[:1] == [(True, {"wake": True})]
        else:
            harness.settle(0.4)
            assert harness.played  # the greeting itself still plays
            assert motor_calls == []
            assert recordings == []
            assert runtime._kids_first_listen is None
    finally:
        release.set()
        thread.join(3)
        harness.close()


def test_conversation_stop_returns_an_atomic_receipt() -> None:
    runtime = HermesVoiceRuntime(_Robot(), threading.Event())
    first = runtime._request_conversation_stop()
    second = runtime._request_conversation_stop()
    assert second == first + 1 == runtime._conversation_stop_generation
    assert runtime._conversation_stop_requested.is_set()


# --- Finding 2: End after ticket consumption must not re-enable motors --------------------------


def test_end_after_ticket_consumption_never_reenables_motors(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = StartHarness(monkeypatch)
    recordings: list[str] = []
    real_pipeline(harness, monkeypatch, recordings)
    runtime = harness.runtime
    consumed, release = threading.Event(), threading.Event()
    original_take = runtime._take_kids_first_listen
    motor_calls: list[bool] = []
    original_motor = runtime._set_motor_mode

    def motor(enabled: bool, **kwargs: object) -> None:
        motor_calls.append(enabled)
        original_motor(enabled, **kwargs)

    def take(session_id: str):
        ticket = original_take(session_id)
        if ticket is not None:
            consumed.set()
            assert release.wait(3)
        return ticket

    monkeypatch.setattr(runtime, "_take_kids_first_listen", take)
    monkeypatch.setattr(runtime, "_set_motor_mode", motor)
    try:
        harness.start_threads()
        runtime.start_kids_mode(profile_for("quiz", "en"), greet=True)
        harness.release_playback.set()
        assert consumed.wait(2)
        runtime._head_safely_folded = True  # synthetic positive fold sensor
        runtime.stop_kids_mode(fold=True)  # real default End teardown
        assert runtime._power_mode == "standby" and runtime._motors_enabled is False
        assert runtime._kids_last_fold_succeeded is True
        release.set()
        harness.settle(0.3)
        assert motor_calls == []
        assert recordings == []
        assert runtime._motors_enabled is False
    finally:
        release.set()
        harness.close()


# --- Finding 3: approved hints never repeat ----------------------------------------------------


@pytest.mark.parametrize(
    ("language", "hints", "request_text"),
    [
        ("en", ["Look for something blue.", "Look for something blue."], "hint"),
        ("en", ["You can sit on it", "  you can SIT on it!! ", "Look for something blue"], "give me a hint"),
        ("nl", ["Je kunt erop zitten.", "je kunt erop zitten", "Zoek iets dat blauw is."], "mag ik een hint"),
    ],
)
def test_duplicate_validated_hints_never_repeat_and_exhaustion_reveals(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    hints: list[str],
    request_text: str,
) -> None:
    game = IspyBridge(monkeypatch, language=language)
    raw = dict(
        TARGET,
        location="next to the table",
        frame_index=0,
        bbox=[0.1, 0.1, 0.3, 0.3],
        confidence=0.9,
        stable=True,
        visible_frame_count=2,
        **{f"hints_{language}": hints},
    )
    game.state["ispy_target"] = game.module._validate_bridge_ispy_target(raw, frame_count=5)
    answers = [game.say(request_text)["text"] for _ in range(3)]
    assert len(set(answers)) == 3
    reveal = "Het was de chair." if language == "nl" else "It was the chair."
    assert any(reveal in answer for answer in answers)
    assert "ispy_target" not in game.state  # round completed, no new target invented


def test_duplicate_hints_with_wrong_guesses_cap_at_three(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch, language="en")
    game.state["ispy_target"] = dict(TARGET, hints_en=["Look for something blue.", "look for something BLUE"])
    first = game.say("a ball")["text"]
    second = game.say("a cup")["text"]
    third = game.say("a book")["text"]
    assert first != second
    assert second.startswith("Not quite.")  # no duplicate hint once the deduplicated ladder is empty
    assert third.startswith("Good try! It was the chair.")
    assert game.judged == ["a ball", "a cup", "a book"]


def test_duplicate_hints_give_up_reveals(monkeypatch: pytest.MonkeyPatch) -> None:
    game = IspyBridge(monkeypatch, language="nl")
    game.state["ispy_target"] = dict(TARGET, hints_nl=["Zoek iets dat blauw is.", "Zoek iets dat blauw is."])
    assert game.say("hint")["text"].startswith("Hier is een hint: Zoek iets dat blauw is.")
    assert game.say("geen idee")["text"].startswith("Dat geeft niet! Het was de chair.")
    assert game.judged == []


# --- Finding 4: untrusted worker endpoints are refused before any connection --------------------


@pytest.fixture
def short_dir():
    # AF_UNIX paths are length-limited; mkdtemp creates a private 0700 directory.
    path = Path(tempfile.mkdtemp(prefix="hbs", dir="/tmp"))
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def impostor(path: Path) -> tuple[socket.socket, list[object]]:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(0.3)
    received: list[object] = []

    def accept() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received.append(connection.recv(4096))
            connection.sendall(b'{"text":"impostor-controlled transcript"}\n')

    threading.Thread(target=accept, daemon=True).start()
    return server, received


def forbid_spawn_and_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "_spawn_worker", lambda *_args: pytest.fail("must not spawn"))
    monkeypatch.setattr(stt, "load_model", lambda: pytest.fail("must not decode"))


@pytest.mark.parametrize("case", ["world_writable_dir", "socket_mode", "symlinked_socket", "symlinked_dir"])
def test_untrusted_worker_endpoint_is_refused_before_connecting(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    forbid_spawn_and_decode(monkeypatch)
    if case == "world_writable_dir":
        directory = short_dir / "pub"
        directory.mkdir()
        directory.chmod(0o777)
        target = directory / "worker.sock"
        server, received = impostor(target)
    elif case == "socket_mode":
        directory = short_dir / "priv"
        directory.mkdir(mode=0o700)
        target = directory / "worker.sock"
        server, received = impostor(target)
        target.chmod(0o666)
    elif case == "symlinked_socket":
        directory = short_dir / "priv"
        directory.mkdir(mode=0o700)
        server, received = impostor(short_dir / "real.sock")
        target = directory / "worker.sock"
        target.symlink_to(short_dir / "real.sock")
    else:
        real = short_dir / "real"
        real.mkdir(mode=0o700)
        server, received = impostor(real / "worker.sock")
        (short_dir / "link").symlink_to(real)
        target = short_dir / "link" / "worker.sock"
    try:
        with pytest.raises(stt.UntrustedEndpointError):
            stt.transcribe_via_worker(target, "/synthetic-kids-audio.wav")
        with pytest.raises(stt.UntrustedEndpointError):
            stt.request(target, "/synthetic-kids-audio.wav")
        time.sleep(0.35)
        assert received == []  # the input path was never disclosed
    finally:
        server.close()


def test_decode_overrun_ends_serving_even_while_decoder_is_held(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-nl-x.wav"
    audio.write_bytes(b"synthetic")
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(stt, "_REPLY_TIMEOUT_SECONDS", 0.05)

    def held(_model: object, _path: str) -> str:
        entered.set()
        release.wait(3)
        return "late"

    monkeypatch.setattr(stt, "transcribe", held)
    server = threading.Thread(target=lambda: stt.serve(sock, idle_seconds=5, model_factory=object))
    server.start()
    try:
        assert wait_for(sock.exists)
        with pytest.raises(RuntimeError):
            stt.request(sock, audio, timeout=2)  # worker answers with a timeout error
        assert entered.is_set()
        server.join(1)
        assert not server.is_alive()
        assert not sock.exists()
    finally:
        release.set()
        server.join(2)


# --- Finding 5: real worker processes are hard bounded -----------------------------------------

HELPER = r"""
import ctypes, sys, time
sys.path.insert(0, sys.argv[1])
from companion import local_stt_command as stt
mode, sock, idle = sys.argv[2], sys.argv[3], sys.argv[4]
stt._LOAD_TIMEOUT_SECONDS = float(sys.argv[5])
stt._REPLY_TIMEOUT_SECONDS = 0.5
stt._WATCHDOG_GRACE_SECONDS = 0.5
gil_sleep = ctypes.PyDLL(None).sleep  # blocks in C while HOLDING the GIL, like a stuck extension
stt.load_model = (lambda: gil_sleep(60)) if mode == "hang_load" else (lambda: object())
if mode == "hang_decode":
    stt.transcribe = lambda model, path: gil_sleep(60)
elif mode == "slow_decode":
    stt.transcribe = lambda model, path: (time.sleep(0.3), "synthetic")[1]
else:
    stt.transcribe = lambda model, path: "synthetic"
sys.argv = ["local_stt_command", "--serve", "--socket", sock, "--idle-seconds", idle]
stt.main()
"""


def helper_worker(mode: str, sock: Path, *, idle: str = "1", load_timeout: str = "0.5") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", HELPER, str(ROOT), mode, str(sock), idle, load_timeout],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def finish(process: subprocess.Popen, timeout: float = 6.0) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        pytest.fail("worker process outlived its hard bound")


def test_worker_process_hung_load_holding_gil_is_killed(short_dir: Path) -> None:
    sock = short_dir / "w" / "worker.sock"
    started = time.monotonic()
    process = helper_worker("hang_load", sock)
    assert finish(process) == -signal.SIGALRM
    assert time.monotonic() - started < 5
    assert stt.worker_running(sock) == 0  # lock released with the process
    assert not sock.exists()


def test_worker_process_hung_decode_holding_gil_is_killed(short_dir: Path) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    process = helper_worker("hang_decode", sock, idle="30")
    try:
        assert wait_for(sock.exists, 5)
        with pytest.raises((RuntimeError, OSError, ValueError)):
            stt.request(sock, audio, timeout=10)
    finally:
        code = finish(process)
    assert code == -signal.SIGALRM
    assert stt.worker_running(sock) == 0


def test_worker_process_survives_client_disconnect_then_exits_when_idle(short_dir: Path) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    process = helper_worker("slow_decode", sock, idle="1")
    try:
        assert wait_for(sock.exists, 5)
        abandoned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abandoned.connect(str(sock))
        abandoned.sendall((json.dumps({"input": str(audio)}) + "\n").encode())
        abandoned.close()  # client disconnects mid-decode
        assert stt.request(sock, audio, timeout=5) == "synthetic"
    finally:
        code = finish(process)
    assert code == 0  # idle exit
    assert not sock.exists()
    assert stt.worker_running(sock) == 0


def test_parallel_clients_share_one_worker_and_reap_the_loser(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    spawned: list[subprocess.Popen] = []
    monkeypatch.setattr(
        stt, "_spawn_worker", lambda path, _idle: spawned.append(helper_worker("ok", path)) or spawned[-1]
    )
    monkeypatch.setattr(stt, "load_model", lambda: pytest.fail("must not decode in-process"))
    results: list[str] = []
    clients = [
        threading.Thread(target=lambda: results.append(stt.transcribe_via_worker(sock, audio, spawn_wait=10)))
        for _ in range(2)
    ]
    try:
        for client in clients:
            client.start()
        for client in clients:
            client.join(15)
        assert results == ["synthetic", "synthetic"]
        assert len(spawned) == 2
        # The loser cannot take the lock and exits without loading; only one worker stays warm.
        assert wait_for(lambda: sum(process.poll() is None for process in spawned) <= 1, 5)
        assert stt.worker_running(sock) in {process.pid for process in spawned}
    finally:
        for process in spawned:
            assert finish(process) in {0, -signal.SIGTERM}
    assert stt.worker_running(sock) == 0


def test_startup_timeout_kills_and_reaps_owned_worker_without_fallback_load(
    short_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    spawned: list[subprocess.Popen] = []

    def spawn(path: Path, _idle: float) -> subprocess.Popen:
        spawned.append(helper_worker("hang_load", path, load_timeout="30"))
        return spawned[-1]

    monkeypatch.setattr(stt, "_spawn_worker", spawn)
    monkeypatch.setattr(stt, "load_model", lambda: pytest.fail("must not decode in-process"))
    with pytest.raises(TimeoutError):
        stt.transcribe_via_worker(sock, audio, spawn_wait=0.5)
    assert len(spawned) == 1
    assert spawned[0].returncode is not None  # killed and reaped
    assert stt.worker_running(sock) == 0


def test_live_loading_worker_blocks_a_second_fallback_load(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock = short_dir / "w" / "worker.sock"
    audio = short_dir / "homebody-stt-en-x.wav"
    audio.write_bytes(b"synthetic")
    holder = helper_worker("hang_load", sock, load_timeout="30")
    spawned: list[subprocess.Popen] = []
    try:
        assert wait_for(lambda: stt.worker_running(sock) > 0, 5)
        monkeypatch.setattr(
            stt,
            "_spawn_worker",
            lambda path, _idle: spawned.append(helper_worker("ok", path)) or spawned[-1],
        )
        monkeypatch.setattr(stt, "load_model", lambda: pytest.fail("must not decode in-process"))
        with pytest.raises(TimeoutError):
            stt.transcribe_via_worker(sock, audio, spawn_wait=1.0)
        assert spawned[0].returncode is not None  # lost the lock race and was reaped
    finally:
        holder.send_signal(signal.SIGTERM)
        finish(holder)
    assert stt.worker_running(sock) == 0


def test_command_alarm_is_cancelled_when_main_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt, "load_model", lambda: object())
    monkeypatch.setattr(stt, "transcribe", lambda _model, _path: "synthetic")
    monkeypatch.setattr(
        sys, "argv", ["x", "--input", str(tmp_path / "a.wav"), "--output-dir", str(tmp_path), "--no-worker"]
    )
    previous = signal.getsignal(signal.SIGALRM)
    stt.main()
    assert signal.alarm(0) == 0  # no pending alarm left in the calling process
    assert signal.getsignal(signal.SIGALRM) == previous
    assert (tmp_path / "transcript.txt").read_text() == "synthetic"
