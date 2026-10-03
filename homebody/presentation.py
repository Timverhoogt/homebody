from __future__ import annotations

import io
import threading

import numpy as np
from PIL import Image, UnidentifiedImageError

_MAX_JPEG_BYTES = 1_000_000
_SAMPLE_SIZE = (64, 48)


class IntentionalPresentationGate:
    """Local, non-semantic change gate for an explicitly started presentation window.

    Only small grayscale feature arrays are retained for stability comparison. JPEG bytes
    are decoded in memory and discarded by the caller after each observation.
    """

    def __init__(
        self,
        *,
        required_stable_frames: int = 3,
        changed_pixel_threshold: int = 35,
        changed_ratio_threshold: float = 0.18,
        candidate_similarity_threshold: float = 12.0,
    ) -> None:
        if not 2 <= required_stable_frames <= 6:
            raise ValueError("required_stable_frames must be between 2 and 6")
        self.required_stable_frames = required_stable_frames
        self.changed_pixel_threshold = changed_pixel_threshold
        self.changed_ratio_threshold = changed_ratio_threshold
        self.candidate_similarity_threshold = candidate_similarity_threshold
        self._lock = threading.RLock()
        self._baseline: np.ndarray | None = None
        self._candidate: np.ndarray | None = None
        self._stable_frames = 0
        self.detected = False

    @staticmethod
    def _features(jpeg: bytes) -> np.ndarray:
        if len(jpeg) > _MAX_JPEG_BYTES:
            raise ValueError("JPEG exceeds the 1 MB size limit")
        if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise ValueError("invalid JPEG")
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                if image.format != "JPEG":
                    raise ValueError("invalid JPEG")
                if image.width < 16 or image.height < 16 or image.width > 4096 or image.height > 4096:
                    raise ValueError("JPEG dimensions are outside the local presentation limit")
                if image.width * image.height > 12_000_000:
                    raise ValueError("JPEG dimensions are outside the local presentation limit")
                grayscale = image.convert("L").resize(_SAMPLE_SIZE)
                pixels = np.asarray(grayscale, dtype=np.int16)
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("invalid JPEG") from exc
        height, width = pixels.shape
        # Use the central half of the frame. An explicit user-started window plus
        # repeated central stability is the intentional-presentation signal.
        return pixels[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4].copy()

    def begin(self, baseline_jpeg: bytes) -> None:
        features = self._features(baseline_jpeg)
        with self._lock:
            self._baseline = features
            self._candidate = None
            self._stable_frames = 0
            self.detected = False

    def observe(self, jpeg: bytes) -> bool:
        current = self._features(jpeg)
        with self._lock:
            if self._baseline is None:
                raise RuntimeError("presentation gate is not active")
            if self.detected:
                return True
            difference = np.abs(current - self._baseline)
            changed_ratio = float(np.mean(difference >= self.changed_pixel_threshold))
            if changed_ratio < self.changed_ratio_threshold:
                self._candidate = None
                self._stable_frames = 0
                return False
            if self._candidate is None:
                self._candidate = current
                self._stable_frames = 1
                return False
            candidate_delta = float(np.mean(np.abs(current - self._candidate)))
            if candidate_delta <= self.candidate_similarity_threshold:
                self._stable_frames += 1
            else:
                self._candidate = current
                self._stable_frames = 1
            if self._stable_frames >= self.required_stable_frames:
                self.detected = True
            return self.detected

    def clear(self) -> None:
        with self._lock:
            self._baseline = None
            self._candidate = None
            self._stable_frames = 0
            self.detected = False
