"""Open-vocabulary local keyword spotting for Homebody."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx
import numpy as np
import numpy.typing as npt

_LOGGER = logging.getLogger(__name__)

WAKE_PROMPT = "Say “Hey Hermes”, “Okay Nabu”, or “Hey Reachy”"
_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)
_MODEL_SHA256 = "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a"
_MODEL_DIRECTORY = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
_REQUIRED_FILES = {
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt",
}


def default_model_cache() -> Path:
    for env in ("HOMEBODY_MODEL_DIR", "REACHY_MINI_HERMES_MODEL_DIR"):
        root = os.getenv(env, "").strip()
        if root:
            return Path(root).expanduser()
    path = Path.home() / ".cache" / "homebody" / "kws"
    legacy = Path.home() / ".cache" / "reachy_mini_hermes" / "kws"
    # Reuse a model verified before the rename instead of downloading it again.
    if not path.exists() and legacy.exists():
        return legacy
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_ready(path: Path) -> bool:
    return all((path / filename).is_file() for filename in _REQUIRED_FILES)


def ensure_kws_model(cache_directory: Path | None = None) -> Path:
    """Download and safely extract the official Apache-2.0 KWS model."""
    cache = cache_directory or default_model_cache()
    model_path = cache / _MODEL_DIRECTORY
    if _model_ready(model_path):
        return model_path

    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reachy-hermes-kws-", dir=cache) as temporary_directory:
        temporary = Path(temporary_directory)
        archive = temporary / "model.tar.bz2"
        _LOGGER.info("Downloading the local keyword-spotting model from %s", _MODEL_URL)
        with httpx.stream("GET", _MODEL_URL, follow_redirects=True, timeout=120.0) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    output.write(chunk)
        actual_hash = _sha256(archive)
        if actual_hash != _MODEL_SHA256:
            raise RuntimeError(f"KWS model checksum mismatch: expected {_MODEL_SHA256}, got {actual_hash}")

        extracted = temporary / "extracted"
        extracted.mkdir()
        with tarfile.open(archive, "r:bz2") as bundle:
            for member in bundle.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError(f"Unsafe path in KWS archive: {member.name}")
                if member.isfile() and member_path.name in _REQUIRED_FILES:
                    source = bundle.extractfile(member)
                    if source is None:
                        raise RuntimeError(f"Could not read {member.name} from KWS archive")
                    destination = extracted / member_path.name
                    with destination.open("wb") as output:
                        shutil.copyfileobj(source, output)

        if not _model_ready(extracted):
            missing = sorted(name for name in _REQUIRED_FILES if not (extracted / name).exists())
            raise RuntimeError(f"KWS archive did not contain required files: {missing}")
        if model_path.exists():
            shutil.rmtree(model_path)
        extracted.replace(model_path)
    return model_path


class HeyHermesSpotter:
    """Streaming sherpa-onnx spotter for every app-bundled local wake phrase."""

    def __init__(
        self,
        model_directory: Path,
        keywords_file: Path,
        *,
        score: float = 1.5,
        threshold: float = 0.25,
    ) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError("sherpa-onnx is required for local wake-word detection") from exc

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model_directory / "tokens.txt"),
            encoder=str(model_directory / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(model_directory / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(model_directory / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(keywords_file),
            num_threads=1,
            keywords_score=score,
            keywords_threshold=threshold,
        )
        self._stream = self._spotter.create_stream()

    def accept(self, samples: npt.NDArray[np.float32], sample_rate: int = 16000) -> str | None:
        if samples.size == 0:
            return None
        self._stream.accept_waveform(sample_rate, np.ascontiguousarray(samples, dtype=np.float32))
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        if not result:
            return None
        self.reset()
        return str(result)

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)
