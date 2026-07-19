from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from reachy_mini_hermes.bluetooth import BluetoothGamepadService, _parse_devices


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
    assert service._axes
    with service._lock:
        service._reset_gamepad_state_locked()
    assert service._axes == {}
    assert service._last_direction == ""

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
    service.handle_joystick_event(0x01, 8, 1)  # Share has no mapped action.

    assert actions == [
        ("action", "look", "right"),
        ("action", "look", "up_right"),
        ("action", "look", "up"),
        ("action", "look", "center"),
        ("action", "emotion", "happy"),
        ("action", "emotion", "surprised"),
        ("stop", "", ""),
    ]


def test_bluetooth_ui_exposes_pairing_mapping_and_v22_assets() -> None:
    static = Path(__file__).resolve().parents[1] / "reachy_mini_hermes" / "static"
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
    assert "Reachy Mini Wireless only" in html
    assert "not supported on Reachy Mini Lite" in html
    assert "/api/bluetooth/scan" in script
    assert "/api/bluetooth/gamepad" in script
    assert "if (body.last_error) throw new Error(body.last_error);" in script
    assert "reachy-hermes-shell-v22" in worker
