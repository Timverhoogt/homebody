"""End-to-end check of tools/gpio_acceptance.py against the real app and the real button service.

The GPIO lines and the robot runtime are simulated; everything between them is production code: the
libgpiod-facing service with its classifier (debounce, long press, startup ownership, stuck lockout),
the app's button mapping, the settings routes and the Kids lock. A scripted operator follows each
instruction the tool gives, the way a person at the robot would.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import homebody.gpio_buttons as gpio_module
import homebody.main as main_module
from homebody.config import AppConfig
from homebody.gpio_buttons import EdgeSample, GpioButtonService
from homebody.main import Homebody

_SPEC = importlib.util.spec_from_file_location(
    "gpio_acceptance", Path(__file__).parents[1] / "tools" / "gpio_acceptance.py"
)
acceptance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = acceptance  # dataclasses look their module up here
_SPEC.loader.exec_module(acceptance)  # type: ignore[union-attr]

GREEN, RED = 17, 27
LONG = 0.5


class Buttons:
    """Two physical buttons; each re-opened line request sees the buttons as they are right now."""

    def __init__(self) -> None:
        self.held: set[int] = set()
        self.reader: Reader | None = None
        self.lock = threading.Lock()

    def backend(self, chip: str, offsets: list[int]) -> Reader:
        reader = Reader(self)
        with self.lock:
            self.reader = reader
        return reader

    def down(self, offset: int) -> None:
        self.held.add(offset)
        self._push(EdgeSample(offset, True, time.monotonic()))

    def up(self, offset: int) -> None:
        self.held.discard(offset)
        self._push(EdgeSample(offset, False, time.monotonic()))

    def tap(self, offset: int) -> None:
        self.down(offset)
        time.sleep(0.15)
        self.up(offset)

    def hold(self, offset: int, seconds: float) -> None:
        self.down(offset)
        time.sleep(seconds)
        self.up(offset)

    def _push(self, sample: EdgeSample) -> None:
        with self.lock:
            reader = self.reader
        if reader is not None:
            reader.push(sample)


class Reader:
    def __init__(self, buttons: Buttons) -> None:
        self.buttons = buttons
        self.queue: list[EdgeSample] = []
        self.lock = threading.Lock()

    def push(self, sample: EdgeSample) -> None:
        with self.lock:
            self.queue.append(sample)

    def read_pressed(self) -> set[int]:
        return set(self.buttons.held)

    def wait_edges(self, timeout: float) -> list[EdgeSample]:
        with self.lock:
            samples, self.queue = self.queue, []
        if not samples:
            time.sleep(min(timeout, 0.01))
        return samples

    def close(self) -> None:
        pass


class Robot:
    """The parts of the runtime the buttons touch, behaving like the real one."""

    control_ready = True

    def __init__(self) -> None:
        self.power_mode = "awake"
        self.state = "waiting_for_wake_word"
        self.folded = False
        self.kids_active = False
        self.kids_controls_locked = False
        self.kids_end_reason = ""

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "power_mode": self.power_mode,
            "head_safely_folded": self.folded,
            "kids_mode": {
                "active": self.kids_active,
                "last_end_reason": self.kids_end_reason,
                "last_fold_succeeded": True if self.kids_end_reason else None,
            },
        }

    def physical_stop(self) -> dict[str, object]:
        kids = self.kids_active
        if self.state == "speaking":
            self.state = "waiting_for_wake_word"
        if kids:
            self.kids_active = self.kids_controls_locked = False
            self.kids_end_reason = "physical_stop"
            self.folded = True
        return {"ok": True, "kids_stopped": kids}

    def request_button_wake(self) -> str:
        if self.power_mode in {"sleep", "meeting"}:
            self.set_power_mode("awake")
            return "awake"
        self.power_mode, self.folded, self.state = "awake", False, "listening"
        return "listen"

    def set_power_mode(self, mode: str, duration_seconds: float = 0.0) -> dict[str, object]:
        self.power_mode = mode
        self.folded = mode in {"standby", "sleep", "meeting"}
        self.state = "waiting_for_wake_word"
        return self.status()


class Operator:
    """Does what the tool asks, a moment after it asks."""

    def __init__(self, buttons: Buttons, robot: Robot, *, misbehave: str = "") -> None:
        self.buttons = buttons
        self.robot = robot
        self.misbehave = misbehave
        self.heard: list[str] = []

    def say(self, text: str) -> None:
        self.heard.append(text)
        threading.Thread(target=self._act, args=(text,), daemon=True).start()

    def _act(self, text: str) -> None:
        time.sleep(0.3)
        b = self.buttons
        if "Press GREEN briefly" in text or "press GREEN briefly once more" in text:
            b.tap(GREEN)
        elif "Ask Reachy a question" in text:
            self.robot.state = "speaking"
            time.sleep(0.3)
            if self.misbehave != "no-red-during-answer":
                b.tap(RED)
        elif "Hold RED for about" in text:
            b.hold(RED, LONG + 0.4)
        elif "Hold GREEN for about" in text:
            b.hold(GREEN, LONG + 0.4)
        elif "Start a short Kids Mode" in text:
            self.robot.kids_active = self.robot.kids_controls_locked = True
            self.robot.folded = False
        elif "Kids Mode is on" in text:
            b.tap(RED)
        elif "Press and HOLD GREEN" in text:
            b.down(GREEN)
        elif "RELEASE GREEN" in text:
            b.up(GREEN)
        elif "Hold GREEN down for about" in text:
            b.hold(GREEN, gpio_module.STUCK_SECONDS + 0.8)


def build(monkeypatch: pytest.MonkeyPatch, *, misbehave: str = "") -> tuple[Any, Operator]:
    config = AppConfig(gpio_buttons_enabled=True, gpio_green_pin=GREEN, gpio_red_pin=RED, gpio_long_press_seconds=LONG)
    stored = [config]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda value: stored.append(value))
    buttons, robot = Buttons(), Robot()
    app = Homebody(False)
    app._runtime = robot  # type: ignore[assignment]
    app._gpio_buttons = GpioButtonService(app._handle_button_event, backend=buttons.backend)
    app._start_gpio_buttons(config)
    client = TestClient(app.settings_app)

    class Api:
        def get(self, path: str) -> tuple[int, Any]:
            response = client.get(path)
            return response.status_code, response.json()

        def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
            response = client.post(path, json=body)
            return response.status_code, response.json()

    operator = Operator(buttons, robot, misbehave=misbehave)
    return Api(), operator


def arguments(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    values = {
        "url": "http://reachy.test:8042",
        "timeout": 8.0,
        "skip_voice": False,
        "skip_kids": False,
        "stuck": False,
        "report": str(tmp_path / "report.md"),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_correct_robot_passes_every_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(gpio_module, "STUCK_SECONDS", 1.5)
    api, operator = build(monkeypatch)

    code = acceptance.run(arguments(tmp_path, stuck=True), api=api, ask=lambda _: "yes", say=operator.say)

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert code == 0, report
    for name in (
        "Buttons monitored",
        "Start in Standby",
        "Green short in Standby",
        "Red short during an answer",
        "Red long press",
        "Green short in Sleep",
        "Green long press",
        "Red short in Kids Mode",
        "Held at start is ignored",
        "Stuck button lockout",
    ):
        assert f"| PASS | {name} |" in report
    assert "0 failed, 0 skipped" in report
    assert "release ignored=True" in report and "next press acted" in report
    assert "events=['red short']" in report  # the Kids step counts only its own press


def test_a_missed_press_fails_with_the_events_seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api, operator = build(monkeypatch, misbehave="no-red-during-answer")

    code = acceptance.run(
        arguments(tmp_path, timeout=1.5, skip_kids=True), api=api, ask=lambda _: "yes", say=operator.say
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert code == 1
    assert "| FAIL | Red short during an answer |" in report and "no 'red short' within 2 s" in report


def test_nothing_moves_without_a_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api, operator = build(monkeypatch)
    assert acceptance.run(arguments(tmp_path), api=api, ask=lambda _: "no", say=operator.say) == 1
    assert operator.heard == []


def test_preconditions_fail_when_a_button_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = AppConfig(gpio_buttons_enabled=True, gpio_green_pin=GREEN, gpio_red_pin=None)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    app = Homebody(False)
    app._runtime = Robot()  # type: ignore[assignment]
    app._gpio_buttons = GpioButtonService(app._handle_button_event, backend=Buttons().backend)
    app._start_gpio_buttons(config)
    client = TestClient(app.settings_app)

    class Api:
        def get(self, path: str) -> tuple[int, Any]:
            response = client.get(path)
            return response.status_code, response.json()

    asked: list[str] = []
    code = acceptance.run(arguments(tmp_path), api=Api(), ask=lambda prompt: asked.append(prompt) or "yes")
    assert code == 1 and asked == []  # stops before the safety prompt
    assert "| FAIL | Buttons monitored |" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_wiring_counts_presses_and_bounce() -> None:
    clean = [(0.0, True), (0.2, False), (0.5, True), (0.7, False)]
    bouncy = [(0.0, True), (0.005, False), (0.01, True), (0.2, False), (0.21, True), (0.22, False)]
    assert acceptance._count_presses(clean) == (2, 0)
    assert acceptance._count_presses(bouncy) == (1, 4)


def fake_gpiod(script: dict[str, list[tuple[int, bool]]], idle_pressed: set[int] = frozenset()):  # type: ignore[no-untyped-def]
    """A stand-in gpiod module: each operator prompt releases that button's scripted edges."""
    import enum
    import types

    class Value(enum.Enum):
        INACTIVE = 0
        ACTIVE = 1

    edge_type = types.SimpleNamespace(RISING_EDGE="rise", FALLING_EDGE="fall")
    pending: list[types.SimpleNamespace] = []

    class Request:
        def get_values(self, lines: list[int]) -> list[Value]:
            return [Value.ACTIVE if line in idle_pressed else Value.INACTIVE for line in lines]

        def wait_edge_events(self, timeout: Any) -> bool:
            time.sleep(0.01)
            return bool(pending)

        def read_edge_events(self) -> list[types.SimpleNamespace]:
            events, pending[:] = list(pending), []
            return events

        def release(self) -> None:
            pass

    module = types.ModuleType("gpiod")
    module.LineSettings = lambda **kwargs: kwargs
    module.request_lines = lambda chip, consumer, config: Request()
    module.EdgeEvent = types.SimpleNamespace(Type=edge_type)
    line = types.ModuleType("gpiod.line")
    line.Bias = line.Direction = line.Edge = types.SimpleNamespace(PULL_UP=1, INPUT=1, BOTH=1)
    line.Value = Value

    def ask(prompt: str) -> str:
        for name, edges in script.items():
            if name.upper() in prompt:
                now = time.time()
                for offset, pressed in edges:
                    pending.append(
                        types.SimpleNamespace(
                            line_offset=offset,
                            event_type="rise" if pressed else "fall",
                            timestamp_ns=int((now + len(pending) * 0.1) * 1e9),
                        )
                    )
        return ""

    return module, line, ask


def wiring_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(tmp_path / "none.json"),
        chip="/dev/gpiochip0",
        green=GREEN,
        red=RED,
        presses=2,
        seconds=0.5,
        report=str(tmp_path / "wiring.md"),
    )


def test_wiring_passes_clean_buttons_and_catches_crossed_wires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean = {"green": [(GREEN, True), (GREEN, False)] * 2, "red": [(RED, True), (RED, False)] * 2}
    module, line, ask = fake_gpiod(clean)
    monkeypatch.setitem(sys.modules, "gpiod", module)
    monkeypatch.setitem(sys.modules, "gpiod.line", line)
    assert acceptance.wiring(wiring_args(tmp_path), ask=ask) == 0
    report = (tmp_path / "wiring.md").read_text(encoding="utf-8")
    assert "| PASS | green presses |" in report and "| PASS | red isolation |" in report

    crossed = {"green": [(GREEN, True), (RED, True), (GREEN, False), (RED, False)] * 2, "red": clean["red"]}
    module, line, ask = fake_gpiod(crossed, idle_pressed={RED})
    monkeypatch.setitem(sys.modules, "gpiod", module)
    monkeypatch.setitem(sys.modules, "gpiod.line", line)
    assert acceptance.wiring(wiring_args(tmp_path), ask=ask) == 1
    report = (tmp_path / "wiring.md").read_text(encoding="utf-8")
    assert "| FAIL | red idle level |" in report and "| FAIL | green isolation |" in report
