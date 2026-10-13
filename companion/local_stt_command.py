"""Local-only Whisper command for a bridge using a separate Hermes runtime.

Run with a Python environment that already has faster-whisper. Configure
HERMES_LOCAL_STT_COMMAND with {input_path} and {output_dir}; no credentials needed.
Models must already be cached. This never downloads or invokes a cloud fallback.

Loading the model cost ~1.2 s of every short voice turn, so the command reuses one model through a
single per-user worker process. The worker listens on a Unix socket inside a private 0700
directory, serves one request at a time and never logs audio or transcripts. Every phase is hard
bounded: in worker-process mode a default-disposition SIGALRM (delivered by the kernel, so it also
ends a model load or decode stuck inside a C extension) caps load, decode and idle time, and a
decode that overruns its deadline ends the worker. The client verifies the directory and socket
before connecting, refuses untrusted endpoints, and kills/reaps a worker it started that does not
become ready. ``--no-worker`` keeps the one-shot behaviour, itself bounded by the same alarm.
"""

import argparse
import fcntl
import json
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MODEL_NAME = "base"
DECODE_OPTIONS = {
    "beam_size": 5,
    "vad_filter": True,
    "condition_on_previous_text": False,
    "without_timestamps": True,
}
# Auto-detection costs an extra 30-second encoder pass (~2.2 s of a ~4.6 s short turn on two
# threads). The bridge names a Kids temp file homebody-stt-<lang>-* for the parent-selected
# profile language; every other input keeps auto-detection.
_LANGUAGE_HINT = re.compile(r"^homebody-stt-(en|nl)-")
IDLE_SECONDS = 300.0
_REQUEST_LIMIT = 4096
_LOAD_TIMEOUT_SECONDS = 60.0
_SPAWN_WAIT_SECONDS = 30.0
_REPLY_TIMEOUT_SECONDS = 60.0
_WATCHDOG_GRACE_SECONDS = 5.0


class UntrustedEndpointError(RuntimeError):
    """The worker directory or socket could be controlled by someone else; never connect."""


def load_model():
    from faster_whisper import WhisperModel

    return WhisperModel(
        MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1, local_files_only=True
    )


def language_hint(input_path):
    match = _LANGUAGE_HINT.match(Path(input_path).name)
    return match.group(1) if match else None


def transcribe(model, input_path):
    segments, _ = model.transcribe(input_path, language=language_hint(input_path), **DECODE_OPTIONS)
    # Preserve Hermes' conservative silence-hallucination AND gate.
    return " ".join(s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()


def default_socket_path():
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base, f"homebody-stt-{os.getuid()}", "worker.sock")


def _check_private_dir(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UntrustedEndpointError("Local STT worker directory is not private")


def _private_dir(path):
    """Create/verify a directory only this user can enter; refuse symlinks or foreign owners."""
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _check_private_dir(path)


def require_trusted_endpoint(socket_path):
    """Validate directory and socket before any connection or disclosure; False if no socket yet."""
    socket_path = Path(socket_path)
    try:
        _check_private_dir(socket_path.parent)
    except FileNotFoundError:
        return False
    try:
        info = os.lstat(socket_path)
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UntrustedEndpointError("Local STT worker socket is not private")
    return True


def _recv_line(conn):
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > _REQUEST_LIMIT * 16:
            raise ValueError("Local STT message is too large")
    return data.split(b"\n", 1)[0]


def _no_watchdog(_seconds):
    pass


def _alarm_watchdog(seconds):
    """Process-mode hard bound: SIGALRM with its default action terminates without needing the GIL."""
    signal.alarm(max(1, math.ceil(seconds + _WATCHDOG_GRACE_SECONDS)))


def serve(socket_path, *, idle_seconds=IDLE_SECONDS, model_factory=None, watchdog=_no_watchdog):
    """Serve sequential requests with one loaded model; return once idle or after a stuck decode."""
    socket_path = Path(socket_path)
    model_factory = model_factory or load_model
    _private_dir(socket_path.parent)
    lock = open(socket_path.with_suffix(".lock"), "a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # Another worker already owns this socket.
        lock.truncate(0)
        lock.write(str(os.getpid()))
        lock.flush()
        watchdog(_LOAD_TIMEOUT_SECONDS)
        model = model_factory()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        staging = socket_path.with_name(f".worker-{os.getpid()}.sock")
        published_inode = None
        served = 0
        try:
            staging.unlink(missing_ok=True)
            server.bind(str(staging))
            os.chmod(staging, 0o600)
            server.listen(4)
            # Publish atomically: the path never names a socket that is not yet accepting.
            published_inode = os.lstat(staging).st_ino
            os.replace(staging, socket_path)
            server.settimeout(idle_seconds)
            while True:
                watchdog(idle_seconds)
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    return served
                watchdog(_REPLY_TIMEOUT_SECONDS)
                with conn:
                    conn.settimeout(_REPLY_TIMEOUT_SECONDS)
                    deadline = time.monotonic() + _REPLY_TIMEOUT_SECONDS
                    try:
                        request_payload = json.loads(_recv_line(conn)[:_REQUEST_LIMIT])
                        input_path = str(request_payload["input"])
                        if not Path(input_path).is_file():
                            raise ValueError("Missing input audio")
                    except Exception as exc:  # Report only the error type, never content.
                        _send(conn, {"error": type(exc).__name__})
                        served += 1
                        continue
                    result: dict[str, object] = {}
                    done = threading.Event()

                    def decode(path=input_path, holder=result, finished=done):
                        try:
                            holder["text"] = transcribe(model, path)
                        except Exception as exc:
                            holder["error"] = type(exc).__name__
                        finally:
                            finished.set()

                    threading.Thread(target=decode, name="local-stt-decode", daemon=True).start()
                    if not done.wait(max(0.0, deadline - time.monotonic())):
                        # A stuck decode poisons this worker: stop serving so the process exits.
                        _send(conn, {"error": "TimeoutError"})
                        return served
                    _send(conn, dict(result))
                    served += 1
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


def _send(conn, reply):
    try:
        conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
    except OSError:
        pass


def request(socket_path, input_path, *, timeout=_REPLY_TIMEOUT_SECONDS):
    """Ask a trusted running worker for one transcript; raise OSError/RuntimeError if unusable."""
    if not require_trusted_endpoint(socket_path):
        raise FileNotFoundError("No local STT worker socket")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall((json.dumps({"input": str(Path(input_path).resolve())}) + "\n").encode("utf-8"))
        reply = json.loads(_recv_line(client) or b"{}")
    finally:
        client.close()
    if "text" not in reply:
        raise RuntimeError("Local STT worker failed")
    return str(reply["text"])


def _spawn_worker(socket_path, idle_seconds):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.abspath(__file__),
            "--serve",
            "--socket",
            str(socket_path),
            "--idle-seconds",
            str(idle_seconds),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _reap(worker):
    if worker.poll() is None:
        worker.kill()
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def transcribe_via_worker(socket_path, input_path, *, idle_seconds=IDLE_SECONDS, spawn_wait=None):
    """Use the warm worker, starting it once if needed.

    Untrusted endpoints are refused before any connection, spawn or decode. A worker that times
    out is never doubled up with another load; a worker this call started is killed and reaped if
    it does not become ready. Only when no worker can run at all does it decode in-process.
    """
    socket_path = Path(socket_path)
    if require_trusted_endpoint(socket_path):
        try:
            return request(socket_path, input_path)
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # Stale socket from a worker that already exited.
    _private_dir(socket_path.parent)
    require_trusted_endpoint(socket_path)
    try:
        worker = _spawn_worker(socket_path, idle_seconds)
    except OSError:
        # No worker process can start; decode once in this (alarm-bounded) command process.
        if worker_running(socket_path):
            raise TimeoutError("Another local STT worker is still loading") from None
        return transcribe(load_model(), input_path)
    deadline = time.monotonic() + (_SPAWN_WAIT_SECONDS if spawn_wait is None else spawn_wait)
    try:
        while time.monotonic() < deadline:
            try:
                return request(socket_path, input_path)
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            # Our worker may have lost a start race; keep waiting only while some worker is loading.
            if worker.poll() is not None and not worker_running(socket_path):
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("Local STT worker did not become ready")
    finally:
        if worker.poll() is None and not _socket_ready(socket_path):
            _reap(worker)
        elif worker.poll() is not None:
            _reap(worker)
    if worker_running(socket_path):
        raise TimeoutError("Another local STT worker is still loading")
    return transcribe(load_model(), input_path)


def _socket_ready(socket_path):
    try:
        return require_trusted_endpoint(socket_path)
    except UntrustedEndpointError:
        return False


def worker_running(socket_path):
    """Return the worker pid holding this socket's lock, or 0 when no worker is running."""
    try:
        with open(Path(socket_path).with_suffix(".lock"), "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.seek(0)
                content = lock.read().strip()
                return int(content) if content.isdigit() else -1
            fcntl.flock(lock, fcntl.LOCK_UN)
            return 0
    except OSError:
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output-dir")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--socket", default="")
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    args = parser.parse_args()
    socket_path = Path(args.socket) if args.socket else default_socket_path()
    idle = min(max(args.idle_seconds, 1.0), 3600.0)
    if args.serve:
        # Only a dedicated worker process installs the kernel-enforced watchdog.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        try:
            serve(socket_path, idle_seconds=idle, watchdog=_alarm_watchdog)
        finally:
            os._exit(0)  # Never linger on a daemon decode thread after serving ends.
    if not args.input or not args.output_dir:
        parser.error("--input and --output-dir are required")
    # The command process is bounded as a whole, including any in-process fallback load. The
    # alarm is cancelled (and the previous handler restored) as soon as the work returns.
    previous = signal.signal(signal.SIGALRM, signal.SIG_DFL)
    try:
        if args.no_worker or os.environ.get("HOMEBODY_STT_WORKER") == "0":
            _alarm_watchdog(_LOAD_TIMEOUT_SECONDS + _REPLY_TIMEOUT_SECONDS)
            text = transcribe(load_model(), args.input)
        else:
            _alarm_watchdog(_SPAWN_WAIT_SECONDS + _LOAD_TIMEOUT_SECONDS + _REPLY_TIMEOUT_SECONDS)
            text = transcribe_via_worker(socket_path, args.input, idle_seconds=idle)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    Path(args.output_dir, "transcript.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
