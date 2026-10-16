"""In-app client for the owned on-robot ASR worker (``asr_worker.py``).

The app owns exactly one worker process: started with a scrubbed environment and ``python -I``,
reached only after the private directory, the socket and the socket peer's pid/uid are verified,
and killed (then reaped off the caller's thread) on any deadline, Stop or protocol violation.
One turn is admitted at a time; a second concurrent request is refused, never queued. A failed
load backs off instead of reloading on every turn. Audio stays in RAM.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import asr_worker
from .asr_worker import (
    MAX_SAMPLES,
    PROTOCOL_VERSION,
    SAMPLE_RATE,
    ProvisioningError,
    UntrustedEndpointError,
    read_manifest,
    require_trusted_endpoint,
)
from .config import default_config_path

_POLL_SECONDS = 0.02
_LOAD_FAILURE_BACKOFF_SECONDS = 60.0
_ENV_ALLOWLIST = ("LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES")


class RobotSttUnavailable(RuntimeError):
    """Infrastructure failure (not silence): unprovisioned, busy, load/decode failure or timeout."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class RobotSttCancelled(RuntimeError):
    """The turn lost ownership while the worker was loading or decoding."""


@dataclass(frozen=True, slots=True)
class RobotReceipt:
    text: str
    accelerator: str  # "gpu" | "cpu" as reported by the worker's backend
    decode_ms: int


def default_manifest_path() -> Path:
    """Fixed owner-provisioned location; never caller-selected."""
    return default_config_path().parent / "robot-stt" / "manifest.json"


def default_socket_path() -> Path:
    # A system service can inherit XDG_RUNTIME_DIR while ProtectHome hides /run/user.
    # Prefer it only when usable in *this* mount namespace; PrivateTmp remains a safe
    # fallback parent. _ensure_worker still validates the private child and socket,
    # and must never fall back around an untrusted existing endpoint.
    base = os.environ.get("XDG_RUNTIME_DIR")
    if (
        not base or not Path(base).is_absolute() or not os.path.isdir(base)
        or not os.access(base, os.W_OK | os.X_OK)
    ):
        base = tempfile.gettempdir()
    return Path(base, f"homebody-robot-stt-{os.getuid()}", "worker.sock")


def _scrubbed_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
    env.update({k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ})
    return env


def _peer_credentials(sock: socket.socket) -> tuple[int, int]:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    return pid, uid


class RobotSttClient:
    def __init__(
        self,
        *,
        socket_path: Path | None = None,
        manifest_path: Path | None = None,
        worker_argv: Callable[[Path, Path], list[str]] | None = None,
        load_timeout: float = asr_worker.LOAD_TIMEOUT_SECONDS,
        decode_timeout: float = asr_worker.DECODE_TIMEOUT_SECONDS,
        idle_seconds: float = asr_worker.IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._socket_path = Path(socket_path or default_socket_path())
        self._manifest_path = Path(manifest_path or default_manifest_path())
        self._worker_argv = worker_argv or self._default_argv
        self._load_timeout = load_timeout
        self._decode_timeout = decode_timeout
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._admission = threading.Lock()  # one turn at a time; never queued
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._reaping: list[subprocess.Popen] = []
        self._failed_until = 0.0
        self._turns = itertools.count(1)
        self._closed = False
        self._epoch = 0
        self._warming = threading.Event()
        self._manifest_signature = None

    def _signature(self):
        manifest = read_manifest(self._manifest_path)
        paths = [self._manifest_path, *(Path(manifest[k]["path"]) for k in ("library", "model"))]
        return (json.dumps(manifest, sort_keys=True), tuple(
            (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for p in paths
        ))

    def cancel(self, *, release: bool = False) -> None:
        """Invalidate pending work immediately; optionally release an idle model for privacy."""
        with self._process_lock:
            self._epoch += 1
        if release or self._admission.locked():
            self._kill_owned()

    def _default_argv(self, socket_path: Path, manifest_path: Path) -> list[str]:
        return [
            sys.executable, "-I", str(Path(asr_worker.__file__).resolve()), "--serve",
            "--socket", str(socket_path), "--manifest", str(manifest_path),
            "--idle-seconds", str(self._idle_seconds),
            "--decode-timeout", str(self._decode_timeout),
            "--load-timeout", str(self._load_timeout),
        ]

    def provisioned(self) -> bool:
        """Cheap shape/ownership check of the pinned manifest; hashes are verified by the worker."""
        try:
            read_manifest(self._manifest_path)
        except ProvisioningError:
            return False
        return True

    @property
    def worker_pid(self) -> int | None:
        with self._process_lock:
            return self._process.pid if self._process is not None and self._process.poll() is None else None

    # ---- process ownership ----

    def _kill_owned(self) -> None:
        """SIGKILL the owned worker and reap it on a daemon thread; never blocks the caller."""
        with self._process_lock:
            process, self._process = self._process, None
            if process is None:
                return
            self._reaping.append(process)
        try:
            process.kill()
        except OSError:
            pass

        def reap() -> None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            with self._process_lock:
                if process.poll() is not None and process in self._reaping:
                    self._reaping.remove(process)

        threading.Thread(target=reap, name="robot-stt-reap", daemon=True).start()

    def _ensure_worker(self, cancelled: Callable[[], bool]) -> subprocess.Popen:
        if cancelled():
            raise RobotSttCancelled("cancelled before load")
        try:
            signature = self._signature()
        except (ProvisioningError, OSError):
            self._kill_owned()
            raise RobotSttUnavailable("unprovisioned") from None
        if self._manifest_signature is not None and signature != self._manifest_signature:
            self._kill_owned()
            raise RobotSttUnavailable("provisioning_changed")
        with self._process_lock:
            if self._closed:
                raise RobotSttUnavailable("closed")
            self._reaping = [p for p in self._reaping if p.poll() is None]
            if self._reaping:
                raise RobotSttUnavailable("busy")  # an old native hang is still being reaped
            process = self._process
            if process is not None and process.poll() is None:
                return process
            self._process = None
            if self._clock() < self._failed_until:
                raise RobotSttUnavailable("load_failed")
        if not self.provisioned():
            raise RobotSttUnavailable("unprovisioned")
        try:
            asr_worker.private_dir(self._socket_path.parent)
            if require_trusted_endpoint(self._socket_path):  # refuse a planted socket before spawning
                # A socket left by a killed worker would look "ready" before ours publishes.
                self._socket_path.unlink(missing_ok=True)
        except (UntrustedEndpointError, OSError):
            raise RobotSttUnavailable("untrusted_endpoint") from None
        try:
            process = subprocess.Popen(
                self._worker_argv(self._socket_path, self._manifest_path),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_scrubbed_env(), close_fds=True, start_new_session=True, cwd="/",
            )
        except OSError:
            raise RobotSttUnavailable("spawn_failed") from None
        with self._process_lock:
            self._process = process
            self._manifest_signature = signature
            closed = self._closed
        if closed or cancelled():
            self._kill_owned()
            raise RobotSttCancelled("cancelled during spawn")
        deadline = self._clock() + self._load_timeout
        while True:
            if cancelled():
                self._kill_owned()
                raise RobotSttCancelled("cancelled during load")
            if process.poll() is not None:
                self._kill_owned()
                self._failed_until = self._clock() + _LOAD_FAILURE_BACKOFF_SECONDS
                raise RobotSttUnavailable("load_failed")
            try:
                if require_trusted_endpoint(self._socket_path):
                    return process
            except UntrustedEndpointError:
                pass  # a staging/foreign entry; keep waiting for our own publication
            if self._clock() >= deadline:
                self._kill_owned()
                self._failed_until = self._clock() + _LOAD_FAILURE_BACKOFF_SECONDS
                raise RobotSttUnavailable("load_timeout")
            time.sleep(_POLL_SECONDS)

    def _connect(self, process: subprocess.Popen) -> socket.socket:
        if not require_trusted_endpoint(self._socket_path):
            raise RobotSttUnavailable("no_endpoint")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(asr_worker.IO_TIMEOUT_SECONDS)
            sock.connect(str(self._socket_path))
            pid, uid = _peer_credentials(sock)
            if pid != process.pid or uid != os.getuid():
                raise UntrustedEndpointError("Robot STT socket is not served by the owned worker")
        except UntrustedEndpointError:
            sock.close()
            raise
        except OSError:
            sock.close()
            raise RobotSttUnavailable("connect_failed") from None
        return sock

    # ---- requests ----

    def prewarm(self, *, cancelled: Callable[[], bool]) -> dict[str, object] | None:
        """Single-flight bounded load + one synthetic decode, never capture/upload/fallback.

        A real first turn waits for this admission instead of starting a second worker.
        Subsequent warmups are idempotent in the worker, including across languages.
        """
        with self._process_lock:
            if not self._admission.acquire(blocking=False):
                return None
            self._warming.set()
            epoch = self._epoch
        def stale() -> bool:
            return cancelled() or self._epoch != epoch or self._closed
        try:
            process = self._ensure_worker(stale)
            with self._connect(process) as sock:
                if stale():
                    raise RobotSttCancelled("cancelled before warmup")
                sock.sendall(b'{"v":1,"op":"warmup"}\n')
                reply = json.loads(self._read_reply(sock, stale, self._clock() + self._decode_timeout))
            if stale():
                raise RobotSttCancelled("cancelled after warmup")
            if (not isinstance(reply, dict) or reply.get("v") != PROTOCOL_VERSION
                    or reply.get("status") != "ok" or reply.get("inference_warm") is not True):
                raise RobotSttUnavailable("protocol")
            return reply
        except UntrustedEndpointError:
            self._kill_owned()
            raise RobotSttUnavailable("untrusted_endpoint") from None
        except RobotSttCancelled:
            self._kill_owned()
            raise
        except (RobotSttUnavailable, OSError, ValueError):
            self._failed_until = self._clock() + _LOAD_FAILURE_BACKOFF_SECONDS
            self._kill_owned()
            raise
        finally:
            with self._process_lock:
                self._warming.clear()
                self._admission.release()

    def status(self) -> dict[str, object]:
        """Worker load/memory/identity metrics (no audio); starts the worker if needed."""
        with self._admission:
            process = self._ensure_worker(lambda: False)
            sock = self._connect(process)
            try:
                sock.sendall(b'{"v":1,"op":"status"}\n')
                return json.loads(self._read_reply(sock, lambda: False, self._clock() + 5.0))
            finally:
                sock.close()

    def _read_reply(self, sock: socket.socket, cancelled: Callable[[], bool], deadline: float) -> bytes:
        sock.settimeout(_POLL_SECONDS * 5)
        data = bytearray()
        while b"\n" not in data:
            if cancelled():
                raise RobotSttCancelled("cancelled during decode")
            if self._clock() >= deadline:
                raise RobotSttUnavailable("decode_timeout")
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                raise RobotSttUnavailable("connection_lost") from None
            if not chunk:
                raise RobotSttUnavailable("connection_lost")
            data += chunk
            if len(data) > 64 * 1024:
                raise RobotSttUnavailable("protocol")
        return bytes(data.split(b"\n", 1)[0])

    def transcribe(
        self,
        samples: np.ndarray,
        *,
        session: str,
        generation: int,
        language: str,
        cancelled: Callable[[], bool],
    ) -> RobotReceipt:
        """Recognize one in-RAM utterance for an immutable turn snapshot; raise on any failure."""
        if language not in {"en", "nl", "es", "de", "fr", "it", "pt", "auto"}:
            raise ValueError("Unsupported robot STT language")
        if self._closed:
            raise RobotSttUnavailable("closed")
        pcm = np.nan_to_num(np.asarray(samples, dtype=np.float32).ravel(), nan=0.0, posinf=1.0, neginf=-1.0)
        if pcm.size > MAX_SAMPLES:
            raise RobotSttUnavailable("too_large")
        epoch = self._epoch
        original_cancelled = cancelled
        def cancelled() -> bool:
            return original_cancelled() or self._epoch != epoch or self._closed
        wait_deadline = self._clock() + self._load_timeout + self._decode_timeout
        while True:
            with self._process_lock:
                admitted = self._admission.acquire(blocking=False)
                warming = self._warming.is_set()
            if admitted:
                break
            if cancelled():
                raise RobotSttCancelled("cancelled waiting for warmup")
            if not warming or self._clock() >= wait_deadline:
                raise RobotSttUnavailable("busy")
            time.sleep(_POLL_SECONDS)
        try:
            turn = next(self._turns)
            header = {
                "v": PROTOCOL_VERSION, "op": "recognize", "session": session, "turn": turn,
                "generation": int(generation), "language": language, "rate": SAMPLE_RATE,
                "samples": int(pcm.size),
            }
            process = self._ensure_worker(cancelled)
            try:
                sock = self._connect(process)
            except UntrustedEndpointError:
                self._kill_owned()
                raise RobotSttUnavailable("untrusted_endpoint") from None
            try:
                if cancelled():
                    raise RobotSttCancelled("cancelled before submission")
                try:
                    sock.sendall(json.dumps(header, separators=(",", ":")).encode() + b"\n")
                    sock.sendall(np.clip(pcm, -1.0, 1.0).astype("<f4", copy=False).tobytes())
                except OSError:
                    raise RobotSttUnavailable("connection_lost") from None
                raw = self._read_reply(sock, cancelled, self._clock() + self._decode_timeout)
            except (RobotSttCancelled, RobotSttUnavailable):
                # A stuck or abandoned native decode poisons the worker: kill the owned process.
                self._kill_owned()
                raise
            finally:
                sock.close()
            del pcm
            try:
                reply = json.loads(raw)
            except ValueError:
                self._kill_owned()
                raise RobotSttUnavailable("protocol") from None
            if not isinstance(reply, dict) or any(
                reply.get(key) != header[key] for key in ("session", "turn", "generation")
            ) or reply.get("v") != PROTOCOL_VERSION:
                self._kill_owned()  # stale or forged reply: never accepted
                raise RobotSttUnavailable("stale_reply")
            if reply.get("status") != "ok" or not isinstance(reply.get("text"), str):
                error = reply.get("error")
                raise RobotSttUnavailable(error if error in {"backend", "malformed", "too_large", "truncated"}
                                          else "protocol")
            accelerator = "gpu" if reply.get("accelerator") == "gpu" else "cpu"
            decode_ms = reply.get("decode_ms")
            if cancelled():
                raise RobotSttCancelled("cancelled after decode")
            return RobotReceipt(reply["text"].strip(), accelerator, decode_ms if type(decode_ms) is int else -1)
        finally:
            self._admission.release()

    def close(self) -> None:
        with self._process_lock:
            self._closed = True
        self._kill_owned()
