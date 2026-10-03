from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import evdev

from homebody.controller_features import ControllerFeatureInterpreter, PlayStationEvdevFeatures


def test_gyro_requires_l2_calibration_and_emits_bounded_debounced_head_nudges() -> None:
    actions: list[tuple[str, str, str]] = []
    feedback: list[str] = []
    clock = [10.0]
    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        feedback.append,
        monotonic=lambda: clock[0],
    )
    controller.set_capabilities(rumble=True, gyro=True, touchpad=False)

    controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": -30_000, "ABS_RZ": 0})
    assert actions == []

    controller.handle_l2(True)
    assert controller.status()["gyro_calibrating"] is True
    for _ in range(24):
        clock[0] += 0.005
        controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    assert controller.status()["gyro_calibrating"] is True
    clock[0] += 0.14
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    assert controller.status()["gyro_active"] is True

    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -30_770, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -35_000, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": 30_670, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 30_820, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": -30_620, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 25})
    controller.handle_motion_frame({"ABS_RX": 100, "ABS_RY": -50, "ABS_RZ": 30_745})

    assert actions == [
        ("precision", "yaw", "2.5"),
        ("precision", "yaw", "-2.5"),
        ("precision", "pitch", "-2.5"),
        ("precision", "pitch", "2.5"),
        ("precision", "roll", "-2.5"),
    ]
    assert "gyro_on" in feedback

    controller.handle_l2(False)
    controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": -30_000, "ABS_RZ": 0})
    assert len(actions) == 5
    assert controller.status()["gyro_active"] is False


def test_touchpad_click_centers_all_and_single_finger_swipes_are_bounded() -> None:
    actions: list[tuple[str, str, str]] = []
    feedback: list[str] = []
    clock = [20.0]
    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        feedback.append,
        monotonic=lambda: clock[0],
    )
    controller.set_capabilities(rumble=True, gyro=False, touchpad=True)

    controller.touch_click()
    controller.touch_down(0, 0.85, 0.50)
    controller.touch_move(0, 0.25, 0.50)
    clock[0] += 0.2
    controller.touch_up(0)
    controller.touch_down(0, 0.20, 0.50)
    controller.touch_move(0, 0.80, 0.50)
    clock[0] += 0.2
    controller.touch_up(0)
    controller.touch_down(0, 0.50, 0.80)
    controller.touch_move(0, 0.50, 0.20)
    clock[0] += 0.2
    controller.touch_up(0)
    controller.touch_down(0, 0.50, 0.20)
    controller.touch_move(0, 0.50, 0.80)
    clock[0] += 0.2
    controller.touch_up(0)

    assert actions == [
        ("precision", "center_all", "0.0"),
        ("precision", "body_yaw", "5.0"),
        ("precision", "body_yaw", "-5.0"),
        ("action", "look", "up"),
        ("action", "look", "down"),
    ]
    assert feedback == ["accepted"] * 5


def test_touchpad_rejects_short_and_multitouch_gestures_and_resets_cleanly() -> None:
    actions: list[tuple[str, str, str]] = []
    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda _pattern: None,
    )
    controller.set_capabilities(rumble=False, gyro=True, touchpad=True)

    controller.touch_down(0, 0.50, 0.50)
    controller.touch_move(0, 0.55, 0.52)
    controller.touch_up(0)
    controller.touch_down(0, 0.90, 0.50)
    controller.touch_down(1, 0.10, 0.50)
    controller.touch_move(0, 0.10, 0.50)
    controller.touch_up(0)
    controller.touch_up(1)
    assert actions == []

    controller.handle_l2(True)
    controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": -30_000, "ABS_RZ": 0})
    controller.reset()
    controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": 30_000, "ABS_RZ": 0})
    assert actions == []
    assert controller.status() == {
        "rumble_available": False,
        "gyro_available": False,
        "touchpad_available": False,
        "gyro_active": False,
        "gyro_calibrating": False,
        "feature_input_error": "",
    }


def test_noisy_gyro_calibration_fails_closed_and_cannot_move_the_base() -> None:
    actions: list[tuple[str, str, str]] = []
    clock = [30.0]
    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda _pattern: None,
        monotonic=lambda: clock[0],
    )
    controller.set_capabilities(rumble=False, gyro=True, touchpad=False)
    controller.handle_l2(True)
    for index in range(26):
        clock[0] += 0.01
        value = 30_000 if index % 2 else -30_000
        controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": value, "ABS_RZ": 0})

    assert actions == []
    assert controller.status()["gyro_active"] is False
    assert controller.status()["gyro_calibrating"] is False
    assert "moving" in controller.last_error


def test_constant_rotation_is_not_absorbed_as_gyro_bias() -> None:
    clock = [35.0]
    controller = ControllerFeatureInterpreter(
        lambda *_args: None,
        lambda _pattern: None,
        monotonic=lambda: clock[0],
    )
    controller.set_capabilities(rumble=False, gyro=True, touchpad=False)
    controller.handle_l2(True)
    for _ in range(26):
        clock[0] += 0.01
        controller.handle_motion_frame({"ABS_RX": 0, "ABS_RY": 30_000, "ABS_RZ": 0})

    assert controller.status()["gyro_active"] is False
    assert "moving" in controller.last_error


def test_touchpad_click_cancels_a_click_drag_swipe() -> None:
    actions: list[tuple[str, str, str]] = []
    clock = [40.0]
    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda _pattern: None,
        monotonic=lambda: clock[0],
    )
    controller.set_capabilities(rumble=False, gyro=False, touchpad=True)
    controller.touch_down(0, 0.9, 0.5)
    controller.touch_click()
    controller.touch_move(0, 0.1, 0.5)
    clock[0] += 0.2
    controller.touch_up(0)

    assert actions == [("precision", "center_all", "0.0")]


def test_capability_status_and_rumble_failures_are_fail_closed() -> None:
    actions: list[tuple[str, str, str]] = []

    def broken_feedback(_pattern: str) -> None:
        raise OSError("write denied")

    controller = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        broken_feedback,
    )
    controller.set_capabilities(rumble=True, gyro=True, touchpad=True)
    controller.touch_click()

    assert actions == [("precision", "center_all", "0.0")]
    assert controller.status() == {
        "rumble_available": False,
        "gyro_available": True,
        "touchpad_available": True,
        "gyro_active": False,
        "gyro_calibrating": False,
        "feature_input_error": "write denied",
    }
    assert "write denied" in controller.last_error


class FakeDevice:
    def __init__(self, capabilities: dict[int, list[int]], *, maxima: dict[int, int] | None = None) -> None:
        self.fd = os.open("/dev/null", os.O_RDWR)
        self.ff_effects_count = 16
        self._capabilities = capabilities
        self._maxima = maxima or {}
        self.events: list[SimpleNamespace] = []
        self.uploaded: list[object] = []
        self.writes: list[tuple[int, int, int]] = []
        self.erased: list[int] = []
        self.closed = False

    def capabilities(self, *, absinfo: bool = True) -> dict[int, list[int]]:
        return self._capabilities

    def absinfo(self, code: int) -> SimpleNamespace:
        return SimpleNamespace(max=self._maxima.get(code, 1), resolution=1_024)

    def read(self) -> list[SimpleNamespace]:
        events, self.events = self.events, []
        return events

    def upload_effect(self, effect: object) -> int:
        self.uploaded.append(effect)
        return 7

    def write(self, event_type: int, code: int, value: int) -> None:
        self.writes.append((event_type, code, value))

    def erase_effect(self, effect_id: int) -> None:
        self.erased.append(effect_id)

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
            self.closed = True


def test_discovery_requires_exact_ds4_hid_parent_and_detects_capabilities(tmp_path: Path) -> None:
    codes = evdev.ecodes
    sys_input = tmp_path / "sys-class-input"
    hid = tmp_path / "devices" / "0005:054C:09CC.0001"
    other_hid = tmp_path / "devices" / "0005:054C:09CC.0002"

    def link_input(class_name: str, parent: Path, input_name: str) -> Path:
        device = parent / "input" / input_name
        device.mkdir(parents=True)
        class_dir = sys_input / class_name
        class_dir.mkdir(parents=True)
        (class_dir / "device").symlink_to(device)
        return device

    js_device = link_input("js0", hid, "input0")
    (js_device / "id").mkdir()
    (js_device / "id" / "vendor").write_text("054c\n", encoding="ascii")
    (js_device / "id" / "product").write_text("09cc\n", encoding="ascii")
    (js_device / "uniq").write_text("a4:ae:12:a6:9f:be\n", encoding="ascii")
    link_input("event0", hid, "input1")
    link_input("event1", hid, "input2")
    link_input("event2", hid, "input3")
    link_input("event9", other_hid, "input9")

    capabilities = {
        "event0": {codes.EV_KEY: [codes.BTN_TL2], codes.EV_FF: [codes.FF_RUMBLE]},
        "event1": {codes.EV_ABS: [codes.ABS_RX, codes.ABS_RY, codes.ABS_RZ]},
        "event2": {
            codes.EV_KEY: [codes.BTN_LEFT],
            codes.EV_ABS: [
                codes.ABS_MT_SLOT,
                codes.ABS_MT_TRACKING_ID,
                codes.ABS_MT_POSITION_X,
                codes.ABS_MT_POSITION_Y,
            ],
        },
        "event9": {codes.EV_KEY: [codes.BTN_TL2], codes.EV_FF: [codes.FF_RUMBLE]},
    }
    names = {
        "event0": "Wireless Controller",
        "event1": "Wireless Controller Motion Sensors",
        "event2": "Wireless Controller Touchpad",
        "event9": "Wireless Controller",
    }
    opened: list[str] = []

    def input_device(path: str) -> FakeDevice:
        event_name = Path(path).name
        opened.append(event_name)
        device = FakeDevice(
            capabilities[event_name],
            maxima={codes.ABS_MT_POSITION_X: 1_920, codes.ABS_MT_POSITION_Y: 942},
        )
        device.name = names[event_name]  # type: ignore[attr-defined]
        device.uniq = "a4:ae:12:a6:9f:be"  # type: ignore[attr-defined]
        device.info = SimpleNamespace(bustype=0x0005, vendor=0x054C, product=0x09CC)  # type: ignore[attr-defined]
        return device

    evdev_module = SimpleNamespace(ecodes=codes, ff=evdev.ff, InputDevice=input_device)
    interpreter = ControllerFeatureInterpreter(lambda *_args: None, lambda _pattern: None)
    features = PlayStationEvdevFeatures.discover(
        "/dev/input/js0",
        interpreter,
        evdev_module=evdev_module,
        event_glob=lambda: [f"/dev/input/event{number}" for number in (0, 1, 2, 9)],
        sys_class_input=sys_input,
    )

    assert opened == ["event0", "event1", "event2"]
    assert features.status() == {
        "rumble_available": True,
        "gyro_available": True,
        "touchpad_available": True,
        "gyro_active": False,
        "gyro_calibrating": False,
        "feature_input_error": "",
    }
    features.close()


def test_rumble_cleanup_erases_effect_even_if_stop_write_fails() -> None:
    codes = evdev.ecodes

    class StopFailDevice(FakeDevice):
        def write(self, event_type: int, code: int, value: int) -> None:
            if event_type == codes.EV_FF and value == 0:
                raise OSError("controller disconnected while stopping effect")
            super().write(event_type, code, value)

    gamepad = StopFailDevice({codes.EV_KEY: [codes.BTN_TL2], codes.EV_FF: [codes.FF_RUMBLE]})
    holder: dict[str, PlayStationEvdevFeatures] = {}
    interpreter = ControllerFeatureInterpreter(
        lambda *_args: None,
        lambda pattern: holder["features"].rumble(pattern),
    )
    features = PlayStationEvdevFeatures(interpreter, gamepad=gamepad, evdev_module=evdev)
    holder["features"] = features
    interpreter.feedback("accepted")
    features.close()

    assert gamepad.erased == [7]


def test_syn_dropped_discards_all_events_through_recovery_report() -> None:
    codes = evdev.ecodes
    actions: list[tuple[str, str, str]] = []
    gamepad = FakeDevice({codes.EV_KEY: [codes.BTN_TL2]})
    sensors = FakeDevice({codes.EV_ABS: [codes.ABS_RX, codes.ABS_RY, codes.ABS_RZ]})
    touchpad = FakeDevice(
        {
            codes.EV_KEY: [codes.BTN_LEFT],
            codes.EV_ABS: [
                codes.ABS_MT_SLOT,
                codes.ABS_MT_TRACKING_ID,
                codes.ABS_MT_POSITION_X,
                codes.ABS_MT_POSITION_Y,
            ],
        }
    )
    interpreter = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda _pattern: None,
    )
    features = PlayStationEvdevFeatures(
        interpreter,
        gamepad=gamepad,
        sensors=sensors,
        touchpad=touchpad,
        evdev_module=evdev,
    )
    gamepad.events = [
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_DROPPED, value=0),
        SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_TL2, value=1),
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_REPORT, value=0),
    ]
    sensors.events = [
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_DROPPED, value=0),
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RX, value=30_000),
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RY, value=30_000),
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RZ, value=30_000),
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_REPORT, value=0),
    ]
    touchpad.events = [
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_DROPPED, value=0),
        SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_LEFT, value=1),
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_REPORT, value=0),
    ]

    assert features.poll() is True
    assert actions == []
    assert interpreter.status()["gyro_calibrating"] is False

    touchpad.events = [SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_LEFT, value=1)]
    assert features.poll() is True
    assert actions == [("precision", "center_all", "0.0")]
    features.close()


def test_node_failure_discards_sibling_events_from_same_poll_cycle() -> None:
    codes = evdev.ecodes
    actions: list[tuple[str, str, str]] = []

    class FailedDevice(FakeDevice):
        def read(self) -> list[SimpleNamespace]:
            raise OSError("device disconnected")

    gamepad = FakeDevice({codes.EV_KEY: [codes.BTN_TL2]})
    sensors = FailedDevice({codes.EV_ABS: [codes.ABS_RX, codes.ABS_RY, codes.ABS_RZ]})
    touchpad = FakeDevice({codes.EV_KEY: [codes.BTN_LEFT]})
    interpreter = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda _pattern: None,
    )
    features = PlayStationEvdevFeatures(
        interpreter,
        gamepad=gamepad,
        sensors=sensors,
        touchpad=touchpad,
        evdev_module=evdev,
    )
    gamepad.events = [SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_TL2, value=1)]
    touchpad.events = [SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_LEFT, value=1)]

    assert features.poll() is False
    assert actions == []
    assert interpreter.status()["gyro_calibrating"] is False
    features.close()


def test_evdev_adapter_polls_motion_touch_and_cleans_rumble_effects() -> None:
    codes = evdev.ecodes
    actions: list[tuple[str, str, str]] = []
    clock = [10.0]
    gamepad = FakeDevice(
        {
            codes.EV_KEY: [codes.BTN_TL2],
            codes.EV_FF: [codes.FF_RUMBLE],
        }
    )
    sensors = FakeDevice({codes.EV_ABS: [codes.ABS_RX, codes.ABS_RY, codes.ABS_RZ]})
    touchpad = FakeDevice(
        {
            codes.EV_KEY: [codes.BTN_LEFT],
            codes.EV_ABS: [
                codes.ABS_MT_SLOT,
                codes.ABS_MT_TRACKING_ID,
                codes.ABS_MT_POSITION_X,
                codes.ABS_MT_POSITION_Y,
            ],
        },
        maxima={codes.ABS_MT_POSITION_X: 1_920, codes.ABS_MT_POSITION_Y: 942},
    )
    holder: dict[str, PlayStationEvdevFeatures] = {}
    interpreter = ControllerFeatureInterpreter(
        lambda kind, action, value: actions.append((kind, action, value)),
        lambda pattern: holder["features"].rumble(pattern),
    )
    features = PlayStationEvdevFeatures(
        interpreter,
        gamepad=gamepad,
        sensors=sensors,
        touchpad=touchpad,
        evdev_module=evdev,
        monotonic=lambda: clock[0],
    )
    holder["features"] = features

    gamepad.events = [SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_TL2, value=1)]
    sensors.events = [
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RX, value=0),
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RY, value=0),
        SimpleNamespace(type=codes.EV_ABS, code=codes.ABS_RZ, value=0),
        SimpleNamespace(type=codes.EV_SYN, code=codes.SYN_REPORT, value=0),
    ]
    touchpad.events = [SimpleNamespace(type=codes.EV_KEY, code=codes.BTN_LEFT, value=1)]

    assert features.poll() is True
    assert actions == [("precision", "center_all", "0.0")]
    assert interpreter.status()["gyro_calibrating"] is True
    assert gamepad.uploaded
    assert gamepad.writes[-1] == (codes.EV_FF, 7, 1)

    clock[0] = 11.0
    assert features.poll() is True
    assert gamepad.erased[-1] == 7
    features.close()
    assert gamepad.closed and sensors.closed and touchpad.closed
    assert interpreter.status()["gyro_active"] is False
