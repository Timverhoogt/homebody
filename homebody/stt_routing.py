"""One speech-recognition routing helper shared by Pipeline/Kids turns and contextual offers.

``stt_provider`` "robot" sends the in-RAM 16 kHz samples to the owned on-robot worker (no WAV,
no upload). Its optional ``stt_robot_fallback`` "host" may send the same samples to the Hermes
host once, requesting only local transcription, and only while the turn is still current; a
cloud or unconfirmed host receipt is discarded (fail closed). Every other provider keeps the
existing host path unchanged. Infrastructure failure is reported separately from silence.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .audio import encode_wav
from .config import AppConfig
from .robot_stt import RobotSttCancelled, RobotSttUnavailable

_LOCAL_STT_RECEIPTS = frozenset({"local", "local_command"})
_CLOUD_STT_RECEIPTS = frozenset({"openai", "groq", "mistral", "elevenlabs", "deepgram", "assemblyai", "azure"})
PROCESSING_LOCATIONS = frozenset({"robot", "host", "cloud"})
_LOGGER = logging.getLogger(__name__)


def receipt_locality(provider: object) -> str:
    """Categorical locality of one actual transcription receipt; anything else is 'unknown'."""
    value = str(provider or "").strip().casefold()
    if value in _LOCAL_STT_RECEIPTS:
        return "local"
    if value in _CLOUD_STT_RECEIPTS:
        return "cloud"
    return "unknown"


def host_processing_location(locality: str) -> str:
    """Where a host-bridge receipt says recognition ran: Hermes host, cloud or unknown."""
    return {"local": "host", "cloud": "cloud"}.get(locality, "unknown")


class SttInfrastructureError(RuntimeError):
    """Recognition could not run (not silence); callers must not enter the no-speech retry."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class SttTurnCancelled(RuntimeError):
    """The turn lost ownership during recognition; the result (if any) was discarded."""


@dataclass(frozen=True, slots=True)
class SttResult:
    text: str
    processing_location: str  # robot | host | cloud | unknown, from this request's receipt
    locality: str  # local | cloud | unknown (existing semantics)
    receipt: str


def robot_language(config: AppConfig) -> str:
    """Kids use the parent-selected profile language per turn; adults keep autodetect.

    This is transport only: whether the provisioned model honours ES/DE/FR/IT/PT is measured on the device.
    """
    if config.kids_mode_enabled and config.language in {"en", "nl", "es", "de", "fr", "it", "pt"}:
        return config.language
    return "auto"


def transcribe_endpoint(
    runtime: object,
    client: object,
    config: AppConfig,
    samples: np.ndarray,
    *,
    is_current: Callable[[], bool],
    session_id: str,
    generation: int,
    on_location: Callable[[str, bool], None] | None = None,
    on_submitted: Callable[[], None] | None = None,
) -> SttResult:
    """Recognize one endpointed utterance for an immutable turn snapshot.

    ``on_location(location, verified)`` reports pending intent (verified=False) and the actual
    receipt (verified=True). Raises ``SttTurnCancelled`` or ``SttInfrastructureError``; host
    bridge errors propagate unchanged for the non-robot providers.
    """
    report = on_location or (lambda _location, _verified: None)
    submitted = on_submitted or (lambda: None)
    if config.stt_provider != "robot":
        submitted()
        text = client.transcribe(encode_wav(samples, 16000))  # type: ignore[attr-defined]
        receipt = str(getattr(client, "last_stt_receipt", "") or "")
        locality = receipt_locality(receipt)
        return SttResult(text, host_processing_location(locality), locality, receipt)

    report("robot", False)
    robot = runtime._robot_stt_client()  # type: ignore[attr-defined]
    try:
        submitted()
        started = time.monotonic()
        receipt = robot.transcribe(
            samples,
            session=session_id,
            generation=generation,
            language=robot_language(config),
            cancelled=lambda: not is_current(),
        )
        round_trip_ms = (time.monotonic() - started) * 1000
    except RobotSttCancelled:
        raise SttTurnCancelled("cancelled") from None
    except RobotSttUnavailable as exc:
        category = exc.category
    else:
        if not is_current():
            raise SttTurnCancelled("late final")  # a late final never reaches the turn
        decode_ms = getattr(receipt, "decode_ms", None)
        if type(decode_ms) is not int or decode_ms < 0:
            decode_ms = "unknown"
        _LOGGER.info("Robot STT final: worker_decode_ms=%s round_trip_ms=%.1f", decode_ms, round_trip_ms)
        return SttResult(receipt.text, "robot", "local", "robot")

    if not is_current():
        raise SttTurnCancelled("stale failure")  # never surfaces in a switched or ended game
    if config.stt_robot_fallback != "host":
        raise SttInfrastructureError(category)
    # Fallback, at most once: only a host whose own health confirms local STT is asked, and the
    # actual receipt must agree; a contradictory receipt is discarded rather than used.
    try:
        health = client.health()  # type: ignore[attr-defined]
    except Exception:
        if not is_current():
            raise SttTurnCancelled("stale fallback health failure") from None
        raise SttInfrastructureError("host_fallback_failed") from None
    if not is_current():
        raise SttTurnCancelled("cancelled before fallback")
    if not isinstance(health, dict) or health.get("stt_locality") != "local":
        raise SttInfrastructureError("host_not_local")
    report("host", False)
    try:
        text = client.transcribe(encode_wav(samples, 16000), provider="local")  # type: ignore[attr-defined]
    except Exception:
        if not is_current():
            raise SttTurnCancelled("stale fallback transcription failure") from None
        raise SttInfrastructureError("host_fallback_failed") from None
    if not is_current():
        raise SttTurnCancelled("late final")
    receipt_name = str(getattr(client, "last_stt_receipt", "") or "")
    if receipt_locality(receipt_name) != "local":
        raise SttInfrastructureError("host_receipt_not_local")
    return SttResult(text, "host", "local", receipt_name)
