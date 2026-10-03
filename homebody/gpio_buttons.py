"""Optional green/red Raspberry Pi buttons through libgpiod, with fail-safe press handling.

Buttons are wired active-low to ground with the Pi's internal pull-up, so a press is a falling
edge and a release a rising edge. The service:

* debounces in hardware (libgpiod debounce period) and again in software;
* classifies a press as ``short`` on release, or ``long`` as soon as the hold threshold passes;
* ignores a button that is already held when monitoring starts until it is released once
  (startup ownership), and stops acting on a button held far too long (stuck switch);
* never raises into the app: a missing library, a busy line, or a failing action is reported
  in ``status()`` and the rest of Reachy keeps running.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

_LOGGER = logging.getLogger(__name__)

ButtonName = Literal["green", "red"]
Gesture = Literal["short", "long"]

DEBOUNCE_SECONDS = 0.03
STUCK_SECONDS = 30.0
# How long start() waits for a previous monitor that is still finishing a dispatched action.
RESTART_WAIT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ButtonEvent:
    button: ButtonName
    gesture: Gesture


@dataclass(frozen=True, slots=True)
class EdgeSample:
    """One debounced level change: ``pressed`` is True on the falling (press) edge."""

    offset: int
    pressed: bool
    timestamp: float


class _ButtonState:
    __slots__ = ("pressed", "pressed_at", "last_change", "long_sent", "armed", "stuck")

    def __init__(self, *, initially_pressed: bool, now: float) -> None:
        self.pressed = initially_pressed
        self.pressed_at = now
        # The first edge after start always counts; debounce only spaces later edges.
        self.last_change = float("-inf")
        self.long_sent = False
        # Startup ownership: a button already held at start must be released before it counts.
        self.armed = not initially_pressed
        self.stuck = False


class PressClassifier:
    """Turn raw press/release samples into short and long gestures; pure and clock-injected."""

    def __init__(
        self,
        buttons: dict[int, ButtonName],
        *,
        long_press_seconds: float,
        initially_pressed: Iterable[int] = (),
        now: float = 0.0,
    ) -> None:
        held = set(initially_pressed)
        self._names = dict(buttons)
        self._long = float(long_press_seconds)
        self._states = {offset: _ButtonState(initially_pressed=offset in held, now=now) for offset in buttons}

    def edge(self, sample: EdgeSample) -> list[ButtonEvent]:
        state = self._states.get(sample.offset)
        if state is None or sample.pressed == state.pressed:
            return []
        if sample.timestamp - state.last_change < DEBOUNCE_SECONDS:
            return []
        state.last_change = sample.timestamp
        state.pressed = sample.pressed
        if sample.pressed:
            state.pressed_at = sample.timestamp
            state.long_sent = False
            return []
        # Release.
        if not state.armed or state.stuck:
            state.armed = True
            state.stuck = False
            return []
        if state.long_sent:
            return []
        # Classify by the edge timestamps, not by whether poll() ran in between: a press and its
        # release can arrive in one batch after the thread was busy dispatching an earlier action.
        held = sample.timestamp - state.pressed_at
        if held >= STUCK_SECONDS:
            return []
        if held >= self._long:
            return [ButtonEvent(self._names[sample.offset], "long")]
        return [ButtonEvent(self._names[sample.offset], "short")]

    def poll(self, now: float) -> list[ButtonEvent]:
        """Emit long presses while a button is still held, and lock out stuck buttons."""
        events: list[ButtonEvent] = []
        for offset, state in self._states.items():
            if not state.pressed or not state.armed or state.stuck:
                continue
            held = now - state.pressed_at
            if held >= STUCK_SECONDS:
                state.stuck = True
                name = self._names[offset]
                _LOGGER.warning("GPIO %s button held for %.0f s; ignoring it until released", name, held)
                continue
            if not state.long_sent and held >= self._long:
                state.long_sent = True
                events.append(ButtonEvent(self._names[offset], "long"))
        return events

    def stuck_buttons(self) -> list[ButtonName]:
        return [self._names[offset] for offset, state in self._states.items() if state.stuck]


class LineReader(Protocol):
    def read_pressed(self) -> set[int]: ...

    def wait_edges(self, timeout: float) -> list[EdgeSample]: ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, list[int]], LineReader]


class _GpiodLineReader:
    """libgpiod v2 request: inputs with pull-up, both edges, and a hardware debounce period."""

    def __init__(self, chip: str, offsets: list[int]) -> None:
        import gpiod  # noqa: PLC0415 - optional dependency, only needed on the Pi
        from gpiod.line import Bias, Direction, Edge, Value  # noqa: PLC0415

        self._value_active = Value.ACTIVE
        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            edge_detection=Edge.BOTH,
            bias=Bias.PULL_UP,
            active_low=True,
            debounce_period=timedelta(seconds=DEBOUNCE_SECONDS),
        )
        self._offsets = offsets
        self._request = gpiod.request_lines(
            chip,
            consumer="homebody",
            config={tuple(offsets): settings},
        )
        self._rising = gpiod.EdgeEvent.Type.RISING_EDGE

    def read_pressed(self) -> set[int]:
        values = self._request.get_values(self._offsets)
        return {offset for offset, value in zip(self._offsets, values, strict=True) if value == self._value_active}

    def wait_edges(self, timeout: float) -> list[EdgeSample]:
        if not self._request.wait_edge_events(timedelta(seconds=timeout)):
            return []
        # active_low: a physical press (falling edge) is reported as a rising "active" edge.
        return [
            EdgeSample(event.line_offset, event.event_type == self._rising, event.timestamp_ns / 1e9)
            for event in self._request.read_edge_events()
        ]

    def close(self) -> None:
        self._request.release()


def gpiod_backend(chip: str, offsets: list[int]) -> LineReader:
    return _GpiodLineReader(chip, offsets)


class GpioButtonService:
    """Own the button lines in a background thread and dispatch classified gestures."""

    def __init__(
        self,
        dispatch: Callable[[ButtonEvent], None],
        *,
        backend: BackendFactory = gpiod_backend,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatch = dispatch
        self._backend = backend
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Each monitor thread gets its own stop event, so a restart can never revive an old one.
        self._stop = threading.Event()
        self._status: dict[str, object] = {"enabled": False, "available": False, "pins": {}}
        self._last_event = ""
        self._last_error = ""
        self._classifier: PressClassifier | None = None

    def start(self, *, chip: str, pins: dict[ButtonName, int], long_press_seconds: float) -> None:
        previous = self._stop_thread()
        if previous is not None and previous.is_alive():
            # The old monitor still holds the lines until its current action returns.
            previous.join(timeout=RESTART_WAIT_SECONDS)
            if previous.is_alive():
                self._set_status(
                    enabled=True,
                    available=False,
                    pins=dict(pins),
                    error="The previous button monitor is still finishing an action; save again to retry",
                )
                return
        if not pins:
            self._set_status(enabled=True, available=False, pins={}, error="No GPIO button pins are configured")
            return
        offsets = {offset: name for name, offset in pins.items()}
        reader: LineReader | None = None
        try:
            reader = self._backend(chip, sorted(offsets))
            pressed = reader.read_pressed()
        except Exception as exc:  # missing library, permissions, busy line, wrong chip
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    _LOGGER.debug("GPIO lines were already released", exc_info=True)
            _LOGGER.warning("GPIO buttons unavailable: %s", exc)
            self._set_status(enabled=True, available=False, pins=dict(pins), error=_describe(exc))
            return
        classifier = PressClassifier(
            offsets,
            long_press_seconds=long_press_seconds,
            initially_pressed=pressed,
            now=self._clock(),
        )
        stop = threading.Event()
        self._set_status(enabled=True, available=True, pins=dict(pins), error="")
        if pressed:
            held = ", ".join(offsets[offset] for offset in sorted(pressed))
            _LOGGER.info("GPIO %s held at start; it will act after its first release", held)
        thread = threading.Thread(
            target=self._run,
            args=(reader, classifier, stop),
            name="reachy-gpio-buttons",
            daemon=True,
        )
        with self._lock:
            self._classifier = classifier
            self._stop = stop
            self._thread = thread
        thread.start()

    def _stop_thread(self) -> threading.Thread | None:
        """Signal the current monitor to stop and wait briefly; return it for callers that must wait."""
        with self._lock:
            thread, self._thread = self._thread, None
            stop = self._stop
        stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        return thread

    def stop(self) -> None:
        self._stop_thread()
        self._set_status(enabled=False, available=False, pins={}, error=self._last_error)

    close = stop

    def status(self) -> dict[str, object]:
        with self._lock:
            status = dict(self._status)
            status["last_event"] = self._last_event
            status["last_error"] = self._last_error
            classifier = self._classifier
        status["stuck"] = classifier.stuck_buttons() if classifier is not None and status["available"] else []
        return status

    def _run(self, reader: LineReader, classifier: PressClassifier, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                events: list[ButtonEvent] = []
                for sample in reader.wait_edges(0.05):
                    events.extend(classifier.edge(sample))
                events.extend(classifier.poll(self._clock()))
                for event in events:
                    if stop.is_set():
                        # Settings changed while an earlier action ran; queued presses are stale.
                        break
                    self._handle(event)
        except Exception as exc:
            _LOGGER.exception("GPIO button monitoring stopped")
            with self._lock:
                self._last_error = _describe(exc)
                self._status["available"] = False
        finally:
            try:
                reader.close()
            except Exception:
                _LOGGER.debug("GPIO lines were already released", exc_info=True)

    def _handle(self, event: ButtonEvent) -> None:
        label = f"{event.button} {event.gesture}"
        with self._lock:
            self._last_event = label
        try:
            self._dispatch(event)
        except Exception as exc:
            _LOGGER.warning("GPIO %s press was not applied: %s", label, exc)
            with self._lock:
                self._last_error = f"{label}: {exc}"[:200]
        else:
            with self._lock:
                self._last_error = ""

    def _set_status(self, *, enabled: bool, available: bool, pins: dict[ButtonName, int], error: str) -> None:
        with self._lock:
            self._status = {"enabled": enabled, "available": available, "pins": pins}
            self._last_error = error


def _describe(exc: Exception) -> str:
    if isinstance(exc, ModuleNotFoundError):
        return "The gpiod library is not installed"
    if isinstance(exc, PermissionError):
        return "No permission to open the GPIO chip (add the service user to the gpio group)"
    return f"{type(exc).__name__}: {exc}"[:200]
