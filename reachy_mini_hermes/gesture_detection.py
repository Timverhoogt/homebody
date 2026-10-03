"""Private, opt-in on-device hand gesture detection and reaction gating.

The ONNX preprocessing and class map are adapted from the Apache-2.0
Reachy Mini Home Assistant app at commit
c5fd1f522ab44e8e9feb2897d4018027a8afb063. Frames never leave Reachy.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .platform_info import create_onnx_session

if TYPE_CHECKING:
    from numpy.typing import NDArray

_LOGGER = logging.getLogger(__name__)

GESTURE_MODEL_SHA256 = {
    "crops_classifier.onnx": "12a02344f63a7c4f2a2ca90f8740ca10a08c17b683b5585d73c3e88323056762",
    "hand_detector.onnx": "a8ef73d466b61a8e8677be9c47008b217a11d1b265d95e36bf2521ff93329af6",
}

_GESTURE_CLASSES = (
    "hand_down",
    "hand_right",
    "hand_left",
    "thumb_index",
    "thumb_left",
    "thumb_right",
    "thumb_down",
    "half_up",
    "half_left",
    "half_right",
    "half_down",
    "part_hand_heart",
    "part_hand_heart2",
    "fist_inverted",
    "two_left",
    "two_right",
    "two_down",
    "grabbing",
    "grip",
    "point",
    "call",
    "three3",
    "little_finger",
    "middle_finger",
    "dislike",
    "fist",
    "four",
    "like",
    "mute",
    "ok",
    "one",
    "palm",
    "peace",
    "peace_inverted",
    "rock",
    "stop",
    "stop_inverted",
    "three",
    "three2",
    "two_up",
    "two_up_inverted",
    "three_gun",
    "one_left",
    "one_right",
    "one_down",
)
_RECOGNIZED_GESTURES = frozenset(
    {
        "call",
        "dislike",
        "fist",
        "four",
        "like",
        "mute",
        "ok",
        "one",
        "palm",
        "peace",
        "peace_inverted",
        "rock",
        "stop",
        "stop_inverted",
        "three",
        "three2",
        "two_up",
        "two_up_inverted",
    }
)
_REACTIONS: dict[str, tuple[str, str]] = {
    "palm": ("emotion", "welcoming"),
    "peace": ("emotion", "excited"),
    "peace_inverted": ("emotion", "excited"),
    "rock": ("dance", "short"),
}


def reaction_for_gesture(gesture: str) -> tuple[str, str] | None:
    """Map a deliberately supported hand sign to one bounded semantic action."""
    return _REACTIONS.get(gesture.strip().lower())


class GestureReactionGate:
    """Confirm repeated detections and edge-trigger reactions with a cooldown."""

    def __init__(
        self,
        *,
        required_frames: int = 3,
        clear_frames: int = 2,
        cooldown_seconds: float = 8.0,
        min_confidence: float = 0.70,
    ) -> None:
        self.required_frames = max(2, int(required_frames))
        self.clear_frames = max(1, int(clear_frames))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self._candidate = ""
        self._candidate_frames = 0
        self._clear_frames = 0
        self._armed = True
        self._last_triggered_at = float("-inf")

    def reset(self) -> None:
        self._candidate = ""
        self._candidate_frames = 0
        self._clear_frames = 0
        self._armed = True

    def update(self, gesture: str, confidence: float, *, now: float) -> tuple[str, str] | None:
        normalized = gesture.strip().lower()
        reaction = reaction_for_gesture(normalized)
        if normalized in {"", "none", "no_gesture"}:
            self._candidate = ""
            self._candidate_frames = 0
            self._clear_frames += 1
            if self._clear_frames >= self.clear_frames:
                self._armed = True
            return None

        self._clear_frames = 0
        if reaction is None or float(confidence) < self.min_confidence:
            self._candidate = ""
            self._candidate_frames = 0
            return None
        if normalized != self._candidate:
            self._candidate = normalized
            self._candidate_frames = 1
        else:
            self._candidate_frames += 1
        if not self._armed or self._candidate_frames < self.required_frames:
            return None
        if float(now) - self._last_triggered_at < self.cooldown_seconds:
            return None
        self._armed = False
        self._last_triggered_at = float(now)
        return reaction


class GestureDetector:
    """Run the bundled HaGRID hand detector and crop classifier locally."""

    def __init__(self, models_directory: Path, *, providers: list[object] | None = None) -> None:
        import cv2

        self._cv2 = cv2
        self._models_directory = Path(models_directory)
        self._verify_models()
        chosen = providers if providers is not None else ["CPUExecutionProvider"]
        self._detector = create_onnx_session(self._models_directory / "hand_detector.onnx", chosen)
        self._classifier = create_onnx_session(self._models_directory / "crops_classifier.onnx", chosen)
        # The provider that actually runs the detector, after any CPU fallback.
        self.active_provider = str(self._detector.get_providers()[0])
        self._det_input = self._detector.get_inputs()[0].name
        self._det_outputs = [output.name for output in self._detector.get_outputs()]
        self._cls_input = self._classifier.get_inputs()[0].name
        shape = self._detector.get_inputs()[0].shape
        self._detector_size = (
            (int(shape[3]), int(shape[2]))
            if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int)
            else (320, 240)
        )
        self._classifier_size = (128, 128)
        self._mean = np.array([127, 127, 127], dtype=np.float32)
        self._std = np.array([128, 128, 128], dtype=np.float32)

    def _verify_models(self) -> None:
        for name, expected in GESTURE_MODEL_SHA256.items():
            path = self._models_directory / name
            if not path.is_file():
                raise RuntimeError(f"Gesture model is missing: {name}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Gesture model checksum mismatch: {name}")

    def _preprocess(self, frame: NDArray[np.uint8], size: tuple[int, int]) -> NDArray[np.float32]:
        image = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        image = self._cv2.resize(image, size)
        normalized = (image.astype(np.float32) - self._mean) / self._std
        return np.expand_dims(np.transpose(normalized, (2, 0, 1)), axis=0)

    def _detect_hands(self, frame: NDArray[np.uint8]) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        height, width = frame.shape[:2]
        outputs = self._detector.run(
            self._det_outputs,
            {self._det_input: self._preprocess(frame, self._detector_size)},
        )
        boxes = np.asarray(outputs[0], dtype=np.float32)
        scores = np.asarray(outputs[2], dtype=np.float32)
        if boxes.size == 0:
            return np.empty((0, 4), dtype=np.float32), np.empty((0,), dtype=np.float32)
        boxes[:, (0, 2)] *= width
        boxes[:, (1, 3)] *= height
        boxes[:, (0, 2)] = np.clip(boxes[:, (0, 2)], 0, width - 1)
        boxes[:, (1, 3)] = np.clip(boxes[:, (1, 3)], 0, height - 1)
        valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        return boxes[valid], scores[valid]

    @staticmethod
    def _square_crops(frame: NDArray[np.uint8], boxes: NDArray[np.float32]) -> list[NDArray[np.uint8]]:
        height, width = frame.shape[:2]
        crops: list[NDArray[np.uint8]] = []
        for box in boxes:
            x1, y1, x2, y2 = (int(value) for value in box)
            box_width, box_height = x2 - x1, y2 - y1
            side = max(box_width, box_height)
            x1 -= (side - box_width) // 2
            y1 -= (side - box_height) // 2
            x2, y2 = x1 + side, y1 + side
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            crop = frame[y1:y2, x1:x2]
            if crop.size:
                crops.append(crop)
        return crops

    def detect_jpeg(self, jpeg: bytes) -> tuple[str, float]:
        encoded = np.frombuffer(jpeg, dtype=np.uint8)
        frame = self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            raise RuntimeError("Gesture camera frame is not a decodable JPEG")
        return self.detect(frame)

    def detect(self, frame: NDArray[np.uint8]) -> tuple[str, float]:
        try:
            boxes, detector_scores = self._detect_hands(frame)
            crops = self._square_crops(frame, boxes)
            if not crops:
                return "no_gesture", 0.0
            processed = [self._preprocess(crop, self._classifier_size) for crop in crops]
            logits = self._classifier.run(None, {self._cls_input: np.concatenate(processed, axis=0)})[0]
            best_name = "no_gesture"
            best_combined = 0.0
            best_classifier = 0.0
            for logit, detector_score in zip(logits, detector_scores, strict=False):
                index = int(np.argmax(logit))
                exponentials = np.exp(logit - np.max(logit))
                classifier_score = float(exponentials[index] / np.sum(exponentials))
                combined = float(detector_score) * classifier_score
                if combined > best_combined:
                    candidate = _GESTURE_CLASSES[index] if index < len(_GESTURE_CLASSES) else "no_gesture"
                    best_name = candidate if candidate in _RECOGNIZED_GESTURES else "no_gesture"
                    best_combined = combined
                    best_classifier = classifier_score
            if best_combined < 0.25 or best_classifier < 0.50:
                return "no_gesture", 0.0
            return best_name, best_classifier
        except Exception:
            _LOGGER.warning("Gesture inference failed", exc_info=True)
            return "no_gesture", 0.0

    def close(self) -> None:
        self._detector = None
        self._classifier = None
