"""Quiesce app playback producers before sleep, without blocking ordinary Stop.

All Homebody audio producers/cleanup enter here. Native SDK wake and daemon
sleep sounds intentionally remain outside this app-only gate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class PlaybackGate:
    def __init__(self, media: Callable[[], Any]) -> None:
        self._media = media
        self.lock = threading.RLock()
        self._idle = threading.Condition(self.lock)
        self._active = 0
        self.blocked = False
        self.generation = 0

    def suspend(self) -> None:
        """Fence late callbacks and boundedly drain admitted media operations.

        Do not hold the shared lock across SDK pushes/clears: an intentionally
        quarantined hung push must not block ordinary Stop/clear or hide its
        unfinished ownership. Sleep refuses rather than remove a live wobbler.
        """
        deadline = time.monotonic() + 2.0
        if not self.lock.acquire(timeout=2.0):
            raise RuntimeError("A voice callback is still in flight; native sleep was not started")
        try:
            self.blocked = True
            self.generation += 1
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("App playback is still in flight; native sleep was not started")
                self._idle.wait(remaining)
        finally:
            self.lock.release()

    def awake(self) -> None:
        with self.lock:
            if self.blocked and self._active:
                raise RuntimeError("Old playback is still in flight; voice playback remains blocked")
            self.blocked = False

    def _run(self, operation: Callable[[], Any]) -> None:
        with self.lock:
            if self.blocked:
                return
            self._active += 1
        try:
            operation()
        finally:
            with self.lock:
                self._active -= 1
                self._idle.notify_all()

    def play_sound(self, path: str) -> None:
        self._run(lambda: self._media().play_sound(path))

    def push_audio_sample(self, samples: Any) -> None:
        self._run(lambda: self._media().push_audio_sample(samples))

    def start_playing(self) -> None:
        def start() -> None:
            method = getattr(self._media(), "start_playing", None)
            if callable(method):
                method()
        self._run(start)

    def clear(self) -> None:
        def clear() -> None:
            method = getattr(getattr(self._media(), "audio", None), "clear_player", None)
            if callable(method):
                method()
        self._run(clear)
