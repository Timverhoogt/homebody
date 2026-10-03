"""Detect which kind of computer drives Reachy, and which optional hardware features it can offer.

Reachy Mini Lite can be driven by a PC or Mac over USB, by a Raspberry Pi (as in Reachy Mini
Wireless), or by an NVIDIA Jetson. Features are keyed on what the host can actually do, not on
its brand, so an unknown Linux board still gets GPIO when it exposes a GPIO character device.
Detection only reads files and never raises.
"""

from __future__ import annotations

import functools
import glob
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

HostKind = Literal["raspberry_pi", "jetson", "linux", "macos", "windows", "other"]

HOST_LABELS: dict[str, str] = {
    "raspberry_pi": "Raspberry Pi",
    "jetson": "NVIDIA Jetson",
    "linux": "Linux computer",
    "macos": "Mac",
    "windows": "Windows PC",
    "other": "Computer",
}

# Preference order for local ONNX inference. CPU is always the last resort.
ACCELERATED_PROVIDERS: tuple[str, ...] = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "CoreMLExecutionProvider",
    "DmlExecutionProvider",
)


@dataclass(frozen=True, slots=True)
class HostCapabilities:
    kind: HostKind
    label: str
    model: str
    system: str
    machine: str
    gpio_chips: list[str] = field(default_factory=list)
    gpio_supported: bool = False
    bluetooth_controller_supported: bool = False
    shutdown_supported: bool = False
    onnx_providers: list[str] = field(default_factory=list)
    accelerated: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_bytes().replace(b"\0", b"").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _host_kind(system: str, model: str) -> HostKind:
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    if system != "Linux":
        return "other"
    lowered = model.lower()
    if "raspberry pi" in lowered:
        return "raspberry_pi"
    if "jetson" in lowered or "nvidia" in lowered or Path("/etc/nv_tegra_release").exists():
        return "jetson"
    return "linux"


def onnx_providers() -> list[str]:
    """Return the execution providers this onnxruntime build offers, or [] when it is missing."""
    try:
        import onnxruntime  # noqa: PLC0415 - heavy optional import

        return [str(name) for name in onnxruntime.get_available_providers()]
    except Exception:
        return []


def detect_host(
    *,
    system: str | None = None,
    model: str | None = None,
    gpio_chips: list[str] | None = None,
    providers: list[str] | None = None,
) -> HostCapabilities:
    """Describe the host. Arguments override detection, for tests and diagnostics."""
    system = system if system is not None else platform.system()
    if model is None:
        model = _read_text("/proc/device-tree/model") if system == "Linux" else ""
    kind = _host_kind(system, model)
    linux = system == "Linux"
    chips = sorted(gpio_chips if gpio_chips is not None else (glob.glob("/dev/gpiochip*") if linux else []))
    available = list(providers if providers is not None else onnx_providers())
    return HostCapabilities(
        kind=kind,
        label=HOST_LABELS[kind],
        model=model or HOST_LABELS[kind],
        system=system,
        machine=platform.machine(),
        gpio_chips=chips,
        gpio_supported=linux and bool(chips),
        # BlueZ, evdev and joystick devices are Linux-only.
        bluetooth_controller_supported=linux,
        # Only offer to power off dedicated robot computers, never someone's desktop or laptop.
        shutdown_supported=kind in {"raspberry_pi", "jetson"},
        onnx_providers=available,
        accelerated=any(name in available for name in ACCELERATED_PROVIDERS),
    )


@functools.lru_cache(maxsize=1)
def host() -> HostCapabilities:
    """The detected host, computed once per process."""
    return detect_host()


def is_linux() -> bool:
    return sys.platform.startswith("linux")
