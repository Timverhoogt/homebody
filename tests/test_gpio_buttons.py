from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_voice_session_lifecycle import make_runtime

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.gpio_buttons import (
    STUCK_SECONDS,
    ButtonEvent,
    EdgeSample,
    GpioButtonService,
    PressClassifier,
)
from homebody.main import Homebody

GREEN, RED = 17, 27


def classifier(**kwargs: object) -> PressClassifier:
    return PressClassifier({GREEN: "green", RED: "red"}, long_press_seconds=2.0, **kwargs)  # type: ignore[arg-type]


def press(offset: int, at: float) -> EdgeSample:
    return EdgeSample(offset, True, at)


def release(offset: int, at: float) -> EdgeSample:
    return EdgeSample(offset, False, at)


def test_short_press_is_reported_on_release() -> None:
    buttons = classifier()

    assert buttons.edge(press(GREEN, 1.0)) == []
    assert buttons.poll(1.5) == []
    assert buttons.edge(release(GREEN, 1.6)) == [ButtonEvent("green", "short")]


def test_long_press_fires_once_while_held_and_release_adds_no_short() -> None:
    buttons = classifier()
    buttons.edge(press(RED, 1.0))

    assert buttons.poll(2.9) == []
    assert buttons.poll(3.0) == [ButtonEvent("red", "long")]
    assert buttons.poll(4.0) == []
    assert buttons.edge(release(RED, 4.5)) == []


def test_contact_bounce_inside_the_debounce_window_is_ignored() -> None:
    buttons = classifier()
    buttons.edge(press(GREEN, 1.0))
    assert buttons.edge(release(GREEN, 1.4)) == [ButtonEvent("green", "short")]

    # A bounce 10 ms after release cannot start a second press.
    assert buttons.edge(press(GREEN, 1.41)) == []
    assert buttons.edge(release(GREEN, 1.42)) == []


def test_button_held_at_startup_is_ignored_until_released_once() -> None:
    buttons = classifier(initially_pressed=[RED], now=0.0)

    assert buttons.poll(10.0) == []
    assert buttons.edge(release(RED, 10.5)) == []
    buttons.edge(press(RED, 11.0))
    assert buttons.edge(release(RED, 11.2)) == [ButtonEvent("red", "short")]


def test_stuck_button_is_locked_out_until_it_releases() -> None:
    buttons = classifier()
    buttons.edge(press(GREEN, 0.0))

    assert buttons.poll(2.0) == [ButtonEvent("green", "long")]
    assert buttons.poll(STUCK_SECONDS + 1.0) == []
    assert buttons.stuck_buttons() == ["green"]
    assert buttons.edge(release(GREEN, STUCK_SECONDS + 2.0)) == []
    assert buttons.stuck_buttons() == []
    buttons.edge(press(GREEN, STUCK_SECONDS + 3.0))
    assert buttons.edge(release(GREEN, STUCK_SECONDS + 3.2)) == [ButtonEvent("green", "short")]


def test_unknown_lines_and_repeated_levels_are_ignored() -> None:
    buttons = classifier()

    assert buttons.edge(press(4, 1.0)) == []
    assert buttons.edge(release(GREEN, 1.0)) == []


class FakeReader:
    def __init__(self, pressed: set[int] | None = None) -> None:
        self.pressed = pressed or set()
        self.queue: list[EdgeSample] = []
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def push(self, *samples: EdgeSample) -> None:
        with self.lock:
            self.queue.extend(samples)

    def read_pressed(self) -> set[int]:
        return set(self.pressed)

    def wait_edges(self, timeout: float) -> list[EdgeSample]:
        with self.lock:
            samples, self.queue = self.queue, []
        if not samples:
            time.sleep(min(timeout, 0.01))
        return samples

    def close(self) -> None:
        self.closed.set()


def wait_for(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition was not reached"
        time.sleep(0.01)


def test_service_dispatches_gestures_and_releases_lines_on_stop() -> None:
    reader = FakeReader()
    events: list[ButtonEvent] = []
    service = GpioButtonService(events.append, backend=lambda chip, offsets: reader)

    service.start(chip="/dev/gpiochip0", pins={"green": GREEN, "red": RED}, long_press_seconds=2.0)
    assert service.status()["available"] is True
    reader.push(press(RED, 100.0), release(RED, 100.2))
    wait_for(lambda: events == [ButtonEvent("red", "short")])
    assert service.status()["last_event"] == "red short"

    service.stop()
    assert reader.closed.is_set()
    assert service.status()["enabled"] is False


def test_service_isolates_a_failing_action_and_keeps_monitoring() -> None:
    reader = FakeReader()
    handled: list[ButtonEvent] = []

    def dispatch(event: ButtonEvent) -> None:
        handled.append(event)
        if len(handled) == 1:
            raise RuntimeError("Voice runtime is still starting")

    service = GpioButtonService(dispatch, backend=lambda chip, offsets: reader)
    service.start(chip="/dev/gpiochip0", pins={"green": GREEN}, long_press_seconds=2.0)
    try:
        reader.push(press(GREEN, 1.0), release(GREEN, 1.2))
        wait_for(lambda: len(handled) == 1)
        wait_for(lambda: "still starting" in str(service.status()["last_error"]))
        reader.push(press(GREEN, 2.0), release(GREEN, 2.2))
        wait_for(lambda: len(handled) == 2)
        wait_for(lambda: service.status()["last_error"] == "")
    finally:
        service.stop()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ModuleNotFoundError("No module named 'gpiod'"), "gpiod library is not installed"),
        (PermissionError(13, "Permission denied"), "gpio group"),
        (OSError(16, "Device or resource busy"), "Device or resource busy"),
    ],
)
def test_unavailable_gpio_is_reported_without_raising(error: Exception, message: str) -> None:
    def backend(chip: str, offsets: list[int]) -> FakeReader:
        raise error

    service = GpioButtonService(lambda event: None, backend=backend)
    service.start(chip="/dev/gpiochip0", pins={"green": GREEN}, long_press_seconds=2.0)

    status = service.status()
    assert status["enabled"] is True and status["available"] is False
    assert message in str(status["last_error"])


def test_lines_are_released_when_the_initial_read_fails() -> None:
    reader = FakeReader()

    def broken_read() -> set[int]:
        raise OSError("read failed")

    reader.read_pressed = broken_read  # type: ignore[method-assign]
    service = GpioButtonService(lambda event: None, backend=lambda chip, offsets: reader)
    service.start(chip="/dev/gpiochip0", pins={"green": GREEN}, long_press_seconds=2.0)

    assert reader.closed.is_set()
    assert service.status()["available"] is False


def test_no_configured_pins_reports_instead_of_requesting_lines() -> None:
    def backend(chip: str, offsets: list[int]) -> FakeReader:
        raise AssertionError("no line request expected")

    service = GpioButtonService(lambda event: None, backend=backend)
    service.start(chip="/dev/gpiochip0", pins={}, long_press_seconds=2.0)

    assert "No GPIO button pins" in str(service.status()["last_error"])


@pytest.mark.parametrize(
    "updates",
    [
        {"gpio_green_pin": 17, "gpio_red_pin": 17},
        {"gpio_red_pin": 1024},
        {"gpio_red_pin": -1},
        {"gpio_red_pin": True},
        {"gpio_long_press_seconds": 0.1},
        {"gpio_chip": "/etc/passwd"},
    ],
)
def test_invalid_gpio_settings_are_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AppConfig(**updates)  # type: ignore[arg-type]


def test_gpio_defaults_are_disabled_with_green_on_gpio17_and_red_unassigned() -> None:
    config = AppConfig()

    assert config.gpio_buttons_enabled is False
    assert (config.gpio_green_pin, config.gpio_red_pin, config.gpio_chip) == (17, None, "/dev/gpiochip0")


class RecordingRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.control_ready = ready
        self.calls: list[str] = []

    def physical_stop(self) -> dict[str, object]:
        self.calls.append("stop")
        return {"ok": True}

    def set_power_mode(self, mode: str) -> dict[str, object]:
        self.calls.append(mode)
        return {}

    def request_button_wake(self) -> str:
        self.calls.append("listen")
        return "listen"


@pytest.mark.parametrize(
    ("event", "calls"),
    [
        (ButtonEvent("red", "short"), ["stop"]),
        (ButtonEvent("red", "long"), ["stop", "sleep"]),
        (ButtonEvent("green", "short"), ["listen"]),
        (ButtonEvent("green", "long"), ["standby"]),
    ],
)
def test_button_gestures_map_to_runtime_actions(event: ButtonEvent, calls: list[str]) -> None:
    app = Homebody(False)
    runtime = RecordingRuntime()
    app._runtime = runtime  # type: ignore[assignment]

    app._handle_button_event(event)

    assert runtime.calls == calls


def test_red_stop_works_while_the_runtime_is_still_starting_but_other_gestures_wait() -> None:
    app = Homebody(False)
    runtime = RecordingRuntime(ready=False)
    app._runtime = runtime  # type: ignore[assignment]

    app._handle_button_event(ButtonEvent("red", "short"))
    with pytest.raises(RuntimeError, match="still starting"):
        app._handle_button_event(ButtonEvent("green", "short"))

    assert runtime.calls == ["stop"]


def test_red_long_press_still_sleeps_when_part_of_stop_fails() -> None:
    class PartlyBrokenRuntime(RecordingRuntime):
        def physical_stop(self) -> dict[str, object]:
            self.calls.append("stop")
            raise RuntimeError("robot: Robot action controller is not ready")

    app = Homebody(False)
    runtime = PartlyBrokenRuntime()
    app._runtime = runtime  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="not ready"):
        app._handle_button_event(ButtonEvent("red", "long"))

    assert runtime.calls == ["stop", "sleep"]


def test_physical_stop_runs_every_step_even_when_the_robot_controller_is_missing() -> None:
    runtime = make_runtime()
    calls: list[str] = []
    runtime.cancel_contextual_offer = lambda reason: calls.append(f"offer:{reason}") or False  # type: ignore[method-assign]
    runtime.stop_announcements = lambda clear_queue: calls.append("announcements") or {}  # type: ignore[method-assign]
    runtime._accept_wake_turn()

    with pytest.raises(RuntimeError, match="robot: Robot action controller is not ready"):
        runtime.physical_stop()

    assert calls == ["offer:emergency_stop", "announcements"]
    assert runtime._conversation_stop_requested.is_set()


def test_physical_stop_ends_kids_mode() -> None:
    runtime = make_runtime()
    stopped: list[str] = []
    runtime._kids_active = True
    runtime.stop_kids_mode = lambda reason, fold: stopped.append(reason) or {}  # type: ignore[method-assign]
    runtime.stop_manual_robot_action = lambda: {"ok": True}  # type: ignore[method-assign]

    assert runtime.physical_stop() == {"ok": True, "kids_stopped": True}
    assert stopped == ["physical_stop"]


def test_green_press_wakes_from_sleep_instead_of_listening() -> None:
    runtime = make_runtime()
    modes: list[str] = []
    runtime._power_mode = "sleep"
    runtime.set_power_mode = lambda mode: modes.append(mode) or {}  # type: ignore[method-assign]

    assert runtime.request_button_wake() == "awake"
    assert modes == ["awake"]
    assert runtime._take_button_wake() is False


def test_green_press_is_taken_once_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime()

    assert runtime.request_button_wake() == "listen"
    assert runtime._take_button_wake() is True
    assert runtime._take_button_wake() is False

    runtime.request_button_wake()
    later = time.monotonic() + 5.0
    monkeypatch.setattr("homebody.manual_control.time.monotonic", lambda: later)
    assert runtime._take_button_wake() is False


def test_wake_loop_starts_a_turn_from_a_button_press_without_a_wake_word() -> None:
    runtime = make_runtime()
    started: list[str] = []

    class SilentSpotter:
        def accept(self, frame: np.ndarray, rate: int) -> str:
            return ""

        def reset(self) -> None:
            pass

    def run_turn(config: AppConfig, keyword: str) -> None:
        started.append(keyword)
        runtime.stop_event.set()

    runtime._spotter = SilentSpotter()  # type: ignore[assignment]
    runtime.config_loader = lambda: AppConfig(api_key="key", wake_cooldown_seconds=60.0)  # type: ignore[call-arg]
    runtime._read_16k_frame = lambda: np.zeros(320, dtype=np.float32)  # type: ignore[method-assign]
    runtime._set_motor_mode = lambda enabled, wake=False: None  # type: ignore[method-assign]
    runtime._motors_enabled = True
    runtime._status.power_mode = "awake"
    runtime._orient_to_voice = lambda config: None  # type: ignore[method-assign]
    runtime._run_selected_voice_conversation = run_turn  # type: ignore[method-assign]
    # A recent spoken wake must not swallow the deliberate button press.
    runtime._last_wake_at = time.monotonic()
    runtime.request_button_wake()

    worker = threading.Thread(target=runtime._listen_for_wake_word, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert started == ["button"]


class FakeGpioService:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
        self.stops = 0
        self.running = False

    def start(self, *, chip: str, pins: dict[str, int], long_press_seconds: float) -> None:
        self.starts.append({"chip": chip, "pins": dict(pins), "long": long_press_seconds})
        self.running = True

    def stop(self) -> None:
        self.stops += 1
        self.running = False

    close = stop

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.running,
            "available": self.running,
            "pins": {},
            "last_event": "",
            "last_error": "",
            "stuck": [],
        }


def gpio_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeGpioService, list[AppConfig]]:
    app = Homebody(False)
    service = FakeGpioService()
    app._gpio_buttons = service  # type: ignore[assignment]
    app.settings_app.extra["hermes"] = app
    stored = [AppConfig()]
    monkeypatch.setattr(main_module, "load_config", lambda: stored[-1])
    monkeypatch.setattr(main_module, "save_config", lambda config: stored.append(config))
    return TestClient(app.settings_app), service, stored


def test_gpio_route_persists_then_opens_the_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    client, service, stored = gpio_client(monkeypatch)

    initial = client.get("/api/gpio/status").json()
    assert initial["enabled"] is False
    assert initial["configured"] == {
        "enabled": False,
        "chip": "/dev/gpiochip0",
        "green_pin": 17,
        "red_pin": None,
        "long_press_seconds": 2.0,
    }

    enabled = client.post(
        "/api/gpio/buttons",
        json={"enabled": True, "green_pin": 17, "red_pin": 27, "long_press_seconds": 3.0},
    )
    assert enabled.status_code == 200
    assert enabled.json()["available"] is True
    assert stored[-1].gpio_buttons_enabled is True and stored[-1].gpio_red_pin == 27
    assert service.starts == [{"chip": "/dev/gpiochip0", "pins": {"green": 17, "red": 27}, "long": 3.0}]

    disabled = client.post("/api/gpio/buttons", json={"enabled": False, "green_pin": 17, "red_pin": 27})
    assert disabled.status_code == 200
    assert stored[-1].gpio_buttons_enabled is False
    assert service.running is False


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": True, "green_pin": 17, "red_pin": 17},
        {"enabled": True, "red_pin": 1024},
        {"enabled": "yes"},
        {"enabled": True, "long_press_seconds": 0.1},
        {"enabled": True, "chip": "/etc/passwd"},
    ],
)
def test_gpio_route_rejects_invalid_settings_without_touching_lines(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    client, service, stored = gpio_client(monkeypatch)

    response = client.post("/api/gpio/buttons", json=payload)

    assert response.status_code in {400, 422}
    assert len(stored) == 1
    assert service.starts == []


def test_gpio_settings_are_parent_controls_while_kids_mode_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    client, service, stored = gpio_client(monkeypatch)
    app = client.app.extra["hermes"]
    app._runtime = SimpleNamespace(kids_controls_locked=True)

    assert client.post("/api/gpio/buttons", json={"enabled": True}).status_code == 423
    assert client.get("/api/gpio/status").status_code == 423
    assert service.starts == [] and len(stored) == 1


def test_robot_tab_has_the_physical_button_card_wired_to_the_gpio_routes() -> None:
    static = Path(main_module.__file__).parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "main.js").read_text(encoding="utf-8")

    assert '<details class="card gpio-card disclosure-card">' in html
    for element in ("gpio-enabled", "gpio-green-pin", "gpio-red-pin", "gpio-long-press", "gpio-save-button"):
        assert f'id="{element}"' in html
    assert '"/api/gpio/status"' in script and '"/api/gpio/buttons"' in script
    # Status text is rendered with textContent, never as HTML.
    assert "gpio-last-event\").innerHTML" not in script
