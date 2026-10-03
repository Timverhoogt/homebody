"""BlueZ pairing and Linux joystick support for safe local gamepad control."""

from __future__ import annotations

import glob
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .controller_features import ControllerFeatureInterpreter, PlayStationEvdevFeatures

_DEVICE_RE = re.compile(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.+)$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_JS_EVENT = struct.Struct("IhBB")
_JS_EVENT_BUTTON = 0x01
_JS_EVENT_AXIS = 0x02
_JS_EVENT_INIT = 0x80
_STICK_THRESHOLD = 20_000
_SONY_USB_VENDOR_ID = "054c"
_SUPPORTED_EXACT_NAMES = {"wireless controller"}
_SUPPORTED_NAME_PARTS = (
    "dualshock 4",
    "dualsense",
    "sony computer entertainment wireless controller",
    "sony interactive entertainment wireless controller",
)


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str], str | None, float], CommandResult]
ActionCallback = Callable[[str, str, str], bool | None]


class ControllerFeatures(Protocol):
    def discovery_complete(self) -> bool: ...

    def status(self) -> dict[str, object]: ...

    def handle_l2(self, pressed: bool) -> None: ...

    def play_feedback(self, pattern: str) -> None: ...

    def poll(self) -> bool: ...

    def close(self) -> None: ...


FeatureFactory = Callable[[str, ActionCallback], ControllerFeatures]


@dataclass(frozen=True, slots=True)
class BluetoothDevice:
    address: str
    name: str
    paired: bool = False
    bonded: bool = False
    connected: bool = False



def _default_command_runner(command: list[str], input_text: str | None, timeout: float) -> CommandResult:
    return subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _default_feature_factory(joystick_path: str, dispatch: ActionCallback) -> ControllerFeatures:
    holder: dict[str, PlayStationEvdevFeatures] = {}
    interpreter = ControllerFeatureInterpreter(dispatch, lambda pattern: holder["features"].rumble(pattern))
    features = PlayStationEvdevFeatures.discover(joystick_path, interpreter)
    holder["features"] = features
    return features


def _clean_output(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "")


def _strip_ansi(output: str) -> str:
    return _clean_output(output)


def _parse_devices(output: str) -> dict[str, str]:
    devices: dict[str, str] = {}
    for raw_line in _clean_output(output).splitlines():
        match = _DEVICE_RE.match(raw_line.strip())
        if match:
            devices[match.group(1).upper()] = match.group(2).strip()[:120]
    return devices


def _validate_address(address: str) -> str:
    address = address.strip().upper()
    if _MAC_RE.fullmatch(address) is None:
        raise ValueError("Bluetooth address must be a colon-separated MAC address")
    return address


class BluetoothGamepadService:
    """Manage BlueZ devices and translate one local joystick into allow-listed actions."""

    def __init__(
        self,
        action_callback: ActionCallback,
        *,
        command_runner: CommandRunner = _default_command_runner,
        joystick_glob: Callable[[], list[str]] | None = None,
        feature_factory: FeatureFactory = _default_feature_factory,
        adapter_available: bool | None = None,
    ) -> None:
        self._action_callback = action_callback
        self._command_runner = command_runner
        self._joystick_glob = joystick_glob or (lambda: sorted(glob.glob("/dev/input/js*")))
        self._feature_factory = feature_factory
        self._lock = threading.RLock()
        self._bluez_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._controller_transition_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._devices: dict[str, BluetoothDevice] = {}
        self._adapter_available = (
            shutil.which("bluetoothctl") is not None if adapter_available is None else adapter_available
        )
        self._adapter_powered = False
        self._last_error = ""
        self._scan_active = False
        self._gamepad_enabled = False
        self._gamepad_connected = False
        self._gamepad_name = ""
        self._gamepad_path = ""
        self._last_gamepad_action = ""
        self._gamepad_stop = threading.Event()
        self._gamepad_thread: threading.Thread | None = None
        self._controller_features: ControllerFeatures | None = None
        self._feature_error = ""
        self._dispatch_suspended = True
        self._axes: dict[int, int] = {}
        self._last_direction = ""
        self._last_base_direction = ""

    def _run(self, args: list[str], *, input_text: str | None = None, timeout: float = 12.0) -> str:
        if not self._adapter_available:
            raise RuntimeError("bluetoothctl is not installed; install the BlueZ package first")
        result = self._command_runner(["bluetoothctl", *args], input_text, timeout)
        output = _clean_output(f"{result.stdout}\n{result.stderr}").strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"bluetoothctl exited with status {result.returncode}")
        return output

    def refresh(self) -> dict[str, object]:
        """Refresh adapter, paired, and connected device state from BlueZ."""
        with self._bluez_lock:
            return self._refresh_locked()

    def _refresh_locked(self, *, strict: bool = False) -> dict[str, object]:
        """Refresh while the caller owns the re-entrant BlueZ operation lock."""
        try:
            show = self._run(["show"], timeout=6.0)
            known_output = self._run(["devices"], timeout=6.0)
            try:
                paired_output = self._run(["devices", "Paired"], timeout=6.0)
            except RuntimeError:
                paired_output = self._run(["paired-devices"], timeout=6.0)
            paired = _parse_devices(paired_output)
            known = _parse_devices(known_output)
            known.update(paired)
            try:
                connected = _parse_devices(self._run(["devices", "Connected"], timeout=6.0))
            except RuntimeError:
                connected = {}
                for address, name in known.items():
                    if "Connected: yes" in self._run(["info", address], timeout=6.0):
                        connected[address] = name
            known.update(connected)
            bonded: set[str] = set()
            for address in paired.keys() | connected.keys():
                try:
                    if self._device_properties(address).get("bonded") == "yes":
                        bonded.add(address)
                except RuntimeError:
                    if strict:
                        raise
            devices = {
                address: BluetoothDevice(
                    address=address,
                    name=name,
                    paired=address in paired,
                    bonded=address in bonded,
                    connected=address in connected,
                )
                for address, name in known.items()
            }
            with self._lock:
                self._adapter_available = True
                self._adapter_powered = "Powered: yes" in show
                self._devices = devices
                self._last_error = ""
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            if strict:
                raise
        return self.status()

    def scan(self, *, seconds: int = 12) -> dict[str, object]:
        seconds = max(5, min(30, int(seconds)))
        with self._bluez_lock:
            with self._lock:
                self._scan_active = True
            try:
                self._run(["power", "on"], timeout=8.0)
                output = self._run(["--timeout", str(seconds), "scan", "on"], timeout=seconds + 5.0)
                discovered = _parse_devices(output)
                with self._lock:
                    for address, name in discovered.items():
                        previous = self._devices.get(address)
                        self._devices[address] = BluetoothDevice(
                            address=address,
                            name=name,
                            paired=bool(previous and previous.paired),
                            connected=bool(previous and previous.connected),
                        )
                    self._last_error = ""
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                raise
            finally:
                with self._lock:
                    self._scan_active = False
            # Some BlueZ builds drop unpaired device objects as soon as the
            # scanning bluetoothctl client exits. Refresh bond/connection state,
            # then preserve this operation's bounded discovery results for the UI.
            self._refresh_locked(strict=True)
            with self._lock:
                for address, name in discovered.items():
                    if address not in self._devices:
                        self._devices[address] = BluetoothDevice(address=address, name=name)
            return self.status()

    def _device_properties(self, address: str) -> dict[str, str]:
        output = self._run(["info", address], timeout=8.0)
        properties: dict[str, str] = {}
        for raw_line in _strip_ansi(output).splitlines():
            line = raw_line.strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            properties[key.strip().lower()] = value.strip().lower()
        return properties

    def pair(self, address: str) -> dict[str, object]:
        with self._controller_transition_lock:
            return self._pair_serialized(address)

    def _pair_serialized(self, address: str) -> dict[str, object]:
        address = _validate_address(address)
        with self._bluez_lock:
            existing = self._device_properties(address)
            if existing.get("paired") == "yes" and existing.get("bonded") != "yes":
                # BlueZ can retain Paired=yes without a link key. The resulting
                # ACL connection is rejected by the input profile as !bonded.
                self._run(["remove", address], timeout=10.0)
            pair_error: Exception | None = None
            try:
                # Register an explicit headless agent for this bounded pairing
                # process; BlueZ requires an agent before first-time pairing.
                self._run(
                    ["--timeout", "35", "--agent", "NoInputNoOutput", "pair", address],
                    timeout=40.0,
                )
            except Exception as exc:
                pair_error = exc
            try:
                properties = self._device_properties(address)
                if properties.get("paired") != "yes":
                    raise RuntimeError(f"BlueZ did not confirm pairing for {address}") from pair_error
                if properties.get("bonded") != "yes":
                    raise RuntimeError(
                        f"BlueZ did not create a bond for {address}; put the controller in pairing mode and try again"
                    ) from pair_error
                self._run(["trust", address], timeout=8.0)
                properties = self._device_properties(address)
                if properties.get("trusted") != "yes":
                    raise RuntimeError(f"BlueZ did not confirm trust for {address}")
                self._run(["connect", address], timeout=15.0)
                properties = self._device_properties(address)
                if properties.get("connected") != "yes":
                    raise RuntimeError(f"BlueZ did not confirm connection for {address}")
                self._resume_controller_dispatch_if_enabled()
                with self._lock:
                    self._last_error = ""
                return self._refresh_locked(strict=True)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                raise

    def connect(self, address: str) -> dict[str, object]:
        with self._controller_transition_lock:
            return self._connect_serialized(address)

    def _connect_serialized(self, address: str) -> dict[str, object]:
        address = _validate_address(address)
        with self._bluez_lock:
            self._run(["trust", address], timeout=8.0)
            self._run(["connect", address], timeout=15.0)
            properties = self._device_properties(address)
            if (
                properties.get("bonded") != "yes"
                or properties.get("trusted") != "yes"
                or properties.get("connected") != "yes"
            ):
                raise RuntimeError(f"BlueZ did not confirm a bonded, trusted connection for {address}")
            self._resume_controller_dispatch_if_enabled()
            return self._refresh_locked(strict=True)

    def disconnect(self, address: str) -> dict[str, object]:
        with self._controller_transition_lock:
            return self._disconnect_serialized(address)

    def _disconnect_serialized(self, address: str) -> dict[str, object]:
        address = _validate_address(address)
        self._quiesce_controller("disconnect")
        try:
            with self._bluez_lock:
                self._run(["disconnect", address], timeout=10.0)
                properties = self._device_properties(address)
                if properties.get("connected") == "yes":
                    raise RuntimeError(f"BlueZ still reports {address} as connected")
                return self._refresh_locked(strict=True)
        except Exception:
            self._resume_controller_dispatch_if_enabled()
            raise

    def remove(self, address: str) -> dict[str, object]:
        with self._controller_transition_lock:
            return self._remove_serialized(address)

    def _remove_serialized(self, address: str) -> dict[str, object]:
        address = _validate_address(address)
        self._quiesce_controller("remove")
        try:
            with self._bluez_lock:
                self._run(["remove", address], timeout=10.0)
                try:
                    properties = self._device_properties(address)
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if "not available" not in message and "not found" not in message:
                        raise
                    properties = {}
                if any(properties.get(key) == "yes" for key in ("paired", "bonded", "trusted", "connected")):
                    raise RuntimeError(f"BlueZ still reports {address} as paired, bonded, trusted, or connected")
                status = self._refresh_locked(strict=True)
                devices = status.get("devices", [])
                if isinstance(devices, list) and any(
                    isinstance(device, dict) and device.get("address") == address
                    for device in devices
                ):
                    raise RuntimeError(f"BlueZ still reports {address} as a known device")
                return status
        except Exception:
            self._resume_controller_dispatch_if_enabled()
            raise

    def _resume_controller_dispatch_if_enabled(self) -> None:
        with self._dispatch_lock:
            with self._lock:
                self._dispatch_suspended = not self._gamepad_enabled

    def _quiesce_controller(self, operation: str) -> None:
        with self._dispatch_lock:
            self._dispatch_suspended = True
            if self._dispatch("stop", "", ""):
                return
            with self._lock:
                reason = self._last_error or "Stop was rejected"
                self._dispatch_suspended = not self._gamepad_enabled
            raise RuntimeError(f"Cannot {operation} controller because Stop failed: {reason}")

    def status(self) -> dict[str, object]:
        with self._lock:
            devices = [asdict(device) for device in sorted(self._devices.values(), key=lambda item: item.name.lower())]
            feature_status: dict[str, object] = {
                "rumble_available": False,
                "gyro_available": False,
                "touchpad_available": False,
                "gyro_active": False,
                "gyro_calibrating": False,
                "feature_input_error": "",
            }
            if self._controller_features is not None:
                feature_status.update(self._controller_features.status())
            feature_error = self._feature_error or str(feature_status.pop("feature_input_error", ""))
            return {
                "adapter_available": self._adapter_available,
                "adapter_powered": self._adapter_powered,
                "scan_active": self._scan_active,
                "devices": devices,
                "gamepad_enabled": self._gamepad_enabled,
                "gamepad_connected": self._gamepad_connected,
                "gamepad_name": self._gamepad_name,
                "gamepad_path": self._gamepad_path,
                "last_gamepad_action": self._last_gamepad_action,
                "feature_error": feature_error,
                **feature_status,
                "last_error": self._last_error,
            }

    def _reset_gamepad_state_locked(self) -> None:
        self._gamepad_connected = False
        self._gamepad_name = ""
        self._gamepad_path = ""
        self._axes.clear()
        self._last_direction = ""
        self._last_base_direction = ""

    def _close_controller_features(self) -> None:
        with self._lock:
            features = self._controller_features
            self._controller_features = None
        if features is not None:
            features.close()

    def set_gamepad_enabled(self, enabled: bool) -> dict[str, object]:
        with self._controller_transition_lock:
            return self._set_gamepad_enabled_serialized(enabled)

    def _set_gamepad_enabled_serialized(self, enabled: bool) -> dict[str, object]:
        enabled = bool(enabled)
        with self._lifecycle_lock:
            thread = self._gamepad_thread
            if enabled:
                if thread is not None and thread.is_alive():
                    if self._gamepad_enabled and not self._gamepad_stop.is_set():
                        self._resume_controller_dispatch_if_enabled()
                        return self.status()
                    raise RuntimeError("Previous gamepad reader is still stopping")
                self._gamepad_stop.clear()
                with self._lock:
                    self._gamepad_enabled = True
                    self._feature_error = ""
                    self._reset_gamepad_state_locked()
                thread = threading.Thread(
                    target=self._gamepad_loop,
                    name="homebody-gamepad",
                    daemon=True,
                )
                self._gamepad_thread = thread
                thread.start()
                self._resume_controller_dispatch_if_enabled()
            else:
                with self._dispatch_lock:
                    self._dispatch_suspended = True
                    with self._lock:
                        self._gamepad_enabled = False
                self._gamepad_stop.set()
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=2.0)
                    if thread.is_alive():
                        raise RuntimeError("Gamepad reader did not stop within two seconds")
                self._gamepad_thread = None
                self._close_controller_features()
                with self._lock:
                    self._reset_gamepad_state_locked()
        return self.status()

    @staticmethod
    def _joystick_name(fd: int, path: str) -> str:
        try:
            import fcntl

            buffer = bytearray(128)
            request = 0x80006A13 + (len(buffer) << 16)
            fcntl.ioctl(fd, request, buffer)
            return bytes(buffer).split(b"\0", 1)[0].decode("utf-8", "replace").strip() or Path(path).name
        except (OSError, ImportError):
            return Path(path).name

    @staticmethod
    def _joystick_vendor(path: str) -> str:
        vendor_path = Path("/sys/class/input") / Path(path).name / "device" / "id" / "vendor"
        try:
            return vendor_path.read_text(encoding="ascii").strip().lower()
        except OSError:
            return ""

    @staticmethod
    def _is_supported_playstation_controller(name: str, vendor: str) -> bool:
        normalized = " ".join(name.lower().split())
        return vendor.lower() == _SONY_USB_VENDOR_ID and (
            normalized in _SUPPORTED_EXACT_NAMES
            or any(part in normalized for part in _SUPPORTED_NAME_PARTS)
        )

    def _select_joystick(self) -> str:
        paths = self._joystick_glob()
        for path in paths:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                name = self._joystick_name(fd, path)
            finally:
                os.close(fd)
            if self._is_supported_playstation_controller(name, self._joystick_vendor(path)):
                return path
        return ""

    def _open_controller_features(self, path: str) -> None:
        with self._lock:
            if self._controller_features is not None:
                return
        try:
            features = self._feature_factory(path, self._dispatch_feature)
        except Exception as exc:
            with self._lock:
                self._feature_error = f"Extended controller features unavailable: {exc}"
            return
        if not features.discovery_complete():
            features.close()
            with self._lock:
                self._feature_error = "Waiting for the DualShock 4 evdev feature nodes"
            return
        with self._lock:
            if self._controller_features is not None:
                duplicate = True
            else:
                duplicate = False
                self._controller_features = features
                self._feature_error = ""
        if duplicate:
            features.close()
        else:
            features.play_feedback("connected")

    def _gamepad_loop(self) -> None:
        while not self._gamepad_stop.is_set():
            path = self._select_joystick()
            if not path:
                with self._lock:
                    self._reset_gamepad_state_locked()
                self._gamepad_stop.wait(1.0)
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                with self._lock:
                    self._last_error = f"Cannot open {path}: {exc}. Add the app user to the input group."
                self._gamepad_stop.wait(1.0)
                continue
            try:
                name = self._joystick_name(fd, path)
                with self._lock:
                    self._reset_gamepad_state_locked()
                    self._gamepad_connected = True
                    self._gamepad_name = name
                    self._gamepad_path = path
                    self._last_error = ""
                self._open_controller_features(path)
                self._read_joystick(fd, path)
            finally:
                self._close_controller_features()
                os.close(fd)
                with self._lock:
                    was_connected = self._gamepad_connected
                    self._reset_gamepad_state_locked()
                if was_connected and not self._gamepad_stop.is_set():
                    self._dispatch("stop", "", "")
            self._gamepad_stop.wait(0.5)

    def _read_joystick(self, fd: int, path: str) -> None:
        pending = b""
        next_feature_retry = 0.0
        while not self._gamepad_stop.is_set():
            now = time.monotonic()
            with self._lock:
                features = self._controller_features
            if features is None and now >= next_feature_retry:
                self._open_controller_features(path)
                next_feature_retry = now + 1.0
                with self._lock:
                    features = self._controller_features
            if features is not None:
                try:
                    feature_healthy = features.poll()
                except Exception as exc:
                    feature_healthy = False
                    with self._lock:
                        self._feature_error = f"Extended controller feature reader failed: {exc}"
                if not feature_healthy:
                    self._close_controller_features()
                    next_feature_retry = now + 1.0
            try:
                chunk = os.read(fd, _JS_EVENT.size * 32)
            except BlockingIOError:
                self._gamepad_stop.wait(0.03)
                continue
            except OSError:
                return
            if not chunk:
                return
            pending += chunk
            while len(pending) >= _JS_EVENT.size:
                event, pending = pending[: _JS_EVENT.size], pending[_JS_EVENT.size :]
                _timestamp, value, event_type, number = _JS_EVENT.unpack(event)
                self.handle_joystick_event(event_type, number, value)

    def _play_feature_feedback(self, pattern: str) -> None:
        with self._lock:
            features = self._controller_features
        if features is not None:
            features.play_feedback(pattern)

    def handle_joystick_event(self, event_type: int, number: int, value: int) -> None:
        """Handle one Linux joystick event; public for deterministic hardware-free tests."""
        if event_type & _JS_EVENT_INIT:
            return
        kind = event_type & ~_JS_EVENT_INIT
        if kind == _JS_EVENT_BUTTON and number == 6 and value in {0, 1}:
            with self._lock:
                features = self._controller_features
            if features is not None:
                features.handle_l2(value == 1)
            return
        if kind == _JS_EVENT_BUTTON and value == 1:
            mapping = {
                0: ("action", "emotion", "happy"),
                1: ("action", "look", "center"),
                2: ("stop", "", ""),
                3: ("action", "emotion", "surprised"),
                4: ("precision", "body_yaw", "5.0"),
                5: ("precision", "body_yaw", "-5.0"),
                11: ("precision", "center_head", "0.0"),
                12: ("precision", "center_base", "0.0"),
            }
            command = mapping.get(number)
            if command:
                accepted = self._dispatch(*command)
                pattern = "stop" if accepted and command[0] == "stop" else "accepted" if accepted else "rejected"
                self._play_feature_feedback(pattern)
            return
        if kind != _JS_EVENT_AXIS or number not in {0, 1, 2, 6, 7}:
            return
        self._axes[number] = int(value)
        if number == 2:
            base_direction = (
                "left"
                if value < -_STICK_THRESHOLD
                else "right"
                if value > _STICK_THRESHOLD
                else ""
            )
            if base_direction == self._last_base_direction:
                return
            self._last_base_direction = base_direction
            if base_direction:
                delta = "5.0" if base_direction == "left" else "-5.0"
                self._dispatch("precision", "body_yaw", delta)
            return
        use_dpad = abs(self._axes.get(6, 0)) > _STICK_THRESHOLD or abs(self._axes.get(7, 0)) > _STICK_THRESHOLD
        x = self._axes.get(6 if use_dpad else 0, 0)
        y = self._axes.get(7 if use_dpad else 1, 0)
        horizontal = "left" if x < -_STICK_THRESHOLD else "right" if x > _STICK_THRESHOLD else ""
        vertical = "up" if y < -_STICK_THRESHOLD else "down" if y > _STICK_THRESHOLD else ""
        direction = "_".join(part for part in (vertical, horizontal) if part)
        if direction == self._last_direction:
            return
        self._last_direction = direction
        if direction:
            self._dispatch("action", "look", direction)

    def _dispatch_feature(self, kind: str, action: str, value: str) -> None:
        if not self._dispatch(kind, action, value):
            with self._lock:
                reason = self._last_error or "Controller command was rejected"
            raise RuntimeError(reason)

    def _dispatch(self, kind: str, action: str, value: str) -> bool:
        label = "stop" if kind == "stop" else f"{action}:{value}"
        with self._dispatch_lock:
            if self._dispatch_suspended and kind != "stop":
                with self._lock:
                    self._last_error = "Controller movement dispatch is quiesced"
                return False
            try:
                accepted = self._action_callback(kind, action, value)
                if accepted is False:
                    raise RuntimeError("Controller command was rejected")
                with self._lock:
                    self._last_gamepad_action = label
                    self._last_error = ""
                return True
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                return False

    def close(self) -> None:
        self.set_gamepad_enabled(False)
