from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from homebody.bluetooth import BluetoothGamepadService, _parse_devices


@dataclass
class FakeResult:
    returncode: int
    stdout: str
    stderr: str = ""


def test_parse_devices_strips_ansi_and_normalizes_addresses() -> None:
    devices = _parse_devices(
        "\x1b[0;94mDevice aa:bb:cc:dd:ee:ff Wireless Controller\x1b[0m\n"
        "Device 11:22:33:44:55:66 DualSense Wireless Controller\n"
    )
    assert devices == {
        "AA:BB:CC:DD:EE:FF": "Wireless Controller",
        "11:22:33:44:55:66": "DualSense Wireless Controller",
    }


def test_refresh_combines_known_paired_and_connected_devices() -> None:
    def runner(command: list[str], _input: str | None, _timeout: float):
        arguments = command[1:]
        outputs = {
            ("show",): "Controller AA:BB\n Powered: yes\n",
            ("devices", "Paired"): "Device AA:BB:CC:DD:EE:FF Wireless Controller\n",
            ("devices", "Connected"): "Device AA:BB:CC:DD:EE:FF Wireless Controller\n",
            ("info", "AA:BB:CC:DD:EE:FF"): "Paired: yes\nBonded: yes\nConnected: yes\n",
            ("devices",): (
                "Device AA:BB:CC:DD:EE:FF Wireless Controller\n"
                "Device 11:22:33:44:55:66 Other Gamepad\n"
            ),
        }
        return FakeResult(returncode=0, stdout=outputs[tuple(arguments)])

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    status = service.refresh()

    assert status["adapter_available"] is True
    assert status["adapter_powered"] is True
    device_items = cast(list[dict[str, object]], status["devices"])
    devices = {str(item["address"]): item for item in device_items}
    assert devices["AA:BB:CC:DD:EE:FF"]["paired"] is True
    assert devices["AA:BB:CC:DD:EE:FF"]["bonded"] is True
    assert devices["AA:BB:CC:DD:EE:FF"]["connected"] is True
    assert devices["11:22:33:44:55:66"]["paired"] is False


def test_pair_uses_one_bounded_bluetoothctl_session_and_validates_mac() -> None:
    calls: list[tuple[list[str], str | None, float]] = []

    def runner(command: list[str], input_text: str | None, timeout: float):
        calls.append((command, input_text, timeout))
        arguments = command[1:]
        if arguments == ["show"]:
            output = "Powered: yes"
        elif arguments == ["devices", "Paired"]:
            output = "Device AA:BB:CC:DD:EE:FF Wireless Controller"
        elif arguments == ["devices", "Connected"]:
            output = "Device AA:BB:CC:DD:EE:FF Wireless Controller"
        elif arguments == ["devices"]:
            output = "Device AA:BB:CC:DD:EE:FF Wireless Controller"
        elif arguments == ["info", "AA:BB:CC:DD:EE:FF"]:
            output = "Paired: yes\nBonded: yes\nTrusted: yes\nConnected: yes"
        else:
            output = "Pairing successful\nConnection successful"
        return FakeResult(returncode=0, stdout=output)

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    status = service.pair("aa:bb:cc:dd:ee:ff")
    pair_call = next(call for call in calls if "pair" in call[0])
    assert pair_call[0] == [
        "bluetoothctl", "--timeout", "35", "--agent", "NoInputNoOutput",
        "pair", "AA:BB:CC:DD:EE:FF",
    ]
    assert pair_call[1] is None
    assert ["bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"] in [call[0] for call in calls]
    assert ["bluetoothctl", "connect", "AA:BB:CC:DD:EE:FF"] in [call[0] for call in calls]
    paired_devices = cast(list[dict[str, object]], status["devices"])
    assert paired_devices[0]["connected"] is True
    assert paired_devices[0]["bonded"] is True

    with pytest.raises(ValueError, match="MAC address"):
        service.pair("not-a-mac")


def test_pair_requires_positive_bluez_properties_before_trust() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], _input: str | None, _timeout: float):
        calls.append(command)
        if command[1:2] == ["info"]:
            return FakeResult(returncode=0, stdout="Paired: no\nTrusted: no\nConnected: no")
        return FakeResult(returncode=0, stdout="Pairing request returned")

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    with pytest.raises(RuntimeError, match="did not confirm pairing"):
        service.pair("AA:BB:CC:DD:EE:FF")
    assert not any(call[1:2] == ["trust"] for call in calls)


def test_pair_removes_stale_unbonded_device_and_requires_a_real_bond() -> None:
    calls: list[list[str]] = []
    info_calls = 0

    def runner(command: list[str], _input: str | None, _timeout: float):
        nonlocal info_calls
        calls.append(command)
        if command[1:2] == ["info"]:
            info_calls += 1
            return FakeResult(returncode=0, stdout="Paired: yes\nBonded: no\nConnected: yes")
        return FakeResult(returncode=0, stdout="Command successful")

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    with pytest.raises(RuntimeError, match="did not create a bond"):
        service.pair("AA:BB:CC:DD:EE:FF")

    assert info_calls == 2
    assert calls[1] == ["bluetoothctl", "remove", "AA:BB:CC:DD:EE:FF"]
    assert calls[2] == [
        "bluetoothctl", "--timeout", "35", "--agent", "NoInputNoOutput",
        "pair", "AA:BB:CC:DD:EE:FF",
    ]
    assert not any(call[1:2] == ["trust"] for call in calls)


def test_scan_failure_propagates_and_retains_error() -> None:
    def runner(_command: list[str], _input: str | None, _timeout: float):
        return FakeResult(returncode=1, stdout="", stderr="adapter blocked")

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    with pytest.raises(RuntimeError, match="adapter blocked"):
        service.scan(seconds=5)
    assert "adapter blocked" in str(service.status()["last_error"])
    assert service.status()["scan_active"] is False

    def successful_runner(command: list[str], _input: str | None, _timeout: float):
        arguments = command[1:]
        outputs = {
            ("power", "on"): "Changing power on succeeded",
            ("--timeout", "5", "scan", "on"): "Device AA:BB:CC:DD:EE:FF Wireless Controller",
            ("show",): "Powered: yes",
            ("devices",): "",
            ("devices", "Paired"): "",
            ("devices", "Connected"): "",
        }
        return FakeResult(returncode=0, stdout=outputs[tuple(arguments)])

    successful = BluetoothGamepadService(
        lambda *_args: None,
        command_runner=successful_runner,
        adapter_available=True,
    )
    devices = cast(list[dict[str, object]], successful.scan(seconds=5)["devices"])
    assert devices == [
        {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Wireless Controller",
            "paired": False,
            "bonded": False,
            "connected": False,
        }
    ]

    def refresh_failure_runner(command: list[str], _input: str | None, _timeout: float):
        arguments = command[1:]
        if arguments == ["power", "on"]:
            return FakeResult(returncode=0, stdout="Changing power on succeeded")
        if arguments == ["--timeout", "5", "scan", "on"]:
            return FakeResult(returncode=0, stdout="Device AA:BB:CC:DD:EE:FF Wireless Controller")
        return FakeResult(returncode=1, stdout="", stderr="org.bluez.Error.Failed")

    refresh_failure = BluetoothGamepadService(
        lambda *_args: None,
        command_runner=refresh_failure_runner,
        adapter_available=True,
    )
    with pytest.raises(RuntimeError, match="org.bluez.Error.Failed"):
        refresh_failure.scan(seconds=5)
    assert "org.bluez.Error.Failed" in str(refresh_failure.status()["last_error"])


def test_unsupported_joystick_fails_closed_and_state_reset_clears_axes(tmp_path, monkeypatch) -> None:
    joystick = tmp_path / "js0"
    joystick.write_bytes(b"")
    identity = {"name": "Unsupported Flight Stick", "vendor": "054c"}
    monkeypatch.setattr(
        BluetoothGamepadService,
        "_joystick_name",
        staticmethod(lambda _fd, _path: identity["name"]),
    )
    monkeypatch.setattr(
        BluetoothGamepadService,
        "_joystick_vendor",
        staticmethod(lambda _path: identity["vendor"]),
    )
    service = BluetoothGamepadService(
        lambda *_args: None,
        joystick_glob=lambda: [str(joystick)],
        adapter_available=False,
    )
    assert service._select_joystick() == ""
    service.handle_joystick_event(0x02, 0, 32_000)
    service.handle_joystick_event(0x02, 2, -32_000)
    assert service._axes
    assert service._last_base_direction == "left"
    with service._lock:
        service._reset_gamepad_state_locked()
    assert service._axes == {}
    assert service._last_direction == ""
    assert service._last_base_direction == ""

    unsupported = (
        ("Xbox Wireless Controller", "045e"),
        ("Generic USB Gamepad", "1234"),
        ("Nintendo Switch Pro Controller", "057e"),
        ("DualSense Wireless Controller", "057e"),
    )
    for name, vendor in unsupported:
        identity.update(name=name, vendor=vendor)
        assert not service._is_supported_playstation_controller(name, vendor)
        assert service._select_joystick() == ""
    identity.update(name="Wireless Controller", vendor="054c")
    assert service._is_supported_playstation_controller("Wireless Controller", "054c")
    assert service._select_joystick() == str(joystick)
    assert service._is_supported_playstation_controller(
        "Sony Interactive Entertainment DualSense Wireless Controller", "054C"
    )


def test_gamepad_mapping_is_allowlisted_debounced_and_stoppable() -> None:
    actions: list[tuple[str, str, str]] = []
    service = BluetoothGamepadService(
        lambda kind, action, value: actions.append((kind, action, value)),
        adapter_available=False,
    )
    service._dispatch_suspended = False

    service.handle_joystick_event(0x82, 0, 32_000)  # Initial-state event is ignored.
    service.handle_joystick_event(0x02, 0, 32_000)
    service.handle_joystick_event(0x02, 0, 32_000)  # Held direction does not flood the queue.
    service.handle_joystick_event(0x02, 1, -32_000)
    service.handle_joystick_event(0x02, 0, 0)
    service.handle_joystick_event(0x02, 1, 0)
    service.handle_joystick_event(0x01, 1, 1)
    service.handle_joystick_event(0x01, 0, 1)
    service.handle_joystick_event(0x01, 3, 1)
    service.handle_joystick_event(0x01, 2, 1)
    service.handle_joystick_event(0x01, 4, 1)
    service.handle_joystick_event(0x01, 5, 1)
    service.handle_joystick_event(0x01, 11, 1)
    service.handle_joystick_event(0x01, 12, 1)
    service.handle_joystick_event(0x02, 2, -32_000)
    service.handle_joystick_event(0x02, 2, -32_000)  # Held right stick is debounced.
    service.handle_joystick_event(0x02, 2, 0)
    service.handle_joystick_event(0x02, 2, 32_000)
    service.handle_joystick_event(0x01, 8, 1)  # Share has no mapped action.

    assert actions == [
        ("action", "look", "right"),
        ("action", "look", "up_right"),
        ("action", "look", "up"),
        ("action", "look", "center"),
        ("action", "emotion", "happy"),
        ("action", "emotion", "surprised"),
        ("stop", "", ""),
        ("precision", "body_yaw", "5.0"),
        ("precision", "body_yaw", "-5.0"),
        ("precision", "center_head", "0.0"),
        ("precision", "center_base", "0.0"),
        ("precision", "body_yaw", "5.0"),
        ("precision", "body_yaw", "-5.0"),
    ]


def test_incomplete_evdev_discovery_is_closed_and_retried() -> None:
    class FakeFeatures:
        def __init__(self, complete: bool) -> None:
            self.complete = complete
            self.closed = False

        def discovery_complete(self) -> bool:
            return self.complete

        def status(self) -> dict[str, object]:
            return {}

        def handle_l2(self, _pressed: bool) -> None:
            return None

        def play_feedback(self, _pattern: str) -> None:
            return None

        def poll(self) -> bool:
            return True

        def close(self) -> None:
            self.closed = True

    attempts = [FakeFeatures(False), FakeFeatures(True)]
    service = BluetoothGamepadService(
        lambda *_args: None,
        feature_factory=lambda _path, _dispatch: attempts.pop(0),  # type: ignore[arg-type]
        adapter_available=False,
    )
    first = attempts[0]
    service._open_controller_features("/dev/input/js0")
    assert first.closed is True
    assert "Waiting" in str(service.status()["feature_error"])
    service._open_controller_features("/dev/input/js0")
    assert service._controller_features is not None
    assert service.status()["feature_error"] == ""


def test_extended_feature_failure_does_not_break_legacy_joydev_actions() -> None:
    actions: list[tuple[str, str, str]] = []

    def broken_features(_path: str, _dispatch: object) -> object:
        raise RuntimeError("evdev unavailable")

    service = BluetoothGamepadService(
        lambda kind, action, value: actions.append((kind, action, value)),
        feature_factory=broken_features,  # type: ignore[arg-type]
        adapter_available=False,
    )
    service._dispatch_suspended = False
    service._open_controller_features("/dev/input/js0")
    service.handle_joystick_event(0x01, 1, 1)

    assert actions == [("action", "look", "center")]
    assert "evdev unavailable" in str(service.status()["feature_error"])
    assert service.status()["gyro_available"] is False


def test_extended_features_are_status_visible_and_l2_is_hold_to_enable() -> None:
    actions: list[tuple[str, str, str]] = []

    class FakeFeatures:
        def __init__(self) -> None:
            self.l2: list[bool] = []
            self.feedback: list[str] = []
            self.closed = False

        def poll(self) -> bool:
            return True

        def close(self) -> None:
            self.closed = True

        def handle_l2(self, pressed: bool) -> None:
            self.l2.append(pressed)

        def status(self) -> dict[str, bool]:
            return {
                "rumble_available": True,
                "gyro_available": True,
                "touchpad_available": True,
                "gyro_active": bool(self.l2 and self.l2[-1]),
            }

        def play_feedback(self, pattern: str) -> None:
            self.feedback.append(pattern)

    features = FakeFeatures()
    service = BluetoothGamepadService(
        lambda kind, action, value: actions.append((kind, action, value)),
        adapter_available=False,
    )
    service._controller_features = features  # type: ignore[assignment]

    service.handle_joystick_event(0x01, 6, 1)
    service.handle_joystick_event(0x01, 6, 0)
    service.handle_joystick_event(0x01, 2, 1)

    assert features.l2 == [True, False]
    assert features.feedback == ["stop"]
    assert actions == [("stop", "", "")]
    status = service.status()
    assert status["rumble_available"] is True
    assert status["gyro_available"] is True
    assert status["touchpad_available"] is True
    assert status["gyro_active"] is False

    service.set_gamepad_enabled(False)
    assert features.closed is True
    assert service.status()["gyro_available"] is False


def test_rejected_stop_uses_rejected_feedback_not_stop_confirmation() -> None:
    class FakeFeatures:
        def status(self) -> dict[str, object]:
            return {}

        def play_feedback(self, pattern: str) -> None:
            feedback.append(pattern)

    def reject_stop(_kind: str, _action: str, _value: str) -> None:
        raise RuntimeError("stop rejected")

    feedback: list[str] = []
    service = BluetoothGamepadService(reject_stop, adapter_available=False)
    service._controller_features = FakeFeatures()  # type: ignore[assignment]
    service.handle_joystick_event(0x01, 2, 1)

    assert feedback == ["rejected"]
    assert service.status()["last_error"] == "stop rejected"


def test_explicit_false_callback_result_is_rejected() -> None:
    service = BluetoothGamepadService(lambda *_args: False, adapter_available=False)

    assert service._dispatch("stop", "", "") is False
    assert service.status()["last_error"] == "Controller command was rejected"
    assert service.status()["last_gamepad_action"] == ""


def test_disconnect_and_remove_abort_before_bluez_when_stop_fails() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], _input: str | None, _timeout: float) -> FakeResult:
        calls.append(command)
        return FakeResult(returncode=0, stdout="Command successful")

    def reject_stop(_kind: str, _action: str, _value: str) -> None:
        raise RuntimeError("motion cancellation failed")

    service = BluetoothGamepadService(reject_stop, command_runner=runner, adapter_available=True)
    for operation in (service.disconnect, service.remove):
        with pytest.raises(RuntimeError, match="Stop failed: motion cancellation failed"):
            operation("AA:BB:CC:DD:EE:FF")
    assert calls == []


def test_remove_rejects_device_that_bluez_still_reports_connected_or_trusted() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], _input: str | None, _timeout: float) -> FakeResult:
        calls.append(command)
        if command[1:2] == ["info"]:
            return FakeResult(returncode=0, stdout="Paired: no\nBonded: no\nTrusted: yes\nConnected: yes")
        return FakeResult(returncode=0, stdout="Command successful")

    service = BluetoothGamepadService(lambda *_args: None, command_runner=runner, adapter_available=True)
    with pytest.raises(RuntimeError, match="paired, bonded, trusted, or connected"):
        service.remove("AA:BB:CC:DD:EE:FF")

    assert calls[0][1:2] == ["remove"]
    assert calls[1][1:2] == ["info"]


def test_disconnect_quiesces_new_controller_input_until_bluez_finishes() -> None:
    actions: list[tuple[str, str, str]] = []
    disconnect_started = threading.Event()
    allow_disconnect = threading.Event()
    errors: list[Exception] = []

    def runner(command: list[str], _input: str | None, _timeout: float) -> FakeResult:
        if command[1:2] == ["disconnect"]:
            disconnect_started.set()
            assert allow_disconnect.wait(timeout=2.0)
            return FakeResult(returncode=0, stdout="Disconnected")
        if command[1:2] == ["info"]:
            return FakeResult(returncode=0, stdout="Paired: yes\nBonded: yes\nTrusted: yes\nConnected: no")
        if command[1:2] == ["show"]:
            return FakeResult(returncode=0, stdout="Powered: yes")
        return FakeResult(returncode=0, stdout="")

    service = BluetoothGamepadService(
        lambda kind, action, value: actions.append((kind, action, value)),
        command_runner=runner,
        adapter_available=True,
    )
    service._gamepad_enabled = True
    service._dispatch_suspended = False

    def run_disconnect() -> None:
        try:
            service.disconnect("AA:BB:CC:DD:EE:FF")
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    worker = threading.Thread(target=run_disconnect)
    worker.start()
    assert disconnect_started.wait(timeout=2.0)
    service.handle_joystick_event(0x02, 2, -32_000)
    allow_disconnect.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert errors == []
    assert actions == [("stop", "", "")]
    assert service._dispatch_suspended is True


def test_connect_cannot_resume_dispatch_inside_a_newer_disconnect_transition() -> None:
    actions: list[tuple[str, str, str]] = []
    connect_started = threading.Event()
    allow_connect = threading.Event()
    disconnect_started = threading.Event()
    allow_disconnect = threading.Event()
    errors: list[Exception] = []

    def runner(command: list[str], _input: str | None, _timeout: float) -> FakeResult:
        if command[1:2] == ["connect"]:
            connect_started.set()
            assert allow_connect.wait(timeout=2.0)
            return FakeResult(returncode=0, stdout="Connection successful")
        if command[1:2] == ["disconnect"]:
            disconnect_started.set()
            assert allow_disconnect.wait(timeout=2.0)
            return FakeResult(returncode=0, stdout="Disconnected")
        if command[1:2] == ["info"]:
            connected = "no" if disconnect_started.is_set() else "yes"
            return FakeResult(
                returncode=0,
                stdout=f"Paired: yes\nBonded: yes\nTrusted: yes\nConnected: {connected}",
            )
        if command[1:2] == ["show"]:
            return FakeResult(returncode=0, stdout="Powered: yes")
        return FakeResult(returncode=0, stdout="")

    service = BluetoothGamepadService(
        lambda kind, action, value: actions.append((kind, action, value)),
        command_runner=runner,
        adapter_available=True,
    )
    service._gamepad_enabled = True
    service._dispatch_suspended = False

    def run(operation: str) -> None:
        try:
            getattr(service, operation)("AA:BB:CC:DD:EE:FF")
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    connect_worker = threading.Thread(target=run, args=("connect",))
    disconnect_worker = threading.Thread(target=run, args=("disconnect",))
    connect_worker.start()
    assert connect_started.wait(timeout=2.0)
    disconnect_worker.start()
    allow_connect.set()
    assert disconnect_started.wait(timeout=2.0)

    service.handle_joystick_event(0x02, 2, -32_000)
    allow_disconnect.set()
    connect_worker.join(timeout=2.0)
    disconnect_worker.join(timeout=2.0)

    assert not connect_worker.is_alive() and not disconnect_worker.is_alive()
    assert errors == []
    assert actions == [("stop", "", "")]
    assert service._dispatch_suspended is True


def test_bluetooth_ui_exposes_pairing_mapping_and_v35_assets() -> None:
    static = Path(__file__).resolve().parents[1] / "homebody" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "main.js").read_text(encoding="utf-8")
    worker = (static / "service-worker.js").read_text(encoding="utf-8")

    for element_id in (
        "bluetooth-device-select",
        "bluetooth-scan-button",
        "bluetooth-pair-button",
        "bluetooth-connect-button",
        "bluetooth-disconnect-button",
        "bluetooth-remove-button",
        "gamepad-enabled",
    ):
        assert f'id="{element_id}"' in html
    assert "Share + PS" in html
    assert "Create + PS" in html
    assert "Right stick rotates the base" in html
    assert "L1 / R1" in html
    assert "L3 centers the head" in html
    assert "R3 centers the base" in html
    assert "Touchpad click centers all" in html
    assert "Hold L2 still for gyro calibration" in html
    assert "Rumble acknowledges selected commands" in html
    assert "feature_error" in script
    assert "gyro_calibrating" in script
    assert "Linux host with Bluetooth only" in html
    assert "Not available when a Mac or Windows PC drives Reachy" in html
    assert "/api/bluetooth/scan" in script
    assert "/api/bluetooth/gamepad" in script
    assert "if (body.last_error) throw new Error(body.last_error);" in script
    assert "homebody-shell-v51" in worker
