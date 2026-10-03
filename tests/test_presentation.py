from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from homebody.presentation import IntentionalPresentationGate


def jpeg(*, center: int = 30, border: int = 30) -> bytes:
    pixels = np.full((120, 160, 3), border, dtype=np.uint8)
    pixels[30:90, 40:120] = center
    output = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


def test_gate_requires_repeated_central_change_after_explicit_start() -> None:
    gate = IntentionalPresentationGate(required_stable_frames=3)
    gate.begin(jpeg())

    assert gate.observe(jpeg()) is False
    assert gate.observe(jpeg(center=220)) is False
    assert gate.observe(jpeg(center=220)) is False
    assert gate.observe(jpeg(center=220)) is True
    assert gate.detected is True


def test_gate_ignores_border_motion_and_resets_unstable_change() -> None:
    gate = IntentionalPresentationGate(required_stable_frames=2)
    gate.begin(jpeg())

    assert gate.observe(jpeg(border=220)) is False
    assert gate.observe(jpeg(center=220)) is False
    assert gate.observe(jpeg(center=30)) is False
    assert gate.observe(jpeg(center=220)) is False
    assert gate.observe(jpeg(center=220)) is True


def test_gate_rejects_invalid_or_oversized_frames_without_retaining_images() -> None:
    gate = IntentionalPresentationGate()
    with pytest.raises(ValueError, match="JPEG"):
        gate.begin(b"not-an-image")
    with pytest.raises(ValueError, match="size"):
        gate.begin(b"\xff\xd8" + b"x" * 1_000_001 + b"\xff\xd9")
    gate.begin(jpeg())
    assert not hasattr(gate, "baseline_jpeg")
    assert not hasattr(gate, "frames")


def test_gate_cannot_observe_before_explicit_start() -> None:
    gate = IntentionalPresentationGate()
    with pytest.raises(RuntimeError, match="not active"):
        gate.observe(jpeg(center=220))
