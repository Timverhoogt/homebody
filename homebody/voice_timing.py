"""Text-free, RAM-only voice latency marks for the owner diagnostics view.

Each turn keeps monotonic offsets (ms) from its first mark, the engine, the assistant mode and
whether it was the first turn of a session (cold) or a later one (warm). No transcript, reply,
tool argument or device name is ever stored. ``player_enqueued`` is when audio was handed to
the robot's player (an SDK enqueue), not proof that it was heard; ``playback_estimated`` is the
queue's own estimate of when that audio starts playing.

Recognition marks: ``speech_last_voiced`` is the last frame the endpointer judged voiced (the
actual end of speech), ``local_speech_end`` and ``endpoint_final`` are when the endpoint returned
(``local_speech_end`` keeps its historical meaning), ``stt_submitted`` is when samples were sent
to the selected recognizer and ``stt_final`` is when its final transcript was accepted.
"""

from __future__ import annotations

import threading
import time
from collections import deque

MARKS = (
    "wake",
    "session_ready",
    "speech_last_voiced",
    "local_speech_end",
    "endpoint_final",
    "stt_submitted",
    "stt_final",
    "provider_speech_end",
    "tool_requested",
    "tool_result",
    "first_provider_audio",
    "player_enqueued",
    "playback_estimated",
    "useful_result_verified",
    "turn_end",
)
_ENGINES = {"pipeline", "realtime", "live"}
_MODES = {"everyday", "workspace"}


class VoiceTimings:
    def __init__(self, limit: int = 50, *, clock=time.monotonic) -> None:
        self._turns: deque[dict[str, object]] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._clock = clock
        self._current: dict[str, object] | None = None
        self._started = 0.0

    def begin(self, engine: str, mode: str, *, warm: bool) -> None:
        with self._lock:
            self._current = {
                "engine": engine if engine in _ENGINES else "unknown",
                "mode": mode if mode in _MODES else "unknown",
                "warm": bool(warm),
                "marks": {},
            }
            self._started = self._clock()
            self._turns.append(self._current)

    def mark(self, name: str, *, at: float | None = None, once: bool = True) -> None:
        if name not in MARKS:
            return
        with self._lock:
            if self._current is None:
                return
            marks = self._current["marks"]
            assert isinstance(marks, dict)
            if once and name in marks:
                return
            marks[name] = max(0, int(((at if at is not None else self._clock()) - self._started) * 1000))

    def end(self) -> None:
        self.mark("turn_end")
        with self._lock:
            self._current = None

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {"engine": t["engine"], "mode": t["mode"], "warm": t["warm"], "marks": dict(t["marks"])}  # type: ignore[arg-type]
                for t in self._turns
            ]

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._current = None
