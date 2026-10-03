"""Conservative PlayStation controller features beyond the legacy joystick API."""

from __future__ import annotations

import glob
import math
import os
import re
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ActionCallback = Callable[[str, str, str], bool | None]
FeedbackCallback = Callable[[str], None]

_GYRO_AXES = ("ABS_RX", "ABS_RY", "ABS_RZ")
_GYRO_AXIS_ACTION = {"ABS_RX": "pitch", "ABS_RY": "yaw", "ABS_RZ": "roll"}
_GYRO_CALIBRATION_FRAMES = 24
_GYRO_CALIBRATION_SECONDS = 0.25
_GYRO_RESOLUTION_FALLBACK = 1024.0
_GYRO_CALIBRATION_STDDEV_DPS = 8.0
_GYRO_CALIBRATION_MEAN_DPS = 15.0
_GYRO_ACTIVATION_DPS = 25.0
_GYRO_NEUTRAL_DPS = 10.0
_TOUCH_SWIPE_THRESHOLD = 0.30
_TOUCH_AXIS_DOMINANCE = 1.5
_TOUCH_MIN_SECONDS = 0.08
_TOUCH_MAX_SECONDS = 0.70


class ControllerFeatureInterpreter:
    """Translate bounded gyro/touch gestures and isolate optional rumble failures."""

    def __init__(
        self,
        dispatch: ActionCallback,
        feedback: FeedbackCallback,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatch = dispatch
        self._feedback_callback = feedback
        self._monotonic = monotonic
        self._rumble_available = False
        self._gyro_available = False
        self._touchpad_available = False
        self._gyro_armed = False
        self._gyro_active = False
        self._gyro_calibrating = False
        self._gyro_calibration_started_at = 0.0
        self._gyro_resolution = {axis: _GYRO_RESOLUTION_FALLBACK for axis in _GYRO_AXES}
        self._gyro_samples: list[dict[str, float]] = []
        self._gyro_bias = {axis: 0.0 for axis in _GYRO_AXES}
        self._gyro_latch: tuple[str, int] | None = None
        self._touches: dict[int, tuple[float, float, float, float, float]] = {}
        self._touch_multitouch = False
        self._touch_cancelled = False
        self.last_error = ""

    def set_capabilities(self, *, rumble: bool, gyro: bool, touchpad: bool) -> None:
        self._rumble_available = bool(rumble)
        self._gyro_available = bool(gyro)
        self._touchpad_available = bool(touchpad)
        self.last_error = ""

    def set_gyro_resolution(self, resolution: dict[str, float]) -> None:
        for axis in _GYRO_AXES:
            value = float(resolution.get(axis, self._gyro_resolution[axis]))
            if math.isfinite(value) and value > 0:
                self._gyro_resolution[axis] = value

    def status(self) -> dict[str, object]:
        return {
            "rumble_available": self._rumble_available,
            "gyro_available": self._gyro_available,
            "touchpad_available": self._touchpad_available,
            "gyro_active": self._gyro_active,
            "gyro_calibrating": self._gyro_calibrating,
            "feature_input_error": self.last_error,
        }

    def _reset_gyro(self) -> None:
        self._gyro_armed = False
        self._gyro_active = False
        self._gyro_calibrating = False
        self._gyro_calibration_started_at = 0.0
        self._gyro_samples.clear()
        self._gyro_latch = None

    def reset_events(self) -> None:
        self._reset_gyro()
        self._touches.clear()
        self._touch_multitouch = False
        self._touch_cancelled = False

    def reset(self) -> None:
        self._rumble_available = False
        self._gyro_available = False
        self._touchpad_available = False
        self.last_error = ""
        self.reset_events()

    def feedback(self, pattern: str) -> None:
        if not self._rumble_available:
            return
        try:
            self._feedback_callback(pattern)
        except Exception as exc:
            self._rumble_available = False
            self.last_error = str(exc)

    def _emit(self, kind: str, action: str, value: str, *, feedback: bool = True) -> None:
        try:
            if self._dispatch(kind, action, value) is False:
                raise RuntimeError("Controller command was rejected")
        except Exception as exc:
            self.last_error = str(exc)
            self.feedback("rejected")
            return
        self.last_error = ""
        if feedback:
            self.feedback("accepted")

    def handle_l2(self, pressed: bool) -> None:
        pressed = bool(pressed and self._gyro_available)
        if pressed == self._gyro_armed:
            return
        self._gyro_armed = pressed
        self._gyro_active = False
        self._gyro_calibrating = pressed
        if pressed:
            self.last_error = ""
        self._gyro_calibration_started_at = self._monotonic() if pressed else 0.0
        self._gyro_samples.clear()
        self._gyro_latch = None
        self.feedback("gyro_on" if pressed else "gyro_off")

    def handle_motion_frame(self, values: dict[str, int | float]) -> None:
        if not self._gyro_armed:
            return
        frame: dict[str, float] = {}
        for axis in _GYRO_AXES:
            value = float(values.get(axis, math.nan))
            if not math.isfinite(value):
                self.reset_events()
                return
            frame[axis] = value
        if self._gyro_calibrating:
            self._gyro_samples.append(frame)
            if (
                len(self._gyro_samples) < _GYRO_CALIBRATION_FRAMES
                or self._monotonic() - self._gyro_calibration_started_at < _GYRO_CALIBRATION_SECONDS
            ):
                return
            for axis in _GYRO_AXES:
                samples = [sample[axis] for sample in self._gyro_samples]
                mean = statistics.fmean(samples)
                resolution = self._gyro_resolution[axis]
                deviation_dps = statistics.pstdev(samples) / resolution
                mean_dps = abs(mean) / resolution
                if deviation_dps > _GYRO_CALIBRATION_STDDEV_DPS or mean_dps > _GYRO_CALIBRATION_MEAN_DPS:
                    self._gyro_calibrating = False
                    self._gyro_armed = False
                    self.last_error = "Gyro calibration rejected because the controller was moving"
                    self.feedback("rejected")
                    return
                self._gyro_bias[axis] = mean
            self._gyro_calibrating = False
            self._gyro_active = True
            self._gyro_samples.clear()
            return
        if not self._gyro_active:
            return
        rates = {axis: (frame[axis] - self._gyro_bias[axis]) / self._gyro_resolution[axis] for axis in _GYRO_AXES}
        dominant_axis = max(_GYRO_AXES, key=lambda axis: abs(rates[axis]))
        dominant_rate = rates[dominant_axis]
        if max(abs(rate) for rate in rates.values()) <= _GYRO_NEUTRAL_DPS:
            self._gyro_latch = None
            return
        if abs(dominant_rate) < _GYRO_ACTIVATION_DPS:
            return
        direction = -1 if dominant_rate < 0 else 1
        latch = (dominant_axis, direction)
        if self._gyro_latch is not None:
            return
        self._gyro_latch = latch
        delta = 2.5 if direction < 0 else -2.5
        self._emit("precision", _GYRO_AXIS_ACTION[dominant_axis], str(delta), feedback=False)

    @staticmethod
    def _finite_unit(value: float) -> float | None:
        value = float(value)
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            return None
        return value

    def touch_click(self) -> None:
        if not self._touchpad_available:
            return
        self._touch_cancelled = bool(self._touches)
        self._emit("precision", "center_all", "0.0")

    def touch_down(self, slot: int, x: float, y: float) -> None:
        if not self._touchpad_available:
            return
        x_value = self._finite_unit(x)
        y_value = self._finite_unit(y)
        if x_value is None or y_value is None:
            return
        self._touches[int(slot)] = (x_value, y_value, x_value, y_value, self._monotonic())
        if len(self._touches) > 1:
            self._touch_multitouch = True
            self._touch_cancelled = True

    def touch_move(self, slot: int, x: float, y: float) -> None:
        slot = int(slot)
        touch = self._touches.get(slot)
        if touch is None:
            return
        x_value = self._finite_unit(x)
        y_value = self._finite_unit(y)
        if x_value is None or y_value is None:
            return
        self._touches[slot] = (touch[0], touch[1], x_value, y_value, touch[4])

    def touch_up(self, slot: int) -> None:
        touch = self._touches.pop(int(slot), None)
        if touch is None:
            return
        if self._touch_cancelled or self._touch_multitouch:
            if not self._touches:
                self._touch_multitouch = False
                self._touch_cancelled = False
            return
        start_x, start_y, end_x, end_y, started_at = touch
        duration = self._monotonic() - started_at
        if duration < _TOUCH_MIN_SECONDS or duration > _TOUCH_MAX_SECONDS:
            return
        delta_x = end_x - start_x
        delta_y = end_y - start_y
        abs_x = abs(delta_x)
        abs_y = abs(delta_y)
        if abs_x >= _TOUCH_SWIPE_THRESHOLD and abs_x >= abs_y * _TOUCH_AXIS_DOMINANCE:
            self._emit("precision", "body_yaw", "5.0" if delta_x < 0 else "-5.0")
        elif abs_y >= _TOUCH_SWIPE_THRESHOLD and abs_y >= abs_x * _TOUCH_AXIS_DOMINANCE:
            self._emit("action", "look", "up" if delta_y < 0 else "down")


class PlayStationEvdevFeatures:
    """Discover and poll same-controller evdev nodes for rumble, IMU and touch."""

    _RUMBLE_PATTERNS = {
        "connected": (0x0000, 0x2000, 70),
        "accepted": (0x0000, 0x1800, 60),
        "gyro_on": (0x0400, 0x2000, 55),
        "gyro_off": (0x0000, 0x1000, 40),
        "stop": (0x0800, 0x2000, 90),
        "rejected": (0x0800, 0x1000, 110),
    }

    def __init__(
        self,
        interpreter: ControllerFeatureInterpreter,
        *,
        gamepad: Any | None = None,
        sensors: Any | None = None,
        touchpad: Any | None = None,
        evdev_module: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        discovery_complete: bool = True,
    ) -> None:
        if evdev_module is None:
            import evdev as evdev_module  # type: ignore[no-redef]

        self._evdev = evdev_module
        self._interpreter = interpreter
        self._gamepad = gamepad
        self._sensors = sensors
        self._touchpad = touchpad
        self._monotonic = monotonic
        self._discovery_complete = bool(discovery_complete)
        self._effect_id: int | None = None
        self._effect_expires_at = 0.0
        self._last_feedback_at = float("-inf")
        self._touch_slot = 0
        self._touch_points: dict[int, dict[str, float | int | bool | None]] = {}
        self._motion_frame: dict[str, float] = {}
        self._gamepad_resync = False
        self._sensor_resync = False
        self._touch_resync = False
        self._touch_x_max = self._axis_max(touchpad, evdev_module.ecodes.ABS_MT_POSITION_X)
        self._touch_y_max = self._axis_max(touchpad, evdev_module.ecodes.ABS_MT_POSITION_Y)
        rumble_available = gamepad is not None and self._supports_rumble(gamepad)
        interpreter.set_capabilities(
            rumble=rumble_available,
            gyro=sensors is not None,
            touchpad=touchpad is not None,
        )
        if sensors is not None:
            interpreter.set_gyro_resolution(
                {
                    "ABS_RX": self._axis_resolution(sensors, evdev_module.ecodes.ABS_RX),
                    "ABS_RY": self._axis_resolution(sensors, evdev_module.ecodes.ABS_RY),
                    "ABS_RZ": self._axis_resolution(sensors, evdev_module.ecodes.ABS_RZ),
                }
            )

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError:
            return ""

    @staticmethod
    def _hid_parent(path: Path) -> Path | None:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        for candidate in (resolved, *resolved.parents):
            if re.fullmatch(r"0005:054[Cc]:09[Cc][Cc]\.[0-9A-Fa-f]+", candidate.name):
                return candidate
        return None

    @classmethod
    def discover(
        cls,
        joystick_path: str,
        interpreter: ControllerFeatureInterpreter,
        *,
        evdev_module: Any | None = None,
        event_glob: Callable[[], list[str]] | None = None,
        sys_class_input: Path = Path("/sys/class/input"),
    ) -> PlayStationEvdevFeatures:
        if evdev_module is None:
            import evdev as evdev_module  # type: ignore[no-redef]

        js_device = sys_class_input / Path(joystick_path).name / "device"
        hid_parent = cls._hid_parent(js_device)
        if hid_parent is None:
            return cls(interpreter, evdev_module=evdev_module)
        vendor_text = cls._read_text(js_device / "id" / "vendor")
        product_text = cls._read_text(js_device / "id" / "product")
        uniq = cls._read_text(js_device / "uniq").lower()
        try:
            vendor = int(vendor_text, 16)
            product = int(product_text, 16)
        except ValueError:
            return cls(interpreter, evdev_module=evdev_module)
        if vendor != 0x054C or product != 0x09CC:
            return cls(interpreter, evdev_module=evdev_module)
        paths = (event_glob or (lambda: sorted(glob.glob("/dev/input/event*"))))()
        opened: list[Any] = []
        gamepad = sensors = touchpad = None
        try:
            for path in paths:
                event_hid_parent = cls._hid_parent(sys_class_input / Path(path).name / "device")
                if event_hid_parent != hid_parent:
                    continue
                try:
                    device = evdev_module.InputDevice(path)
                except PermissionError as exc:
                    raise RuntimeError(
                        f"Cannot open {path} for controller features; add the app user to the input group"
                    ) from exc
                except OSError:
                    continue
                opened.append(device)
                if (
                    int(device.info.bustype) != 0x0005
                    or int(device.info.vendor) != vendor
                    or int(device.info.product) != product
                ):
                    continue
                if uniq and str(device.uniq).lower() != uniq:
                    continue
                capabilities = device.capabilities(absinfo=False)
                name = " ".join(str(device.name).lower().split())
                if (
                    evdev_module.ecodes.EV_KEY in capabilities
                    and evdev_module.ecodes.BTN_TL2 in capabilities[evdev_module.ecodes.EV_KEY]
                    and "controller" in name
                ):
                    gamepad = device
                elif (
                    "motion sensors" in name
                    and evdev_module.ecodes.EV_ABS in capabilities
                    and {
                        evdev_module.ecodes.ABS_RX,
                        evdev_module.ecodes.ABS_RY,
                        evdev_module.ecodes.ABS_RZ,
                    }.issubset(capabilities[evdev_module.ecodes.EV_ABS])
                ):
                    sensors = device
                elif (
                    "touchpad" in name
                    and evdev_module.ecodes.EV_KEY in capabilities
                    and evdev_module.ecodes.BTN_LEFT in capabilities[evdev_module.ecodes.EV_KEY]
                    and evdev_module.ecodes.EV_ABS in capabilities
                    and {
                        evdev_module.ecodes.ABS_MT_SLOT,
                        evdev_module.ecodes.ABS_MT_TRACKING_ID,
                        evdev_module.ecodes.ABS_MT_POSITION_X,
                        evdev_module.ecodes.ABS_MT_POSITION_Y,
                    }.issubset(capabilities[evdev_module.ecodes.EV_ABS])
                ):
                    touchpad = device
            selected = {id(item) for item in (gamepad, sensors, touchpad) if item is not None}
            for device in opened:
                if id(device) not in selected:
                    device.close()
            return cls(
                interpreter,
                gamepad=gamepad,
                sensors=sensors,
                touchpad=touchpad,
                evdev_module=evdev_module,
                discovery_complete=all(item is not None for item in (gamepad, sensors, touchpad)),
            )
        except Exception:
            for device in opened:
                try:
                    device.close()
                except OSError:
                    pass
            raise

    @staticmethod
    def _axis_max(device: Any | None, code: int) -> float:
        if device is None:
            return 1.0
        try:
            info = device.absinfo(code)
            maximum = float(info.max)
            return maximum if maximum > 0 else 1.0
        except (OSError, AttributeError, ValueError):
            return 1.0

    @staticmethod
    def _axis_resolution(device: Any, code: int) -> float:
        try:
            resolution = float(device.absinfo(code).resolution)
            return resolution if resolution > 0 and math.isfinite(resolution) else _GYRO_RESOLUTION_FALLBACK
        except (OSError, AttributeError, ValueError):
            return _GYRO_RESOLUTION_FALLBACK

    def _supports_rumble(self, device: Any) -> bool:
        try:
            capabilities = device.capabilities(absinfo=False)
            access_mode = os.O_RDONLY
            try:
                import fcntl

                access_mode = fcntl.fcntl(device.fd, fcntl.F_GETFL) & os.O_ACCMODE
            except (OSError, AttributeError, ImportError):
                pass
            return (
                self._evdev.ecodes.EV_FF in capabilities
                and self._evdev.ecodes.FF_RUMBLE in capabilities[self._evdev.ecodes.EV_FF]
                and int(getattr(device, "ff_effects_count", 0)) > 0
                and access_mode != os.O_RDONLY
            )
        except OSError:
            return False

    def discovery_complete(self) -> bool:
        return self._discovery_complete

    def status(self) -> dict[str, object]:
        return self._interpreter.status()

    def handle_l2(self, pressed: bool) -> None:
        self._interpreter.handle_l2(pressed)

    def play_feedback(self, pattern: str) -> None:
        self._interpreter.feedback(pattern)

    def rumble(self, pattern: str) -> None:
        if self._gamepad is None:
            raise RuntimeError("Controller rumble is unavailable")
        now = self._monotonic()
        if now - self._last_feedback_at < 0.25:
            return
        strong, weak, duration_ms = self._RUMBLE_PATTERNS.get(pattern, self._RUMBLE_PATTERNS["accepted"])
        self._erase_effect()
        ff = self._evdev.ff
        effect = ff.Effect(
            self._evdev.ecodes.FF_RUMBLE,
            -1,
            0,
            ff.Trigger(0, 0),
            ff.Replay(duration_ms, 0),
            ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=strong, weak_magnitude=weak)),
        )
        self._effect_id = int(self._gamepad.upload_effect(effect))
        self._gamepad.write(self._evdev.ecodes.EV_FF, self._effect_id, 1)
        self._last_feedback_at = now
        self._effect_expires_at = now + (duration_ms / 1000.0) + 0.05

    def _erase_effect(self) -> None:
        effect_id = self._effect_id
        self._effect_id = None
        self._effect_expires_at = 0.0
        if effect_id is not None and self._gamepad is not None:
            try:
                self._gamepad.write(self._evdev.ecodes.EV_FF, effect_id, 0)
            except OSError:
                pass
            try:
                self._gamepad.erase_effect(effect_id)
            except OSError:
                pass

    @staticmethod
    def _read_device_events(device: Any | None) -> tuple[bool, list[Any]]:
        if device is None:
            return True, []
        try:
            return True, list(device.read())
        except BlockingIOError:
            return True, []
        except OSError:
            return False, []

    def poll(self) -> bool:
        if self._effect_id is not None and self._monotonic() >= self._effect_expires_at:
            self._erase_effect()
        batches: list[tuple[Callable[[Any], None], list[Any]]] = []
        for device, handler in (
            (self._gamepad, self._handle_gamepad_event),
            (self._sensors, self._handle_sensor_event),
            (self._touchpad, self._handle_touch_event),
        ):
            healthy, events = self._read_device_events(device)
            if not healthy:
                self._interpreter.reset_events()
                return False
            batches.append((handler, events))
        for handler, events in batches:
            for event in events:
                handler(event)
        return True

    def _handle_gamepad_event(self, event: Any) -> None:
        codes = self._evdev.ecodes
        if event.type == codes.EV_SYN and event.code == codes.SYN_DROPPED:
            self._interpreter.reset_events()
            self._gamepad_resync = True
            return
        if self._gamepad_resync:
            if event.type == codes.EV_SYN and event.code == codes.SYN_REPORT:
                self._gamepad_resync = False
            return
        if event.type == codes.EV_KEY and event.code == codes.BTN_TL2 and event.value in {0, 1}:
            self._interpreter.handle_l2(event.value == 1)

    def _handle_sensor_event(self, event: Any) -> None:
        codes = self._evdev.ecodes
        if event.type == codes.EV_SYN and event.code == codes.SYN_DROPPED:
            self._motion_frame.clear()
            self._interpreter.reset_events()
            self._sensor_resync = True
            return
        if self._sensor_resync:
            if event.type == codes.EV_SYN and event.code == codes.SYN_REPORT:
                self._sensor_resync = False
            return
        if event.type == codes.EV_SYN:
            if event.code == codes.SYN_REPORT and all(axis in self._motion_frame for axis in _GYRO_AXES):
                self._interpreter.handle_motion_frame(dict(self._motion_frame))
            return
        if event.type != codes.EV_ABS:
            return
        names = {
            codes.ABS_RX: "ABS_RX",
            codes.ABS_RY: "ABS_RY",
            codes.ABS_RZ: "ABS_RZ",
        }
        name = names.get(event.code)
        if name:
            self._motion_frame[name] = float(event.value)

    def _touch_point(self) -> dict[str, float | int | bool | None]:
        return self._touch_points.setdefault(
            self._touch_slot,
            {"tracking": False, "started": False, "x": None, "y": None},
        )

    def _commit_touch_point(self, slot: int, point: dict[str, float | int | bool | None]) -> None:
        if not point["tracking"] or point["x"] is None or point["y"] is None:
            return
        x = max(0.0, min(1.0, float(point["x"]) / self._touch_x_max))
        y = max(0.0, min(1.0, float(point["y"]) / self._touch_y_max))
        if point["started"]:
            self._interpreter.touch_move(slot, x, y)
        else:
            self._interpreter.touch_down(slot, x, y)
            point["started"] = True

    def _handle_touch_event(self, event: Any) -> None:
        codes = self._evdev.ecodes
        if event.type == codes.EV_SYN and event.code == codes.SYN_DROPPED:
            self._touch_points.clear()
            self._interpreter.reset_events()
            self._touch_resync = True
            return
        if self._touch_resync:
            if event.type == codes.EV_SYN and event.code == codes.SYN_REPORT:
                self._touch_resync = False
            return
        if event.type == codes.EV_SYN:
            return
        if event.type == codes.EV_KEY and event.code == codes.BTN_LEFT and event.value == 1:
            self._interpreter.touch_click()
            return
        if event.type != codes.EV_ABS:
            return
        if event.code == codes.ABS_MT_SLOT:
            self._touch_slot = max(0, int(event.value))
            return
        point = self._touch_point()
        if event.code == codes.ABS_MT_TRACKING_ID:
            if event.value < 0:
                if point["started"]:
                    self._interpreter.touch_up(self._touch_slot)
                self._touch_points.pop(self._touch_slot, None)
            else:
                point.update(tracking=True, started=False, x=None, y=None)
            return
        if event.code == codes.ABS_MT_POSITION_X:
            point["x"] = float(event.value)
        elif event.code == codes.ABS_MT_POSITION_Y:
            point["y"] = float(event.value)
        else:
            return
        self._commit_touch_point(self._touch_slot, point)

    def close(self) -> None:
        self._erase_effect()
        for device in (self._gamepad, self._sensors, self._touchpad):
            if device is not None:
                try:
                    device.close()
                except OSError:
                    pass
        self._gamepad = self._sensors = self._touchpad = None
        self._touch_points.clear()
        self._interpreter.reset()
