"""HA device continuity and request-only camera/power privacy regressions."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from homebody import home_assistant as module
from homebody.config import AppConfig
from homebody.home_assistant import DeviceIdentity, HermesHomeAssistantProvider, HomeAssistantBridge

IDENTITY: dict[str, Any] = dict(
    home_assistant_device_name="Reachy Mini Jetson",
    home_assistant_node_name="reachy-mini-jetson",
    home_assistant_mac_address="02376b021637",
)


def test_existing_ha_device_identity_is_preserved_by_the_native_bridge() -> None:
    config = AppConfig(**IDENTITY)
    runtime = SimpleNamespace(control_ready=True, robot=object(), status=lambda: {})
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config)
    bridge = HomeAssistantBridge(provider, config=config)
    assert bridge.identity == DeviceIdentity("Reachy Mini Jetson", "reachy-mini-jetson", "02376b021637")
    assert provider.read("robot_name") == "Reachy Mini Jetson"


@pytest.mark.parametrize("override", [
    {"home_assistant_device_name": ""},
    {"home_assistant_device_name": "bad\nname"},
    {"home_assistant_node_name": "bad.local."},
    {"home_assistant_node_name": "-bad"},
    {"home_assistant_mac_address": "02:37:6b:02:16:37"},
    {"home_assistant_mac_address": "02376b0216370"},
    {"home_assistant_mac_address": 123},
])
def test_invalid_or_partial_ha_identity_is_rejected_without_repair(override) -> None:
    with pytest.raises(ValueError, match="Home Assistant identity"):
        AppConfig(**(IDENTITY | override))


class CameraRuntime:
    control_ready = True
    robot = object()

    def __init__(self) -> None:
        self.mode = "awake"
        self.kids = {"active": False, "locked": False}
        self.capture_calls = 0
        self.power_calls: list[str] = []
        self.after_capture = lambda: None
        source = io.BytesIO()
        Image.new("RGB", (32, 24), (60, 80, 100)).save(source, format="JPEG")
        self.jpeg = source.getvalue()

    def status(self):
        return {"power_mode": self.mode, "kids_mode": self.kids, "last_error": ""}

    def camera_snapshot(self):
        self.capture_calls += 1
        self.after_capture()
        return self.jpeg

    def set_power_mode(self, mode, **kwargs):
        self.power_calls.append(mode)
        self.mode = mode
        return {"power_mode": mode, "motors_enabled": mode == "awake", "head_safely_folded": mode != "awake"}


def provider_for(runtime, **settings):
    config = AppConfig(home_assistant_enabled=True, home_assistant_camera_enabled=True,
                       home_assistant_controls_enabled=True)
    for key, value in settings.items():
        setattr(config, key, value)
    return HermesHomeAssistantProvider(runtime, config_loader=lambda: config)


@pytest.mark.parametrize("mode", ["standby", "meeting", "sleep", "unknown"])
def test_ha_camera_does_not_capture_outside_awake(mode) -> None:
    runtime = CameraRuntime()
    runtime.mode = mode
    assert provider_for(runtime).camera_image() is None
    assert runtime.capture_calls == 0


@pytest.mark.parametrize("flag", ["active", "locked"])
def test_ha_camera_and_adult_power_cannot_bypass_kids(flag) -> None:
    runtime = CameraRuntime()
    runtime.kids[flag] = True
    provider = provider_for(runtime)
    assert provider.camera_image() is None
    assert runtime.capture_calls == 0
    assert provider.write("awake", True) is False
    assert provider.write("awake", False) is False
    assert provider.write("mute", True) is False
    assert runtime.power_calls == []


def test_ha_camera_discards_capture_that_finishes_after_standby() -> None:
    runtime = CameraRuntime()
    runtime.after_capture = lambda: setattr(runtime, "mode", "standby")
    assert provider_for(runtime).camera_image() is None
    assert runtime.capture_calls == 1


def test_ha_camera_rechecks_privacy_after_jpeg_encoding(monkeypatch) -> None:
    runtime = CameraRuntime()
    normalize = module._normalize_camera_jpeg_for_home_assistant
    def revoke_during_encoding(jpeg):
        result = normalize(jpeg)
        runtime.kids["locked"] = True
        return result
    monkeypatch.setattr(module, "_normalize_camera_jpeg_for_home_assistant", revoke_during_encoding)
    assert provider_for(runtime).camera_image() is None


@pytest.mark.parametrize("settings", [
    {"home_assistant_enabled": False}, {"home_assistant_camera_enabled": False},
])
def test_ha_camera_does_not_capture_after_local_opt_in_revocation(settings) -> None:
    runtime = CameraRuntime()
    assert provider_for(runtime, **settings).camera_image() is None
    assert runtime.capture_calls == 0


def test_ha_power_cannot_run_before_runtime_readiness() -> None:
    runtime = CameraRuntime()
    runtime.control_ready = False
    assert provider_for(runtime).write("awake", True) is False
    assert runtime.power_calls == []


def test_ha_power_cannot_run_after_bridge_opt_in_revocation() -> None:
    runtime = CameraRuntime()
    assert provider_for(runtime, home_assistant_enabled=False).write("awake", True) is False
    assert runtime.power_calls == []


def test_ha_camera_only_captures_on_request_not_provider_creation_or_telemetry() -> None:
    runtime = CameraRuntime()
    provider = provider_for(runtime)
    assert runtime.capture_calls == 0
    assert provider.read("backend_ready") is True
    assert runtime.capture_calls == 0
    assert provider.camera_image() is not None
    assert runtime.capture_calls == 1
