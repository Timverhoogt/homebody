"""Trusted static catalog of short Kids "thinking" acknowledgements played while STT is pending.

Trust boundary: these clips are app-owned categorical filler shipped inside the package, not
model output. Nothing here accepts caller-supplied text, audio, paths, voices or providers. A
clip is usable only if ALL of the following hold, otherwise its language simply has no filler
(there is never a live TTS fallback):

* ``manifest.json`` in this package's ``assets/kids_acknowledgements`` directory has exactly the
  keys ``schema_version`` (1), ``voice_sha256`` (SHA-256 of the voice the clips were generated
  with), ``model`` (the Kids Flash model), ``sample_rate`` and ``clips``. The digest is NOT trusted
  by itself: a turn may use the clips only when the bridge's current health proof of the
  effective Kids streaming voice equals it (``eligible_clips``);
* each entry has exactly ``id``, ``language``, ``text``, ``file``, ``sha256``,
  ``duration_seconds``; the language is one the bundle may hold (``en``/``nl`` for the original
  and Ruth bundles; only its own language for an optional ``es``/``de``/``fr``/``it``/``pt``
  bundle), the id is the file stem and starts with the language, and the text is one of the
  fixed content-free phrases below (no correctness,
  "heard"/"understood" claims, transcript, nickname or target);
* each file name is a plain ``[a-z0-9-]+.wav`` basename, the path is a regular non-symlink file
  inside the directory, at most 256 KiB, and its SHA-256 equals the manifest digest;
* the WAV is mono 16-bit PCM at the manifest rate, 0.3-2.5 seconds long and within 20 ms of the
  declared duration.

Every generated answer keeps its existing input+output moderation and exact-text approval; this
catalog never carries generated text, so it never needs or bypasses those approvals.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import stat
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .kids_mode import KIDS_TTS_MODEL

_LOGGER = logging.getLogger("homebody.runtime")

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "kids_acknowledgements"
_PHRASES = {
    "en": ("Let me think", "Hmm", "Let's see"),
    "nl": ("Even denken", "Hmm", "Eens kijken"),
    "es": ("Un momento", "Mmm", "A ver"),
    "de": ("Lass mich überlegen", "Hmm", "Mal sehen"),
    "fr": ("Laisse-moi réfléchir", "Hmm", "Voyons voir"),
    "it": ("Fammi pensare", "Mmm", "Vediamo"),
    "pt": ("Deixa-me pensar", "Hmm", "Vamos ver"),
}
# ES/DE/FR/IT/PT clips may only come from their own fixed optional bundle directory; none is
# shipped until the owner selects a voice and generates/verifies clips, so those turns are silent.
SESSION_BUNDLE_LANGUAGES = ("es", "de", "fr", "it", "pt")
ALLOWED_PHRASES = {
    language: frozenset(f"{phrase}{end}" for phrase in phrases for end in (".", "…"))
    for language, phrases in _PHRASES.items()
}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def eligible_clips(clips: tuple[AckClip, ...], voice_proof: object, model_proof: object) -> tuple[AckClip, ...]:
    """Clips usable for this turn: only with the host's current Kids voice proof and Flash model.

    The proof comes from the bridge's health, derived from the same resolver that selects the
    actual Kids streaming voice. Missing, malformed or mismatching proof => no filler. The
    packaged default digest is never treated as proof on its own.
    """
    if model_proof != KIDS_TTS_MODEL or not isinstance(voice_proof, str) or not _HEX64.match(voice_proof):
        return ()
    return tuple(clip for clip in clips if clip.voice_sha256 == voice_proof)


_FILE_NAME = re.compile(r"[a-z0-9-]{1,64}\.wav\Z")
_MAX_FILE_BYTES = 256 * 1024
_MAX_CLIPS = 12
_SAMPLE_RATES = frozenset({16000, 22050, 24000, 44100, 48000})
_MIN_SECONDS, _MAX_SECONDS = 0.3, 2.5


@dataclass(frozen=True, slots=True)
class AckClip:
    """One verified clip; ``samples`` is mono float32 PCM at ``sample_rate``."""

    language: str
    name: str
    samples: np.ndarray
    sample_rate: int
    voice_sha256: str = ""  # from the manifest; usable only if the host's current proof matches
    text: str = ""  # one of ALLOWED_PHRASES; selects the wait stage, never spoken or logged

    @property
    def duration(self) -> float:
        return self.samples.size / float(self.sample_rate)

    @property
    def stage(self) -> str:
        """'see' for the fixed looking phrase ("Eens kijken."/"Let's see."), otherwise 'think'."""
        phrases = _PHRASES.get(self.language, ())
        return "see" if len(phrases) > 2 and self.text.rstrip(".…") == phrases[2] else "think"


def stage_clips(clips: tuple[AckClip, ...], stage: str | None) -> tuple[AckClip, ...]:
    """Clips for one wait stage; ``None`` keeps the original STT behaviour (any phrase)."""
    return tuple(clips) if stage is None else tuple(clip for clip in clips if clip.stage == stage)


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
            raise ValueError("clip must be mono 16-bit PCM")
        rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    if rate not in _SAMPLE_RATES:
        raise ValueError("unsupported clip sample rate")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if not _MIN_SECONDS <= samples.size / rate <= _MAX_SECONDS:
        raise ValueError("clip duration outside bounds")
    return samples, rate


def load_catalog(
    directory: Path = ASSET_DIR, languages: tuple[str, ...] = ("en", "nl"),
) -> dict[str, tuple[AckClip, ...]]:
    """Verify the manifest and clips; any problem yields no clips (fail closed, categorical log).

    ``languages`` are the only clip languages this bundle may contain.
    """
    try:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("asset directory is not a directory")
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "voice_sha256", "model", "sample_rate", "clips"}
            or manifest["schema_version"] != 1
            or not isinstance(manifest["voice_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["voice_sha256"])
            or manifest["model"] != KIDS_TTS_MODEL
            or manifest["sample_rate"] not in _SAMPLE_RATES
            or not isinstance(manifest["clips"], list)
            or not 1 <= len(manifest["clips"]) <= _MAX_CLIPS
        ):
            raise ValueError("manifest does not match the Kids voice catalog contract")
        catalog: dict[str, list[AckClip]] = {language: [] for language in languages}
        seen: set[str] = set()
        for entry in manifest["clips"]:
            keys = {"id", "language", "text", "file", "sha256", "duration_seconds"}
            if not isinstance(entry, dict) or set(entry) != keys:
                raise ValueError("invalid manifest entry")
            language, text, name, digest, clip_id = (
                str(entry[key]) for key in ("language", "text", "file", "sha256", "id")
            )
            if language not in catalog or text not in ALLOWED_PHRASES[language]:
                raise ValueError("clip phrase is not in the fixed catalog")
            if (
                not _FILE_NAME.fullmatch(name)
                or name in seen
                or clip_id != name[:-4]
                or not clip_id.startswith(f"{language}-")
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError("invalid clip file entry")
            seen.add(name)
            path = directory / name
            file_info = os.lstat(path)
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_size > _MAX_FILE_BYTES:
                raise ValueError("clip is not a bounded regular file")
            data = path.read_bytes()
            if len(data) > _MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("clip digest mismatch")
            samples, rate = _decode_wav(data)
            declared = entry["duration_seconds"]
            if (
                rate != manifest["sample_rate"]
                or isinstance(declared, bool)
                or not isinstance(declared, (int, float))
                or abs(samples.size / rate - float(declared)) > 0.02
            ):
                raise ValueError("clip metadata does not match its audio")
            catalog[language].append(AckClip(language, name, samples, rate, manifest["voice_sha256"], text))
        return {language: tuple(clips) for language, clips in catalog.items()}
    except FileNotFoundError:
        _LOGGER.info("Kids acknowledgement clips are not installed; no thinking filler")
    except Exception as exc:  # categorical only: never log paths, text or bytes
        _LOGGER.warning("Kids acknowledgement catalog rejected (%s); no thinking filler", type(exc).__name__)
    return {language: () for language in languages}


ACK_DELAY_SECONDS = 1.0  # only when STT is genuinely still pending after a noticeable pause
# Waiting speech beyond STT: one clip only if the I Spy camera search / backend reply is still
# pending after these delays (read at arm time so tests can shorten them).
SEARCH_WAIT_DELAY_SECONDS = 1.0
REPLY_WAIT_DELAY_SECONDS = 1.5
ACK_COOLDOWN_SECONDS = 8.0
_CHUNK_SECONDS = 0.1


class AckSelector:
    """Sparse, bounded variation: never the same clip twice in a row, at most one per cooldown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_name = ""
        self._last_at = float("-inf")

    def choose(self, clips: tuple[AckClip, ...], now: float) -> AckClip | None:
        with self._lock:
            if not clips or now - self._last_at < ACK_COOLDOWN_SECONDS:
                return None
            options = [clip for clip in clips if clip.name != self._last_name] or list(clips)
            clip = options[0]
            self._last_name, self._last_at = clip.name, now
            return clip


SETTLE_JOIN_SECONDS = 0.5


def output_quarantined(owner: object, *, search_owner: object | None = None) -> bool:
    """Block new voice admission while a search retains output or an old SDK push is blocked.

    A search owns a future final flush even after its clip thread exits. Only that exact internal
    lease may arm its own filler; ordinary dispatchers/engines always use the default gate.
    """
    search = getattr(owner, "_kids_search_audio_owner", None)
    if search is not None and search is not search_owner:
        return True
    producer = getattr(owner, "_kids_ack_producer", None)
    return producer is not None and bool(producer.inflight)


_install_lock = threading.Lock()


def install_producer(owner: object, player: KidsAckPlayer) -> bool:
    """Make ``player`` the owner's single filler producer and start it, or refuse.

    Refused while the previous producer is still running (in flight, playing, watching its
    queued audio or resolving a proof): a newer producer never replaces a live old owner, so
    ``output_quarantined`` always observes the one producer that may still push or clear.
    """
    with _install_lock:
        previous = getattr(owner, "_kids_ack_producer", None)
        if previous is not None and previous.running:
            return False
        owner._kids_ack_producer = player  # type: ignore[attr-defined]
        player.start()
        return True


class KidsAckPlayer:
    """Plays at most one delayed clip for one STT wait; cancellable, with bounded settle.

    ``is_current`` must compare immutable snapshots (exact Kids session, original turn stop
    generation) and live privacy/power/shutdown state; it is checked before playback, before and
    after every PCM chunk and while queued audio is still audible. The player never opens the
    microphone, never touches motors and never resets cancellation state.

    SDK limits, stated honestly: ``push_audio_sample``/``clear_player`` are opaque SDK calls and
    Python cannot forcibly cancel a thread blocked inside one. Therefore no lock is ever held
    across them; ``inflight`` covers pushes, every outstanding clear, and admitted producer threads
    that can still perform a late clear after settlement. A bounded join does not discharge an
    outstanding flush obligation. A failed clear leaves output quarantined for runtime recovery.
    ``settle`` returns after a bounded join plus its own opaque clear; if producer work survives it
    returns False and the caller must keep output quarantined (no answer, re-listen or announcement).
    No newer audio is admitted until the old push, pending watcher and all clears are quiescent.
    """

    def __init__(self, *, clips, selector, push, clear, is_current, resample, output_rate, clock, delay=None):
        self._clips = clips
        self._selector = selector
        self._push = push
        self._clear = clear
        self._is_current = is_current
        self._resample = resample
        self._output_rate = output_rate
        self._clock = clock
        self._delay = ACK_DELAY_SECONDS if delay is None else delay
        self._stt_done = threading.Event()
        self._settled = threading.Event()
        self._lock = threading.Lock()  # guards state only; never held across SDK calls
        self._inflight = False
        self._clears_inflight = 0  # every opaque clear, including the queued-audio watcher
        self._clear_failed = False  # unknown output state fails closed until runtime recovery
        self._admitted = False  # at least one push was entered (its audio may be queued)
        self.queued_until = 0.0  # estimate only, used solely to keep watching for cancellation
        self.first_enqueue_at: float | None = None
        self.played: str = ""
        self._thread = threading.Thread(target=self._run, name="kids-stt-ack", daemon=True)

    @property
    def inflight(self) -> bool:
        with self._lock:
            return bool(
                self._inflight or self._clears_inflight or self._clear_failed
                or (self._settled.is_set() and self._admitted and self._thread.is_alive())
            )

    def _clear_output(self) -> None:
        """Keep quarantine until this exact clear returns; never hold a lock across the SDK."""
        with self._lock:
            self._clears_inflight += 1
        try:
            self._clear()
        except Exception:
            with self._lock:
                self._clear_failed = True
            _LOGGER.warning("Kids filler audio clear failed; output remains quarantined")
        finally:
            with self._lock:
                self._clears_inflight -= 1

    @property
    def running(self) -> bool:
        """True until the producer thread has exited and no push is in flight."""
        return self.inflight or self._thread.is_alive()

    @property
    def clip_started(self) -> bool:
        """At least one PCM push was entered for this wait (actual call, not an estimate)."""
        with self._lock:
            return self._admitted

    def start(self) -> KidsAckPlayer:
        self._thread.start()
        return self

    def _cancelled(self) -> bool:
        return self._settled.is_set() or not self._is_current()

    def _run(self) -> None:
        if self._stt_done.wait(self._delay):
            return  # fast turn: no filler
        if self._stt_done.is_set() or self._cancelled():
            return
        # A callable resolves clips only now (after the delay, off the answer path), e.g. from a
        # fresh bridge proof of the effective Kids voice; any failure yields no clips.
        try:
            clips = self._clips() if callable(self._clips) else self._clips
        except Exception:
            clips = ()
        if not clips or self._stt_done.is_set() or self._cancelled():
            return
        clip = self._selector.choose(tuple(clips), self._clock())
        if clip is None:
            return
        pcm = self._resample(clip.samples, clip.sample_rate, self._output_rate)
        step = max(1, int(self._output_rate * _CHUNK_SECONDS))
        for offset in range(0, pcm.size, step):
            chunk = pcm[offset : offset + step]
            with self._lock:
                # A wait that finished after selection but before the first push stays silent.
                if self._cancelled() or (not self._admitted and self._stt_done.is_set()):
                    break
                self._inflight = True
                self._admitted = True
                if self.first_enqueue_at is None:
                    self.first_enqueue_at = self._clock()
                    self.played = clip.name
            try:
                self._push(chunk)  # opaque SDK call: no lock held
            finally:
                cancelled_after = self._cancelled()
                if cancelled_after:
                    # A chunk admitted after Stop/End/privacy/replacement/expiry/settle is flushed
                    # while still marked in flight, so nothing newer can be playing yet.
                    self._clear_output()
                with self._lock:
                    now = self._clock()
                    self.queued_until = max(self.queued_until, now) + chunk.size / float(self._output_rate)
                    self._inflight = False
            if cancelled_after:
                return
        # Stop/End/privacy must also preempt audio that is already queued in the player.
        while not self._settled.is_set():
            with self._lock:
                watching = self._admitted and self._clock() < self.queued_until
            if not watching:
                return
            if not self._is_current():
                self._clear_output()
                return
            self._settled.wait(0.02)

    def stt_finished(self) -> None:
        """STT returned: never start a clip now; one already queued may finish during the reply request."""
        self._stt_done.set()

    # The same contract for every wait (camera search, backend reply): the work it covered is done.
    wait_finished = stt_finished

    def settle(self) -> bool:
        """Before reply speech, a re-listen or exit: stop, bounded join, flush anything admitted.

        Returns True when the producer is quiesced. False means a push is still blocked in the
        SDK: the caller must keep audio output quarantined (see class docstring).
        """
        self._stt_done.set()
        self._settled.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=SETTLE_JOIN_SECONDS)
        with self._lock:
            admitted = self._admitted
        if admitted:
            # Decided from actual pushes, not from an estimated deadline.
            self._clear_output()
        # Re-read after our clear: another producer clear or pending watcher may still survive
        # the bounded join. Its complete obligation, not just a sampled push, owns quarantine.
        return not self.inflight


_catalog_lock = threading.Lock()
_catalog: dict[str, tuple[AckClip, ...]] | None = None


def catalog() -> dict[str, tuple[AckClip, ...]]:
    """Verify fixed packaged voice bundles once; every clip still requires a matching turn proof."""
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = load_catalog()
            # Fixed app-owned bundle only: no caller paths, live synthesis or weakened manifests.
            dutch = load_catalog(ASSET_DIR / "ruth-nl")
            _catalog = {language: clips + dutch[language] for language, clips in _catalog.items()}
            for language in SESSION_BUNDLE_LANGUAGES:
                _catalog.update(load_catalog(ASSET_DIR / language, (language,)))
        return _catalog
