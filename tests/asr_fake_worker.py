"""Explicit fake backends for the real on-robot ASR worker protocol (tests only).

Runs the production ``asr_worker.serve`` loop with its kernel watchdog; only the native backend
factory and the artifact hashing are replaced. A fake success is never device or GPU proof.
Usage: python asr_fake_worker.py MODE SOCKET MANIFEST
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homebody import asr_worker  # noqa: E402


class FakeBackend:
    accelerator = "cpu"  # a fake never claims robot GPU inference

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.identity = {"backend": "fake", "env_keys": sorted(os.environ)}

    def recognize(self, samples: bytearray, language: str | None) -> str:
        if self.mode == "native_hang_decode":
            ctypes.PyDLL(None).sleep(600)  # deliberately retains the GIL in this child only
        if self.mode == "hang_decode" or len(samples) // 4 == 4321:  # 4321 samples: hang on demand
            time.sleep(600)
        if self.mode == "crash_decode":
            os._exit(9)
        if not any(samples):
            return ""  # silence
        return f"heard {len(samples) // 4} samples lang={language or 'auto'}"


def factory(mode: str):
    def build(_manifest):
        if mode == "native_hang_load":
            ctypes.PyDLL(None).sleep(600)
        if mode == "hang_load":
            time.sleep(600)
        if mode == "fail_load":
            raise RuntimeError("load failed")
        return FakeBackend(mode)

    return build


def main() -> None:
    mode, socket_path, manifest = sys.argv[1:4]
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    asr_worker._deny_network()
    if mode == "stale":
        original = asr_worker._send

        def forged(conn, reply):
            if "turn" in reply:
                reply = {**reply, "turn": reply["turn"] + 1}
            original(conn, reply)

        asr_worker._send = forged
    asr_worker.serve(
        socket_path, manifest,
        backend_factory=factory(mode),
        verify=asr_worker.read_manifest,  # shape/ownership checks; no multi-GB hashing in tests
        idle_seconds=float(sys.argv[4]) if len(sys.argv) > 4 else 30.0,
        decode_timeout=30.0,
        load_timeout=30.0,
        watchdog=asr_worker.alarm_watchdog,
    )
    os._exit(0)


if __name__ == "__main__":
    main()
