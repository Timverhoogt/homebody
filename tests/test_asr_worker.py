"""Owned on-robot ASR worker: real subprocess protocol, deadlines, trust and cleanup.

Fake backends are explicit (``asr_fake_worker.py``); passing these tests is not device, GPU or
latency proof. No microphone, network, model download or native library is used.
"""

from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from homebody import asr_worker
from homebody.robot_stt import RobotReceipt, RobotSttCancelled, RobotSttClient, RobotSttUnavailable

FAKE = Path(__file__).with_name("asr_fake_worker.py")


def write_manifest(directory: Path, **overrides) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    library = directory / "libnemo_speech_asr.so"
    model = directory / "parakeet.gguf"
    library.write_bytes(b"synthetic library bytes")
    model.write_bytes(b"synthetic model bytes")
    for path in (library, model):
        path.chmod(0o644)
    payload = {
        "schema": asr_worker.MANIFEST_SCHEMA,
        "backend": asr_worker.NEMO_BACKEND,
        "runtime_revision": asr_worker.PINNED_NEMO_REVISION,
        "library": {"path": str(library), "sha256": hashlib.sha256(library.read_bytes()).hexdigest()},
        "model": {"path": str(model), "sha256": hashlib.sha256(model.read_bytes()).hexdigest(), "name": "fake"},
        "gpu": 0,
        **overrides,
    }
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o644)
    return manifest


@pytest.fixture
def short_dir():
    # AF_UNIX paths are limited to ~108 bytes; pytest tmp paths can exceed that.
    path = Path(tempfile.mkdtemp(prefix="hbasr"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_client(short_dir: Path, mode: str, *, spawns: list | None = None, idle: float = 30.0, **kwargs):
    manifest = write_manifest(short_dir / "prov")
    sock = short_dir / "run" / "worker.sock"

    def argv(socket_path, manifest_path):
        if spawns is not None:
            spawns.append(mode)
        return [sys.executable, str(FAKE), mode, str(socket_path), str(manifest_path), str(idle)]

    kwargs.setdefault("load_timeout", 10.0)
    kwargs.setdefault("decode_timeout", 10.0)
    return RobotSttClient(socket_path=sock, manifest_path=manifest, worker_argv=argv, **kwargs)


def speech(seconds: float = 0.5) -> np.ndarray:
    return (np.sin(np.linspace(0, 400 * np.pi, int(16000 * seconds))) * 0.2).astype(np.float32)


def never() -> bool:
    return False


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            zombie = Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            # Reaped between kill(0) and proc read. Confirm via kill(0) next iteration,
            # never treat a zombie or merely missing proc entry as successful reaping.
            continue
        if zombie:
            time.sleep(0.05)  # zombie until reaped by the client's reaper thread
            continue
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_wait_dead_confirms_reaping_after_proc_read_race(monkeypatch, error):
    calls = []
    def kill(pid, signal):
        calls.append((pid, signal))
        if len(calls) == 2:
            raise ProcessLookupError
    def disappeared(path, *args, **kwargs):
        raise error
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(Path, "read_text", disappeared)
    assert wait_dead(123)
    assert calls == [(123, 0), (123, 0)]


def test_wait_dead_does_not_accept_an_unreaped_zombie(monkeypatch):
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(Path, "read_text", lambda path: "123 (fake) Z")
    assert not wait_dead(123, timeout=1.0)


def test_round_trip_echoes_turn_and_is_immutable(short_dir):
    client = make_client(short_dir, "ok")
    try:
        receipt = client.transcribe(speech(), session="kids-abc", generation=4, language="nl", cancelled=never)
        assert receipt.text == "heard 8000 samples lang=nl"
        assert receipt.accelerator == "cpu"  # fake: never reported as robot GPU
        with pytest.raises(dataclasses.FrozenInstanceError):
            receipt.text = "changed"  # type: ignore[misc]
        adult = client.transcribe(speech(), session="", generation=4, language="auto", cancelled=never)
        assert adult.text.endswith("lang=auto")
        assert isinstance(adult, RobotReceipt)
    finally:
        client.close()


def test_silence_is_empty_text_not_failure(short_dir):
    client = make_client(short_dir, "ok")
    try:
        receipt = client.transcribe(np.zeros(16000, dtype=np.float32), session="", generation=0,
                                    language="en", cancelled=never)
        assert receipt.text == ""
    finally:
        client.close()


def test_worker_environment_is_scrubbed_and_status_has_metrics(short_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    client = make_client(short_dir, "ok")
    try:
        status = client.status()
        assert status["status"] == "ok" and status["load_ms"] >= 0 and status["rss_peak_kb"] > 0
        env_keys = set(status["identity"]["env_keys"])
        assert not {"OPENAI_API_KEY", "HERMES_API_KEY", "HOME"} & env_keys
    finally:
        client.close()


def test_hung_native_decode_is_killed_at_deadline_and_reaped(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "hang_decode", spawns=spawns, decode_timeout=0.6)
    started = time.monotonic()
    with pytest.raises(RobotSttUnavailable) as failure:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert failure.value.category == "decode_timeout"
    assert time.monotonic() - started < 5
    assert client.worker_pid is None
    proc_pid = client._reaping[0].pid if client._reaping else None
    if proc_pid:
        assert wait_dead(proc_pid)
    client.close()


def test_next_turn_respawns_cleanly_after_a_killed_hang(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "ok", spawns=spawns, decode_timeout=0.8)
    try:
        assert client.transcribe(speech(), session="", generation=1, language="en", cancelled=never).text
        first = client._process
        with pytest.raises(RobotSttUnavailable):
            client.transcribe(np.full(4321, 0.1, dtype=np.float32), session="", generation=2, language="en",
                              cancelled=never)
        assert client.worker_pid is None
        deadline = time.monotonic() + 5
        while first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = client.transcribe(speech(), session="", generation=3, language="en", cancelled=never)
        assert receipt.text and client.worker_pid not in {None, first.pid}
        assert spawns == ["ok", "ok"]  # exactly one reload after the kill, never concurrent
    finally:
        client.close()


def test_cancel_during_decode_returns_promptly_and_kills_worker(short_dir):
    client = make_client(short_dir, "hang_decode")
    flag = threading.Event()
    started = time.monotonic()
    threading.Timer(0.8, flag.set).start()
    with pytest.raises(RobotSttCancelled):
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=flag.is_set)
    assert time.monotonic() - started < 3
    assert client.worker_pid is None
    client.close()


def test_hung_load_times_out_and_backs_off_without_duplicate_loads(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "hang_load", spawns=spawns, load_timeout=0.6)
    with pytest.raises(RobotSttUnavailable) as first:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert first.value.category == "load_timeout"
    with pytest.raises(RobotSttUnavailable) as second:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert second.value.category in {"load_failed", "busy"}
    assert spawns == ["hang_load"]  # no reload storm while the first load is failing
    client.close()


def test_failed_load_and_crash_are_infrastructure_failures(short_dir):
    client = make_client(short_dir, "fail_load")
    with pytest.raises(RobotSttUnavailable) as failure:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert failure.value.category == "load_failed"
    client.close()
    crash = make_client(short_dir / "c", "crash_decode")
    with pytest.raises(RobotSttUnavailable) as crashed:
        crash.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert crashed.value.category == "connection_lost"
    crash.close()


def test_stale_reply_is_rejected_and_worker_killed(short_dir):
    client = make_client(short_dir, "stale")
    with pytest.raises(RobotSttUnavailable) as failure:
        client.transcribe(speech(), session="s", generation=2, language="en", cancelled=never)
    assert failure.value.category == "stale_reply"
    assert client.worker_pid is None
    client.close()


def test_only_one_turn_admitted_at_a_time(short_dir):
    client = make_client(short_dir, "hang_decode", decode_timeout=1.5)
    errors: list[str] = []

    def first():
        try:
            client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
        except RobotSttUnavailable as exc:
            errors.append(exc.category)

    thread = threading.Thread(target=first)
    thread.start()
    time.sleep(0.3)
    with pytest.raises(RobotSttUnavailable) as busy:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert busy.value.category == "busy"
    thread.join(5)
    assert errors == ["decode_timeout"]
    client.close()


def test_unprovisioned_fails_closed_without_spawning(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "ok", spawns=spawns)
    client._manifest_path.unlink()
    assert client.provisioned() is False
    with pytest.raises(RobotSttUnavailable) as failure:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert failure.value.category == "unprovisioned" and spawns == []


def test_availability_check_never_spawns_or_creates_endpoint(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "ok", spawns=spawns)
    assert client.provisioned() is True  # what routine status / voice-options uses
    assert spawns == [] and client.worker_pid is None and not (short_dir / "run").exists()
    client.close()


def test_untrusted_directory_is_refused_before_spawn(short_dir):
    spawns: list[str] = []
    client = make_client(short_dir, "ok", spawns=spawns)
    run = short_dir / "run"
    run.mkdir(mode=0o755)
    run.chmod(0o755)
    with pytest.raises(RobotSttUnavailable) as failure:
        client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert failure.value.category == "untrusted_endpoint" and spawns == []


def test_symlinked_socket_is_refused(short_dir):
    run = short_dir / "run"
    run.mkdir(mode=0o700)
    (run / "worker.sock").symlink_to(short_dir / "elsewhere.sock")
    with pytest.raises(asr_worker.UntrustedEndpointError):
        asr_worker.require_trusted_endpoint(run / "worker.sock")


def test_forged_endpoint_with_wrong_peer_receives_no_audio(short_dir):
    client = make_client(short_dir, "ok")
    run = short_dir / "run"
    run.mkdir(mode=0o700)
    forged = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    forged.bind(str(run / "worker.sock"))
    os.chmod(run / "worker.sock", 0o600)
    forged.listen(1)
    received = bytearray()

    def accept():
        conn, _ = forged.accept()
        conn.settimeout(1.0)
        try:
            while chunk := conn.recv(4096):
                received.extend(chunk)
        except OSError:
            pass
        conn.close()

    thread = threading.Thread(target=accept)
    thread.start()
    # An "owned" process that is not the socket's peer (the test process is).
    impostor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    client._process = impostor
    try:
        with pytest.raises(RobotSttUnavailable) as failure:
            client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
        assert failure.value.category == "untrusted_endpoint"
        thread.join(3)
        assert bytes(received) == b""  # nothing disclosed before the peer check
        assert wait_dead(impostor.pid)
    finally:
        forged.close()
        if impostor.poll() is None:
            impostor.kill()
        impostor.wait(5)


def raw_request(sock_path: Path, payload: bytes) -> dict:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    conn.connect(str(sock_path))
    conn.sendall(payload)
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    conn.close()
    return json.loads(data)


def test_malformed_and_oversized_requests_get_categorical_errors(short_dir):
    client = make_client(short_dir, "ok")
    try:
        client.status()  # spawn + publish
        sock = short_dir / "run" / "worker.sock"
        assert raw_request(sock, b"not json\n")["error"] == "malformed"
        assert raw_request(sock, b"x" * (asr_worker.HEADER_LIMIT + 10))["error"] == "header_too_large"
        header = {"v": 1, "op": "recognize", "session": "s", "turn": 1, "generation": 1,
                  "language": "en", "rate": 16000, "samples": asr_worker.MAX_SAMPLES + 1}
        assert raw_request(sock, json.dumps(header).encode() + b"\n")["error"] == "too_large"
        extra = {**header, "samples": 4, "path": "/etc/passwd"}
        assert raw_request(sock, json.dumps(extra).encode() + b"\n")["error"] == "malformed"
        wrong_rate = {**header, "samples": 4, "rate": 44100}
        assert raw_request(sock, json.dumps(wrong_rate).encode() + b"\n")["error"] == "malformed"
        bad_language = {**header, "samples": 4, "language": "sv"}
        assert raw_request(sock, json.dumps(bad_language).encode() + b"\n")["error"] == "malformed"
        # The worker survives malformed input and still serves the owner.
        assert client.transcribe(speech(), session="", generation=1, language="en", cancelled=never).text
        with pytest.raises(RobotSttUnavailable):
            client.transcribe(np.zeros(asr_worker.MAX_SAMPLES + 1, dtype=np.float32), session="",
                              generation=1, language="en", cancelled=never)
    finally:
        client.close()


def test_idle_worker_exits_and_close_kills_owned_process(short_dir):
    client = make_client(short_dir, "ok", idle=1.0)
    client.transcribe(speech(), session="", generation=1, language="en", cancelled=never)
    assert client.worker_pid
    deadline = time.monotonic() + 20.0  # generous under a loaded CI host; normally ~1 s
    while client._process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.worker_pid is None  # exited on its own idle deadline
    assert not (short_dir / "run" / "worker.sock").exists()
    busy = make_client(short_dir / "b", "ok")
    busy.status()
    pid = busy.worker_pid
    busy.close()
    assert wait_dead(pid, timeout=20.0)
    with pytest.raises(RobotSttUnavailable):
        busy.transcribe(speech(), session="", generation=1, language="en", cancelled=never)


def test_worker_python_guard_refuses_network_sockets():
    code = (
        "import socket,sys; sys.path.insert(0, sys.argv[1]);"
        "from homebody import asr_worker; asr_worker._deny_network();"
        "socket.socket(socket.AF_UNIX).close();"
        "\ntry:\n socket.socket(socket.AF_INET)\nexcept PermissionError:\n print('denied')"
    )
    out = subprocess.run([sys.executable, "-c", code, str(FAKE.parents[1])], capture_output=True, text=True,
                         timeout=10)
    assert out.stdout.strip() == "denied"


# ---- provisioning manifest ----


def test_manifest_requires_pinned_hashes_and_private_artifacts(short_dir):
    manifest = write_manifest(short_dir / "m")
    assert asr_worker.verify_manifest(manifest)["gpu"] == 0
    tampered = json.loads(manifest.read_text())
    Path(tampered["model"]["path"]).write_bytes(b"different weights")
    with pytest.raises(asr_worker.ProvisioningError, match="pinned hash"):
        asr_worker.verify_manifest(manifest)
    for overrides in (
        {"runtime_revision": "main"},
        {"backend": "whisper"},
        {"gpu": "0"},
        {"extra": 1},
    ):
        bad = write_manifest(short_dir / f"bad{len(str(overrides))}", **overrides)
        with pytest.raises(asr_worker.ProvisioningError):
            asr_worker.read_manifest(bad)
    writable = write_manifest(short_dir / "w")
    writable.chmod(0o666)
    with pytest.raises(asr_worker.ProvisioningError):
        asr_worker.read_manifest(writable)
    linked = short_dir / "link.json"
    linked.symlink_to(manifest)
    with pytest.raises(asr_worker.ProvisioningError):
        asr_worker.read_manifest(linked)


# ---- NeMo-Speech.cpp C ABI shape (include/nemo_speech/asr.h @ pinned revision) ----


def test_nemo_struct_layouts_match_header_declaration_order():
    names = lambda struct: [name for name, _ in struct._fields_]  # noqa: E731
    assert names(asr_worker.NemoBackendConfig) == ["size", "gpu"]
    assert names(asr_worker.NemoModelConfig) == ["size", "path", "name"]
    assert names(asr_worker.NemoRecognizerConfig) == [
        "size", "backend", "model", "streaming", "decoder", "vad", "endpointing", "postproc", "diar", "batching",
    ]
    assert names(asr_worker.NemoRecognitionOptions) == [
        "size", "request_id", "language_code", "interim_results", "enable_word_time_offsets",
        "enable_automatic_punctuation", "verbatim_transcripts", "profanity_filter", "stop_history_eou_ms",
        "speech_contexts", "speech_context_count", "max_alternatives", "enable_speaker_diarization",
        "max_speaker_count",
    ]
    assert asr_worker.NemoBackendConfig.gpu.offset == ctypes.sizeof(ctypes.c_size_t)
    assert ctypes.sizeof(asr_worker.NemoRecognizerConfig) == 10 * ctypes.sizeof(ctypes.c_void_p)


class FakeNemoLibrary:
    """Pure-Python stand-in for the C symbols; checks how the adapter calls the ABI."""

    def __init__(self, status=0, text=b"hallo wereld"):
        self.status = status
        self.text = text
        self.calls: list[tuple] = []
        self.destroyed: list[int] = []

        def options_default():
            return asr_worker.NemoRecognitionOptions(size=ctypes.sizeof(asr_worker.NemoRecognitionOptions))

        def create(cfg, out):
            config = cfg._obj
            self.calls.append(("create", config.size, config.backend.contents.gpu,
                               config.backend.contents.size, config.model.contents.path, bool(config.streaming)))
            out._obj.value = 0x1000
            return 0

        def recognize(handle, options, samples, count, rate, out):
            self.calls.append(("recognize", handle.value if hasattr(handle, "value") else handle,
                               options._obj.language_code, count, rate, samples[0]))
            out._obj.value = 0x2000
            return self.status

        self.nemo_speech_asr_recognition_options_default = options_default
        self.nemo_speech_asr_create = create
        self.nemo_speech_asr_destroy = lambda handle: None
        self.nemo_speech_asr_recognize_f32 = recognize
        self.nemo_speech_asr_result_alternative_count = lambda result: 1
        self.nemo_speech_asr_result_transcript = lambda result, alt: self.text
        self.nemo_speech_asr_result_destroy = lambda result: self.destroyed.append(result.value)
        self.nemo_speech_asr_version = lambda: b"0.2.0"


def test_nemo_adapter_calls_c_abi_with_gpu_config_and_frees_results(short_dir):
    manifest = asr_worker.read_manifest(write_manifest(short_dir / "n"))
    library = FakeNemoLibrary()
    backend = asr_worker.NemoSpeechBackend(manifest, cdll=lambda _path: library)
    assert backend.accelerator == "gpu"
    create = library.calls[0]
    assert create[1] == ctypes.sizeof(asr_worker.NemoRecognizerConfig)
    assert create[2] == 0 and create[3] == ctypes.sizeof(asr_worker.NemoBackendConfig)
    assert create[5] is False  # streaming config NULL: library defaults (Stage A is offline)
    pcm = bytearray(np.array([0.25, -0.5], dtype="<f4").tobytes())
    assert backend.recognize(pcm, "nl") == "hallo wereld"
    _, _, language, count, rate, first = library.calls[1]
    assert (language, count, rate, first) == (b"nl", 2, 16000, 0.25)
    assert library.destroyed == [0x2000]
    backend.recognize(pcm, None)
    assert library.calls[2][2] is None  # NULL language_code = auto
    assert backend.identity["runtime_revision"] == asr_worker.PINNED_NEMO_REVISION
    cpu = asr_worker.NemoSpeechBackend({**manifest, "gpu": -1}, cdll=lambda _path: FakeNemoLibrary())
    assert cpu.accelerator == "cpu"  # CPU inference is never labelled robot GPU
    failing = asr_worker.NemoSpeechBackend(manifest, cdll=lambda _path: FakeNemoLibrary(status=3))
    with pytest.raises(RuntimeError, match="status 3"):
        failing.recognize(pcm, "en")
