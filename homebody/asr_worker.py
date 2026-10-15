"""Resident on-robot speech-recognition worker (Stage A: whole utterance at the endpoint).

This file is deliberately standard-library only so it can run as ``python -I asr_worker.py``
in a scrubbed environment: no Homebody imports, no credentials, no network client, no tools and
no media device. Homebody stays the only microphone reader and sends the 16 kHz float32 samples
it already holds in RAM over a private Unix socket. Audio and transcripts are never written to
disk or logged.

Protocol v1, one request per connection:

* request: one JSON header line (at most ``HEADER_LIMIT`` bytes, exact key set) followed, for
  ``recognize``, by exactly ``samples * 4`` bytes of little-endian float32 mono PCM at 16 kHz::

    {"v": 1, "op": "recognize", "session": "<id>", "turn": 7, "generation": 3,
     "language": "en" | "nl" | "es" | "de" | "fr" | "it" | "pt" | "auto", "rate": 16000, "samples": 48000}
    {"v": 1, "op": "status"}

* reply: one JSON line. ``recognize`` echoes ``session``/``turn``/``generation`` exactly and
  carries ``status`` ``ok`` (``text``, ``processing`` = ``robot``, ``accelerator`` = ``gpu`` or
  ``cpu``, ``decode_ms``) or ``error`` with a categorical ``error`` only (never content).

Every phase is bounded. In process mode a default-disposition SIGALRM (delivered by the kernel,
so it also ends a load or decode stuck in native code) caps load, decode and idle time; the
owning client additionally kills the process on its own deadline or on Stop.

Backends are a minimal contract: ``factory(manifest) -> backend`` where ``backend`` has
``recognize(samples: bytearray, language: str | None) -> str``, ``accelerator`` and
``identity``. Production supports only the NVIDIA NeMo-Speech.cpp stable C ABI
(``include/nemo_speech/asr.h`` at the pinned revision), loaded through ``ctypes`` from an
owner-provisioned, sha256-pinned library and model named by a read-only manifest.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import resource
import signal
import socket
import stat
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = 1
SAMPLE_RATE = 16000
HEADER_LIMIT = 1024
MAX_SECONDS = 120.0  # AppConfig.max_utterance_seconds upper bound
MAX_SAMPLES = int(MAX_SECONDS * SAMPLE_RATE)
MANIFEST_LIMIT = 16 * 1024
MANIFEST_SCHEMA = "homebody-robot-stt/1"
NEMO_BACKEND = "nemo_speech_c_abi"
PINNED_NEMO_REVISION = "083861c241061562b8ec29eb06d7f6cdc3af8775"
LOAD_TIMEOUT_SECONDS = 90.0
DECODE_TIMEOUT_SECONDS = 20.0
IDLE_SECONDS = 600.0
IO_TIMEOUT_SECONDS = 5.0
_WATCHDOG_GRACE_SECONDS = 2.0
_SESSION = re.compile(r"^[A-Za-z0-9_-]{0,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECOGNIZE_KEYS = frozenset({"v", "op", "session", "turn", "generation", "language", "rate", "samples"})
_LANGUAGES = {"en": "en", "nl": "nl", "es": "es", "de": "de", "fr": "fr", "it": "it", "pt": "pt", "auto": None}


class UntrustedEndpointError(RuntimeError):
    """The worker directory, socket or a provisioned artifact could be controlled by someone else."""


class ProvisioningError(RuntimeError):
    """The pinned backend artifacts are missing, untrusted or do not match their hashes."""


class ProtocolError(ValueError):
    """A malformed, oversized or out-of-schema request."""


# ---- trust checks (shared by worker and in-app client) ----


def check_private_dir(path) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UntrustedEndpointError("Robot STT worker directory is not private")


def private_dir(path) -> None:
    """Create/verify a directory only this user can enter; refuse symlinks or foreign owners."""
    try:
        Path(path).mkdir(mode=0o700)
    except FileExistsError:
        pass
    check_private_dir(path)


def require_trusted_endpoint(socket_path) -> bool:
    """Validate directory and socket before any connection or disclosure; False if absent."""
    socket_path = Path(socket_path)
    try:
        check_private_dir(socket_path.parent)
    except FileNotFoundError:
        return False
    try:
        info = os.lstat(socket_path)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UntrustedEndpointError("Robot STT worker socket is not private")
    return True


def _trusted_file(path: str, what: str) -> os.stat_result:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ProvisioningError(f"{what} path must be absolute")
    try:
        info = os.lstat(path)
    except OSError:
        raise ProvisioningError(f"{what} is not provisioned") from None
    if not stat.S_ISREG(info.st_mode):
        raise ProvisioningError(f"{what} is not a regular file")
    if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
        raise ProvisioningError(f"{what} is writable by another user")
    return info


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path) -> dict:
    """Parse the owner-provisioned read-only manifest; no hashing (cheap availability check)."""
    path = str(path)
    info = _trusted_file(path, "Robot STT manifest")
    if info.st_size > MANIFEST_LIMIT:
        raise ProvisioningError("Robot STT manifest is too large")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        raise ProvisioningError("Robot STT manifest is not valid JSON") from None
    expected = {"schema", "backend", "runtime_revision", "library", "model", "gpu"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ProvisioningError("Robot STT manifest has an unexpected shape")
    if payload["schema"] != MANIFEST_SCHEMA or payload["backend"] != NEMO_BACKEND:
        raise ProvisioningError("Unsupported robot STT backend")
    if payload["runtime_revision"] != PINNED_NEMO_REVISION:
        raise ProvisioningError("Robot STT runtime is not the pinned revision")
    gpu = payload["gpu"]
    if type(gpu) is not int or not -1 <= gpu <= 15:
        raise ProvisioningError("Robot STT GPU index is invalid")
    for key, fields in (("library", {"path", "sha256"}), ("model", {"path", "sha256", "name"})):
        entry = payload[key]
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ProvisioningError(f"Robot STT {key} entry has an unexpected shape")
        if not isinstance(entry["sha256"], str) or not _SHA256.match(entry["sha256"]):
            raise ProvisioningError(f"Robot STT {key} hash is not a sha256 digest")
        _trusted_file(entry["path"], f"Robot STT {key}")
    if not isinstance(payload["model"]["name"], str) or len(payload["model"]["name"]) > 128:
        raise ProvisioningError("Robot STT model name is invalid")
    return payload


def verify_manifest(path) -> dict:
    """Full verification before any native load: shape, ownership/mode and exact sha256."""
    payload = read_manifest(path)
    for key in ("library", "model"):
        if _sha256(payload[key]["path"]) != payload[key]["sha256"]:
            raise ProvisioningError(f"Robot STT {key} does not match its pinned hash")
    return payload


# ---- NeMo-Speech.cpp stable C ABI (include/nemo_speech/asr.h @ PINNED_NEMO_REVISION) ----

NEMO_SPEECH_ASR_OK = 0


class NemoBackendConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("gpu", ctypes.c_int32)]


class NemoModelConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("path", ctypes.c_char_p), ("name", ctypes.c_char_p)]


class NemoRecognizerConfig(ctypes.Structure):
    # Subsystem configs are optional pointers; NULL = library defaults.
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("backend", ctypes.POINTER(NemoBackendConfig)),
        ("model", ctypes.POINTER(NemoModelConfig)),
        ("streaming", ctypes.c_void_p),
        ("decoder", ctypes.c_void_p),
        ("vad", ctypes.c_void_p),
        ("endpointing", ctypes.c_void_p),
        ("postproc", ctypes.c_void_p),
        ("diar", ctypes.c_void_p),
        ("batching", ctypes.c_void_p),
    ]


class NemoRecognitionOptions(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("request_id", ctypes.c_char_p),
        ("language_code", ctypes.c_char_p),
        ("interim_results", ctypes.c_bool),
        ("enable_word_time_offsets", ctypes.c_bool),
        ("enable_automatic_punctuation", ctypes.c_bool),
        ("verbatim_transcripts", ctypes.c_bool),
        ("profanity_filter", ctypes.c_bool),
        ("stop_history_eou_ms", ctypes.c_int32),
        ("speech_contexts", ctypes.c_void_p),
        ("speech_context_count", ctypes.c_size_t),
        ("max_alternatives", ctypes.c_int32),
        ("enable_speaker_diarization", ctypes.c_bool),
        ("max_speaker_count", ctypes.c_int32),
    ]


def bind_nemo_abi(lib) -> None:
    """Declare exactly the C prototypes this adapter calls (no invented symbols)."""
    p = ctypes.c_void_p
    lib.nemo_speech_asr_recognition_options_default.argtypes = []
    lib.nemo_speech_asr_recognition_options_default.restype = NemoRecognitionOptions
    lib.nemo_speech_asr_create.argtypes = [ctypes.POINTER(NemoRecognizerConfig), ctypes.POINTER(p)]
    lib.nemo_speech_asr_create.restype = ctypes.c_int
    lib.nemo_speech_asr_destroy.argtypes = [p]
    lib.nemo_speech_asr_destroy.restype = None
    lib.nemo_speech_asr_recognize_f32.argtypes = [
        p, ctypes.POINTER(NemoRecognitionOptions), ctypes.POINTER(ctypes.c_float),
        ctypes.c_size_t, ctypes.c_int32, ctypes.POINTER(p),
    ]
    lib.nemo_speech_asr_recognize_f32.restype = ctypes.c_int
    lib.nemo_speech_asr_result_alternative_count.argtypes = [p]
    lib.nemo_speech_asr_result_alternative_count.restype = ctypes.c_size_t
    lib.nemo_speech_asr_result_transcript.argtypes = [p, ctypes.c_size_t]
    lib.nemo_speech_asr_result_transcript.restype = ctypes.c_char_p
    lib.nemo_speech_asr_result_destroy.argtypes = [p]
    lib.nemo_speech_asr_result_destroy.restype = None
    lib.nemo_speech_asr_version.argtypes = []
    lib.nemo_speech_asr_version.restype = ctypes.c_char_p


class NemoSpeechBackend:
    """One recognizer handle owned by this process; destroyed only at exit."""

    def __init__(self, manifest: dict, *, cdll=ctypes.CDLL) -> None:
        self._lib = cdll(manifest["library"]["path"])
        bind_nemo_abi(self._lib)
        gpu = int(manifest["gpu"])
        self.accelerator = "cpu" if gpu < 0 else "gpu"
        self._backend_cfg = NemoBackendConfig(ctypes.sizeof(NemoBackendConfig), gpu)
        self._model_path = manifest["model"]["path"].encode()
        self._model_name = manifest["model"]["name"].encode()
        self._model_cfg = NemoModelConfig(ctypes.sizeof(NemoModelConfig), self._model_path, self._model_name)
        config = NemoRecognizerConfig()
        config.size = ctypes.sizeof(NemoRecognizerConfig)
        config.backend = ctypes.pointer(self._backend_cfg)
        config.model = ctypes.pointer(self._model_cfg)
        handle = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_create(ctypes.byref(config), ctypes.byref(handle))
        if status != NEMO_SPEECH_ASR_OK or not handle.value:
            # nemo_speech_asr_last_error() may name paths; only the status code is kept.
            raise RuntimeError(f"nemo_speech_asr_create failed with status {int(status)}")
        self._handle = handle
        version = self._lib.nemo_speech_asr_version() or b""
        self.identity = {
            "backend": NEMO_BACKEND,
            "runtime_revision": manifest["runtime_revision"],
            "runtime_version": version.decode("utf-8", "replace")[:64],
            "library_sha256": manifest["library"]["sha256"],
            "model_sha256": manifest["model"]["sha256"],
            "model_name": manifest["model"]["name"],
            "accelerator": self.accelerator,
        }

    def recognize(self, samples: bytearray, language: str | None) -> str:
        count = len(samples) // 4
        if count == 0:
            return ""
        options = self._lib.nemo_speech_asr_recognition_options_default()
        # Header: language_code is prompt selection, NULL = auto/default. Whether a given model
        # honours a fixed language is a runtime/model property measured on the device.
        options.language_code = language.encode() if language else None
        buffer = (ctypes.c_float * count).from_buffer(samples)
        result = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_recognize_f32(
            self._handle, ctypes.byref(options), buffer, count, SAMPLE_RATE, ctypes.byref(result)
        )
        if status != NEMO_SPEECH_ASR_OK:
            raise RuntimeError(f"nemo_speech_asr_recognize_f32 failed with status {int(status)}")
        try:
            if not result.value or self._lib.nemo_speech_asr_result_alternative_count(result) == 0:
                return ""
            text = self._lib.nemo_speech_asr_result_transcript(result, 0) or b""
            return text.decode("utf-8", "replace").strip()
        finally:
            if result.value:
                self._lib.nemo_speech_asr_result_destroy(result)


def nemo_backend_factory(manifest: dict) -> NemoSpeechBackend:
    return NemoSpeechBackend(manifest)


# ---- request handling ----


def _recv_header(conn) -> bytes:
    data = bytearray()
    while True:
        byte = conn.recv(1)
        if not byte:
            raise ProtocolError("truncated")
        if byte == b"\n":
            return bytes(data)
        data += byte
        if len(data) > HEADER_LIMIT:
            raise ProtocolError("header_too_large")


def parse_header(raw: bytes) -> dict:
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("malformed") from None
    if not isinstance(header, dict) or header.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("malformed")
    if header.get("op") == "status" and set(header) == {"v", "op"}:
        return header
    if header.get("op") != "recognize" or set(header) != _RECOGNIZE_KEYS:
        raise ProtocolError("malformed")
    session, turn, generation = header["session"], header["turn"], header["generation"]
    if not isinstance(session, str) or not _SESSION.match(session):
        raise ProtocolError("malformed")
    for value in (turn, generation):
        if type(value) is not int or not 0 <= value < 2**53:
            raise ProtocolError("malformed")
    if header["language"] not in _LANGUAGES or header["rate"] != SAMPLE_RATE:
        raise ProtocolError("malformed")
    samples = header["samples"]
    if type(samples) is not int or samples < 0:
        raise ProtocolError("malformed")
    if samples > MAX_SAMPLES:
        raise ProtocolError("too_large")
    return header


def _recv_exact(conn, size: int) -> bytearray:
    buffer = bytearray(size)
    view = memoryview(buffer)
    received = 0
    while received < size:
        count = conn.recv_into(view[received:], min(1 << 20, size - received))
        if count == 0:
            raise ProtocolError("truncated")
        received += count
    return buffer


def _send(conn, reply: dict) -> None:
    try:
        conn.sendall((json.dumps(reply, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        pass


def _peak_rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _no_watchdog(_seconds: float) -> None:
    pass


def alarm_watchdog(seconds: float) -> None:
    """Process-mode hard bound: default SIGALRM terminates even inside native code."""
    signal.alarm(max(1, math.ceil(seconds + _WATCHDOG_GRACE_SECONDS)))


def _deny_network() -> None:
    """Python-level guard: this process may only open Unix sockets (native code is not covered)."""
    original = socket.socket

    class _UnixOnly(original):  # type: ignore[misc, valid-type]
        def __init__(self, family=-1, *args, **kwargs):
            if family != socket.AF_UNIX:
                raise PermissionError("Robot STT worker has no network access")
            super().__init__(family, *args, **kwargs)

    socket.socket = _UnixOnly  # type: ignore[misc]


def serve(
    socket_path,
    manifest_path,
    *,
    backend_factory=nemo_backend_factory,
    verify=verify_manifest,
    idle_seconds: float = IDLE_SECONDS,
    decode_timeout: float = DECODE_TIMEOUT_SECONDS,
    load_timeout: float = LOAD_TIMEOUT_SECONDS,
    watchdog=_no_watchdog,
) -> int:
    """Load once, publish the socket atomically, serve sequential requests; return when idle."""
    socket_path = Path(socket_path)
    private_dir(socket_path.parent)
    lock = open(socket_path.with_suffix(".lock"), "a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # Another worker owns this socket; the client's peer check rejects it.
        watchdog(load_timeout)
        load_started = time.monotonic()
        manifest = verify(manifest_path)
        backend = backend_factory(manifest)
        load_ms = int((time.monotonic() - load_started) * 1000)
        accelerator = "gpu" if getattr(backend, "accelerator", "cpu") == "gpu" else "cpu"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        staging = socket_path.with_name(f".worker-{os.getpid()}.sock")
        published_inode = None
        served = 0
        try:
            staging.unlink(missing_ok=True)
            server.bind(str(staging))
            os.chmod(staging, 0o600)
            server.listen(1)
            published_inode = os.lstat(staging).st_ino
            os.replace(staging, socket_path)
            server.settimeout(idle_seconds)
            while True:
                watchdog(idle_seconds)
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    return served
                watchdog(decode_timeout + 2 * IO_TIMEOUT_SECONDS)
                with conn:
                    conn.settimeout(IO_TIMEOUT_SECONDS)
                    served += 1
                    try:
                        header = parse_header(_recv_header(conn))
                    except ProtocolError as exc:
                        _send(conn, {"v": PROTOCOL_VERSION, "status": "error", "error": str(exc)})
                        continue
                    except OSError:
                        continue
                    if header["op"] == "status":
                        _send(conn, {
                            "v": PROTOCOL_VERSION, "status": "ok", "accelerator": accelerator,
                            "load_ms": load_ms, "rss_peak_kb": _peak_rss_kb(),
                            "identity": dict(getattr(backend, "identity", {}) or {}),
                        })
                        continue
                    echo = {
                        "v": PROTOCOL_VERSION, "session": header["session"],
                        "turn": header["turn"], "generation": header["generation"],
                    }
                    try:
                        samples = _recv_exact(conn, header["samples"] * 4)
                    except (ProtocolError, OSError):
                        _send(conn, {**echo, "status": "error", "error": "truncated"})
                        continue
                    started = time.monotonic()
                    try:
                        text = backend.recognize(samples, _LANGUAGES[header["language"]])
                    except Exception:
                        _send(conn, {**echo, "status": "error", "error": "backend"})
                        continue
                    finally:
                        del samples
                    _send(conn, {
                        **echo, "status": "ok", "text": str(text or "")[:4000], "processing": "robot",
                        "accelerator": accelerator, "decode_ms": int((time.monotonic() - started) * 1000),
                    })
        finally:
            server.close()
            staging.unlink(missing_ok=True)
            try:
                if published_inode is not None and os.lstat(socket_path).st_ino == published_inode:
                    socket_path.unlink()
            except OSError:
                pass
    finally:
        lock.close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    parser.add_argument("--decode-timeout", type=float, default=DECODE_TIMEOUT_SECONDS)
    parser.add_argument("--load-timeout", type=float, default=LOAD_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if sys.byteorder != "little":
        os._exit(3)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    _deny_network()
    try:
        serve(
            args.socket, args.manifest,
            idle_seconds=min(max(args.idle_seconds, 1.0), 3600.0),
            decode_timeout=min(max(args.decode_timeout, 1.0), 120.0),
            load_timeout=min(max(args.load_timeout, 1.0), 600.0),
            watchdog=alarm_watchdog,
        )
    except BaseException:
        os._exit(2)  # never print a traceback that could carry paths
    os._exit(0)


if __name__ == "__main__":
    main()
