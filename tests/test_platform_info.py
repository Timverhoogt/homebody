from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody import platform_info
from homebody.config import AppConfig
from homebody.home_assistant import default_device_identity
from homebody.main import Homebody
from homebody.platform_info import detect_host


def test_raspberry_pi_is_detected_with_gpio_bluetooth_and_shutdown() -> None:
    info = detect_host(
        system="Linux",
        model="Raspberry Pi 4 Model B Rev 1.5",
        gpio_chips=["/dev/gpiochip0"],
        providers=["CPUExecutionProvider"],
    )

    assert info.kind == "raspberry_pi"
    assert info.gpio_supported and info.bluetooth_controller_supported and info.shutdown_supported
    assert info.accelerated is False


def test_jetson_is_detected_with_gpu_acceleration() -> None:
    info = detect_host(
        system="Linux",
        model="NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super",
        gpio_chips=["/dev/gpiochip0", "/dev/gpiochip1"],
        providers=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert info.kind == "jetson" and info.label == "NVIDIA Jetson"
    assert info.accelerated is True
    assert info.gpio_supported and info.shutdown_supported


@pytest.mark.parametrize(
    ("system", "kind", "provider"),
    [("Darwin", "macos", "CoreMLExecutionProvider"), ("Windows", "windows", "DmlExecutionProvider")],
)
def test_desktop_hosts_offer_no_gpio_controller_or_remote_power_off(system: str, kind: str, provider: str) -> None:
    info = detect_host(system=system, model="", gpio_chips=[], providers=[provider, "CPUExecutionProvider"])

    assert info.kind == kind
    assert not info.gpio_supported
    assert not info.bluetooth_controller_supported
    assert not info.shutdown_supported
    assert info.accelerated is True


def test_generic_linux_pc_gets_controller_but_not_remote_power_off(monkeypatch) -> None:
    from pathlib import Path

    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: False if str(p) == "/etc/nv_tegra_release" else original_exists(p))
    info = detect_host(system="Linux", model="", gpio_chips=[], providers=["CPUExecutionProvider"])

    assert info.kind == "linux"
    assert info.bluetooth_controller_supported and not info.gpio_supported and not info.shutdown_supported


def test_status_reports_the_host_and_shutdown_refuses_on_a_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = detect_host(system="Darwin", model="", gpio_chips=[], providers=["CPUExecutionProvider"])
    monkeypatch.setattr(main_module, "host", lambda: desktop)
    monkeypatch.setattr(main_module, "load_config", lambda: AppConfig())
    ran: list[object] = []
    monkeypatch.setattr(main_module.subprocess, "run", lambda *args, **kwargs: ran.append(args))
    client = TestClient(Homebody(False).settings_app)

    status = client.get("/api/status").json()
    assert status["host"]["kind"] == "macos" and status["host"]["shutdown_supported"] is False

    refused = client.post("/api/shutdown", json={"confirm": "shutdown"})
    assert refused.status_code == 409
    assert "Mac" in refused.json()["detail"]
    assert ran == []


def test_home_assistant_identity_is_host_specific_without_machine_id(tmp_path) -> None:
    identity = default_device_identity(machine_id_path=tmp_path / "missing")

    assert identity.mac_address != "000000000000"
    assert len(identity.mac_address) == 12


def test_settings_ui_hides_hardware_cards_the_host_cannot_drive() -> None:
    from pathlib import Path

    script = (Path(main_module.__file__).parent / "static" / "main.js").read_text(encoding="utf-8")

    assert '.bluetooth-card").hidden = !hostInfo.bluetooth_controller_supported' in script
    assert '.gpio-card").hidden = !hostInfo.gpio_supported' in script
    assert '$("shutdown-button").hidden = !hostInfo.shutdown_supported' in script


def test_auto_prefers_tensorrt_then_cuda_and_always_keeps_the_cpu(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        platform_info,
        "onnx_providers",
        lambda: ["CPUExecutionProvider", "CUDAExecutionProvider", "TensorrtExecutionProvider"],
    )

    providers = platform_info.onnx_session_providers("auto", cache_dir=tmp_path)

    assert providers[0][0] == "TensorrtExecutionProvider"  # type: ignore[index]
    assert providers[0][1]["trt_engine_cache_path"] == str(tmp_path)  # type: ignore[index]
    assert providers[1:] == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_cpu_preference_and_plain_builds_use_only_the_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_info, "onnx_providers", lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"])

    assert platform_info.onnx_session_providers("cpu") == ["CPUExecutionProvider"]
    assert platform_info.onnx_session_providers("auto") == ["CoreMLExecutionProvider", "CPUExecutionProvider"]


def test_a_failing_accelerator_falls_back_to_a_cpu_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import onnxruntime

    opened: list[object] = []

    class FakeSession:
        def __init__(self, path: str, providers: list[object]) -> None:
            opened.append(providers)
            if providers != ["CPUExecutionProvider"]:
                raise RuntimeError("libcudnn.so.9: cannot open shared object file")

    monkeypatch.setattr(onnxruntime, "InferenceSession", FakeSession)

    accelerated = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    session = platform_info.create_onnx_session(tmp_path / "model.onnx", accelerated)

    assert isinstance(session, FakeSession)
    assert opened == [["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]]


def test_unknown_accelerator_setting_is_rejected() -> None:
    with pytest.raises(ValueError, match="local_ai_accelerator"):
        AppConfig(local_ai_accelerator="gpu")
