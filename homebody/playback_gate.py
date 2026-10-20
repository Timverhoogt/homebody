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
        self._cleanup_allowed = True
        self._stop_confirmed = False
        self._recovering = False

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
            self._stop_confirmed = False
            self._cleanup_allowed = True
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("App playback is still in flight; native sleep was not started")
                self._idle.wait(remaining)
            # No more SDK calls may enter between drain and stop_playing.
            # A timeout deliberately leaves cleanup admitted: the old push can
            # still enqueue after refusal and its owner must really flush it.
            self._cleanup_allowed = False
        finally:
            self.lock.release()

    def confirm_stopped(self) -> None:
        """Called only after the SDK stop returns, under the shared motion lock."""
        with self.lock:
            if not self.blocked or self._active:
                raise RuntimeError("Playback stop cannot be confirmed before quiescence")
            self._stop_confirmed = True

    def awake(self) -> None:
        with self.lock:
            if self.blocked and (self._active or self._recovering):
                raise RuntimeError("Old playback is still in flight; voice playback remains blocked")
            if not self.blocked:
                return
            generation = self.generation
            needs_cleanup = not self._stop_confirmed
            if needs_cleanup and not self._cleanup_allowed:
                raise RuntimeError("Playback stop is not confirmed; voice playback remains blocked")
            self._recovering = True
        try:
            # A refused sleep never stopped the SDK. Even a producer without its
            # own cleanup must not carry old queued PCM into a wake. A second
            # wake cannot open admission ahead of this delayed recovery clear.
            if needs_cleanup:
                self.clear()  # Never hold the shared lock across opaque SDK work.
            with self.lock:
                if generation != self.generation or self._active:
                    raise RuntimeError("Playback changed during recovery; voice playback remains blocked")
                self.blocked = False
                self._cleanup_allowed = True
        finally:
            with self.lock:
                self._recovering = False

    def _run(self, operation: Callable[[], Any], *, cleanup: bool = False) -> None:
        with self.lock:
            if self.blocked:
                if not cleanup or self._stop_confirmed:
                    return
                if not self._cleanup_allowed:
                    # A fold has drained producers and owns SDK stop/clear now. Late
                    # voice finalizers must not race that stop or mask session.close().
                    # This does not confirm the stop or reopen playback; awake() still
                    # requires the real stop receipt (and reports failed stops).
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
        self._run(clear, cleanup=True)
