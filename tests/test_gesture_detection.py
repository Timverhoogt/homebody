from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from homebody.gesture_detection import (
    GESTURE_MODEL_SHA256,
    GestureDetector,
    GestureReactionGate,
    reaction_for_gesture,
)


def test_supported_gestures_map_to_bounded_fun_actions() -> None:
    assert reaction_for_gesture("palm") == ("emotion", "welcoming")
    assert reaction_for_gesture("peace") == ("emotion", "excited")
    assert reaction_for_gesture("peace_inverted") == ("emotion", "excited")
    assert reaction_for_gesture("rock") == ("dance", "short")
    assert reaction_for_gesture("stop") is None
    assert reaction_for_gesture("no_gesture") is None


def test_reaction_gate_requires_confirmation_cooldown_and_clear_to_rearm() -> None:
    gate = GestureReactionGate(required_frames=3, clear_frames=2, cooldown_seconds=8.0, min_confidence=0.70)

    assert gate.update("palm", 0.80, now=10.0) is None
    assert gate.update("palm", 0.82, now=10.3) is None
    assert gate.update("palm", 0.84, now=10.6) == ("emotion", "welcoming")
    assert gate.update("palm", 0.90, now=11.0) is None
    assert gate.update("peace", 0.95, now=12.0) is None
    assert gate.update("peace", 0.95, now=12.3) is None
    assert gate.update("peace", 0.95, now=12.6) is None

    assert gate.update("no_gesture", 0.0, now=18.7) is None
    assert gate.update("no_gesture", 0.0, now=19.0) is None
    assert gate.update("peace", 0.95, now=19.3) is None
    assert gate.update("peace", 0.95, now=19.6) is None
    assert gate.update("peace", 0.95, now=19.9) == ("emotion", "excited")


def test_reaction_gate_resets_on_low_confidence_or_changed_candidate() -> None:
    gate = GestureReactionGate(required_frames=3, clear_frames=2, cooldown_seconds=0.0, min_confidence=0.70)

    assert gate.update("rock", 0.90, now=1.0) is None
    assert gate.update("rock", 0.60, now=1.1) is None
    assert gate.update("rock", 0.90, now=1.2) is None
    assert gate.update("peace", 0.90, now=1.3) is None
    assert gate.update("rock", 0.90, now=1.4) is None
    assert gate.update("rock", 0.90, now=1.5) is None
    assert gate.update("rock", 0.90, now=1.6) == ("dance", "short")


def test_bundled_gesture_models_have_pinned_hashes() -> None:
    models = Path(__file__).resolve().parents[1] / "homebody" / "assets" / "gesture_models"
    assert GESTURE_MODEL_SHA256 == {
        "crops_classifier.onnx": "12a02344f63a7c4f2a2ca90f8740ca10a08c17b683b5585d73c3e88323056762",
        "hand_detector.onnx": "a8ef73d466b61a8e8677be9c47008b217a11d1b265d95e36bf2521ff93329af6",
    }
    assert {path.name for path in models.glob("*.onnx")} == set(GESTURE_MODEL_SHA256)


def test_real_models_load_and_reject_a_blank_frame() -> None:
    models = Path(__file__).resolve().parents[1] / "homebody" / "assets" / "gesture_models"
    detector = GestureDetector(models)
    try:
        assert detector.active_provider == "CPUExecutionProvider"
        ok, encoded = cv2.imencode(".jpg", np.zeros((240, 320, 3), dtype=np.uint8))
        assert ok
        assert detector.detect_jpeg(encoded.tobytes()) == ("no_gesture", 0.0)
    finally:
        detector.close()
