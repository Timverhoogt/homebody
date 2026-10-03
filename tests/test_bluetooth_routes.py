from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody


class FakeBluetoothService:
    def __init__(self) -> None:
        self.scanned_seconds = 0
        self.enabled: bool | None = None
        self.enable_calls: list[bool] = []

    def _status(self) -> dict[str, object]:
        return {
            "adapter_available": True,
            "adapter_powered": True,
            "scan_active": False,
            "devices": [],
            "gamepad_enabled": bool(self.enabled),
            "gamepad_connected": False,
            "gamepad_name": "",
            "gamepad_path": "",
            "last_gamepad_action": "",
            "last_error": "",
        }

    def refresh(self) -> dict[str, object]:
        return self._status()

    def scan(self, *, seconds: int) -> dict[str, object]:
        if seconds == 5:
            raise RuntimeError("adapter blocked")
        self.scanned_seconds = seconds
        return self._status()

    def set_gamepad_enabled(self, enabled: bool) -> dict[str, object]:
        self.enable_calls.append(enabled)
        self.enabled = enabled
        return self._status()


def test_bluetooth_management_routes_are_bounded_and_persist_gamepad_opt_in(monkeypatch) -> None:
    app = Homebody(False)
    service = FakeBluetoothService()
    app._bluetooth = service  # type: ignore[assignment]
    saved: list[AppConfig] = []
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig())
    monkeypatch.setattr(main_module, "save_config", lambda config: saved.append(config))
    client = TestClient(app.settings_app)

    status = client.get("/api/bluetooth/status")
    assert status.status_code == 200
    assert status.json()["adapter_powered"] is True

    scan = client.post("/api/bluetooth/scan", json={"seconds": 7})
    assert scan.status_code == 200
    assert service.scanned_seconds == 7
    failed_scan = client.post("/api/bluetooth/scan", json={"seconds": 5})
    assert failed_scan.status_code == 503
    assert failed_scan.json()["detail"] == "adapter blocked"

    invalid = client.post("/api/bluetooth/pair", json={"address": "not-a-mac"})
    assert invalid.status_code == 422

    enabled = client.post("/api/bluetooth/gamepad", json={"enabled": True})
    assert enabled.status_code == 200
    assert service.enabled is True
    assert saved and saved[-1].gamepad_enabled is True


def test_gamepad_enable_failure_does_not_persist_future_auto_enable(monkeypatch) -> None:
    class FailingBluetoothService(FakeBluetoothService):
        def set_gamepad_enabled(self, enabled: bool) -> dict[str, object]:
            self.enable_calls.append(enabled)
            raise RuntimeError("Previous gamepad reader is still stopping")

    app = Homebody(False)
    service = FailingBluetoothService()
    app._bluetooth = service  # type: ignore[assignment]
    saved: list[AppConfig] = []
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(gamepad_enabled=False))
    monkeypatch.setattr(main_module, "save_config", lambda config: saved.append(config))
    client = TestClient(app.settings_app)

    response = client.post("/api/bluetooth/gamepad", json={"enabled": True})

    assert response.status_code == 409
    assert service.enable_calls == [True]
    assert saved == []


def test_gamepad_enable_rolls_back_runtime_if_persistence_fails(monkeypatch) -> None:
    app = Homebody(False)
    service = FakeBluetoothService()
    app._bluetooth = service  # type: ignore[assignment]
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(gamepad_enabled=False))

    def fail_save(_config: AppConfig) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(main_module, "save_config", fail_save)
    client = TestClient(app.settings_app)

    response = client.post("/api/bluetooth/gamepad", json={"enabled": True})

    assert response.status_code == 500
    assert service.enable_calls == [True, False]
    assert service.enabled is False


def test_gamepad_enable_disable_transactions_are_serialized(monkeypatch) -> None:
    enable_entered = threading.Event()
    release_enable = threading.Event()
    disable_called = threading.Event()

    class BlockingBluetoothService(FakeBluetoothService):
        def set_gamepad_enabled(self, enabled: bool) -> dict[str, object]:
            self.enable_calls.append(enabled)
            if enabled:
                enable_entered.set()
                assert release_enable.wait(timeout=2.0)
            else:
                disable_called.set()
            self.enabled = enabled
            return self._status()

    app = Homebody(False)
    service = BlockingBluetoothService()
    app._bluetooth = service  # type: ignore[assignment]
    saved: list[AppConfig] = []
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig(gamepad_enabled=False))
    monkeypatch.setattr(main_module, "save_config", lambda config: saved.append(config))
    client = TestClient(app.settings_app)
    responses: list[int] = []

    enable_thread = threading.Thread(
        target=lambda: responses.append(client.post("/api/bluetooth/gamepad", json={"enabled": True}).status_code)
    )
    disable_thread = threading.Thread(
        target=lambda: responses.append(client.post("/api/bluetooth/gamepad", json={"enabled": False}).status_code)
    )
    enable_thread.start()
    assert enable_entered.wait(timeout=2.0)
    disable_thread.start()
    assert not disable_called.wait(timeout=0.1)
    release_enable.set()
    enable_thread.join(timeout=2.0)
    disable_thread.join(timeout=2.0)

    assert not enable_thread.is_alive() and not disable_thread.is_alive()
    assert sorted(responses) == [200, 200]
    assert service.enable_calls == [True, False]
    assert [config.gamepad_enabled for config in saved] == [True, False]
    assert service.enabled is False


def test_gamepad_stop_requires_authoritative_idle_confirmation() -> None:
    class FakeRuntime:
        @staticmethod
        def stop_manual_robot_action() -> dict[str, object]:
            return {"ok": True, "robot_stopped": False}

    app = Homebody(False)
    app._runtime = FakeRuntime()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="did not confirm Stop completion"):
        app._handle_gamepad_action("stop", "", "")


def test_gamepad_precision_actions_use_the_existing_safe_precision_runtime_path() -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.precision: list[tuple[str, float]] = []

        def queue_precision_robot_action(self, axis: str, delta: float) -> None:
            self.precision.append((axis, delta))

    app = Homebody(False)
    runtime = FakeRuntime()
    app._runtime = runtime  # type: ignore[assignment]

    app._handle_gamepad_action("precision", "body_yaw", "5.0")
    app._handle_gamepad_action("precision", "body_yaw", "-5.0")
    app._handle_gamepad_action("precision", "center_base", "0.0")
    app._handle_gamepad_action("precision", "pitch", "2.5")
    app._handle_gamepad_action("precision", "yaw", "-2.5")
    app._handle_gamepad_action("precision", "roll", "2.5")
    app._handle_gamepad_action("precision", "center_all", "0.0")

    assert runtime.precision == [
        ("body_yaw", 5.0),
        ("body_yaw", -5.0),
        ("center_base", 0.0),
        ("pitch", 2.5),
        ("yaw", -2.5),
        ("roll", 2.5),
        ("center_all", 0.0),
    ]
