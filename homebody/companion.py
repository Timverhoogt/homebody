"""Deterministic companion coordinator and direct visual help for the voice runtime.

``CompanionMixin`` is mixed into ``HermesVoiceRuntime``. It is not a model loop: every explicit
look is admitted as a ticket that freezes the original Stop generation, capability profile,
camera permission, voice ownership and (for phone requests) the owner session, and that ticket is
re-validated after every blocking step. One explicit look permits exactly one capture. Only a
bounded text-only analysis stays in RAM, torn down by Stop/privacy/Kids/profile change or TTL.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .config import AppConfig
from .local_vision import LocalVisionError
from .look_intent import looks_like_look_request
from .safety_gate import CAMERA_CAPTURE_POLICY

__all__ = ["CompanionCoordinator", "CompanionMixin", "CompanionTicket", "LookContext", "looks_like_look_request"]

_LOGGER = logging.getLogger("homebody.runtime")

Activity = Literal["quiet", "acknowledged", "listening", "looking", "replying", "follow_up", "unavailable"]
LookOrigin = Literal["phone", "voice", "offer"]

LOOK_CONTEXT_TTL_SECONDS = 120.0
LOOK_MAX_FOLLOW_UPS = 3
LOOK_MAX_QUESTION_CHARS = 500
PRIVATE_DETAIL_TTL_SECONDS = 900.0
# Public-safe failure categories; raw provider/camera messages never leave the runtime log.
FAILURE_CATEGORIES = frozenset(
    {
        "",
        "cancelled",
        "blocked",
        "busy",
        "camera_unavailable",
        "vision_unavailable",
        "empty_answer",
        "bridge_unavailable",
    }
)


class LookCancelled(RuntimeError):
    """The original request lost its authority (Stop, privacy, Kids, profile, owner, camera permission)."""


@dataclass(frozen=True, slots=True)
class CompanionTicket:
    token: int
    kind: str
    origin: str
    stop_generation: int
    capability_profile: str
    admitted_at: float
    # Route-bound owner/session validation for phone requests; None for voice/offer paths.
    authority: Callable[[], bool] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class CompanionAuthority:
    """Immutable admitting scope captured before preflight, locks or worker scheduling."""

    identity: object
    origin: str
    check: Callable[[], bool] = field(repr=False, compare=False)
    camera_deadline: float = 0.0

    def __call__(self) -> bool:
        return self.check()


class CompanionCoordinator:
    """Compact interaction state plus text-free receipts; validity is checked against live runtime state."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._generation = 0
        self._epoch = 0
        self._current: CompanionTicket | None = None
        self.activity: Activity = "quiet"
        self._receipts: deque[dict[str, object]] = deque(maxlen=12)

    def _record(self, event: str, detail: str) -> None:
        self._receipts.append({"at": round(self._clock(), 1), "event": event, "detail": detail[:48]})

    def admit(
        self,
        kind: str,
        *,
        origin: str,
        stop_generation: int,
        capability_profile: str = "",
        authority: Callable[[], bool] | None = None,
    ) -> CompanionTicket:
        with self._lock:
            self._generation += 1
            ticket = CompanionTicket(
                self._generation, kind, origin, int(stop_generation), capability_profile, self._clock(), authority
            )
            self._current = ticket
            self.activity = "looking" if kind == "look" else "replying"
            self._record("admitted", f"{kind}:{origin}")
            return ticket

    def is_current(
        self,
        ticket: CompanionTicket,
        *,
        stop_generation: int,
        privacy: bool,
        power_mode: str,
        stop_event: bool = False,
        capability_profile: str | None = None,
    ) -> bool:
        with self._lock:
            current = ticket.token == self._generation
        return (
            current
            and ticket.stop_generation == int(stop_generation)
            and not privacy
            and not stop_event
            and power_mode not in {"meeting", "sleep"}
            and (capability_profile is None or capability_profile == ticket.capability_profile)
        )

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self._generation += 1
            self._epoch += 1
            self._current = None
            self.activity = "quiet"
            self._record("invalidated", reason)

    def finish(self, ticket: CompanionTicket, outcome: str) -> None:
        with self._lock:
            if ticket.token == self._generation:
                self._current = None
                self.activity = "quiet"
            self._record("finished", f"{ticket.kind}:{outcome}")

    def current_token(self) -> int:
        with self._lock:
            return self._generation

    def current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def public_status(self) -> dict[str, object]:
        with self._lock:
            return {"activity": self.activity, "receipts": list(self._receipts)}


@dataclass(slots=True)
class LookContext:
    """Bounded text-only analysis from the last explicit look; never image bytes."""

    question: str
    answer: str
    ticket_token: int
    created_at: float
    expires_at: float
    follow_ups: int = 0
    capture_token: int = 0
    authority: CompanionAuthority | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls,
        question: str,
        answer: str,
        *,
        ticket_token: int,
        now: float,
        ttl_seconds: float = LOOK_CONTEXT_TTL_SECONDS,
    ) -> LookContext:
        return cls(
            question[:LOOK_MAX_QUESTION_CHARS],
            answer[:1200],
            ticket_token,
            now,
            now + float(ttl_seconds),
            capture_token=ticket_token,
        )

    def live(self, *, now: float, current_token: int) -> bool:
        return now < self.expires_at and self.ticket_token == current_token and self.follow_ups < LOOK_MAX_FOLLOW_UPS


def _clean_question(question: str) -> str:
    clean = " ".join(str(question or "").split())
    if not 1 <= len(clean) <= LOOK_MAX_QUESTION_CHARS:
        raise ValueError("Look question must be between 1 and 500 characters")
    return clean


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, LookCancelled):
        return "cancelled"
    if isinstance(exc, LocalVisionError):
        return "vision_unavailable"
    name = type(exc).__name__
    if name == "HermesBridgeError":
        return "bridge_unavailable"
    text = str(exc).lower()
    if "camera" in text:
        return "camera_unavailable"
    if "empty answer" in text:
        return "empty_answer"
    return "blocked"


class CompanionMixin:
    """Direct looking, companion status and a companion Stop that keeps durable project work."""

    def _init_companion_state(self) -> None:
        self._companion = CompanionCoordinator()
        self._look_lock = threading.Lock()
        self._look_context: LookContext | None = None
        self._look_context_lock = threading.RLock()
        self._look_last_failure = ""
        self._look_last_failure_token = 0
        self._camera_in_use = False
        self._private_details_lock = threading.RLock()
        self._offer_epoch = 0
        self._private_details: list[dict[str, object]] = []
        self._companion_timers: list[threading.Timer] = []

    # --- fences -------------------------------------------------------------------------------

    def _companion_snapshot(self) -> tuple[int, str]:
        with self._conversation_stop_generation_lock:
            stop_generation = self._conversation_stop_generation
        with self._agent_lock:
            profile = str(self._capability_profile)
        return stop_generation, profile

    def _look_camera_permitted(self, config: AppConfig) -> bool:
        # The retired Everyday flag and a session window cannot override the owner switch.
        return bool(config.camera_enabled)

    def _companion_authority(
        self,
        origin: str,
        authority: Callable[[], bool] | None = None,
    ) -> CompanionAuthority:
        """Freeze the caller's scope now, never later on a worker or after blocking work."""
        camera_generation = getattr(self, "_everyday_camera_generation", 0)
        camera_deadline = 0.0
        camera_lock = getattr(self, "_everyday_lock", None)
        if camera_lock is not None:
            with camera_lock:
                camera_generation = getattr(self, "_everyday_camera_generation", 0)
                window = getattr(self, "_everyday_camera_until", 0.0)
                if window > time.monotonic():
                    camera_deadline = window
        original_deadline = authority.camera_deadline if isinstance(authority, CompanionAuthority) else 0.0
        if original_deadline:
            camera_deadline = min(camera_deadline, original_deadline) if camera_deadline else original_deadline
        snapshot = self._companion_snapshot()
        with self._agent_lock:
            agent_generation = self._agent_session_generation
        epoch = self._companion.current_epoch()
        config = self.config_loader()
        route = (
            config.camera_enabled,
            config.local_vision_enabled,
            config.local_vision_url,
            config.local_vision_model,
            config.bridge_url,
            config.api_key,
        )
        identity = getattr(authority, "identity", authority)

        def current() -> bool:
            if epoch != self._companion.current_epoch() or snapshot != self._companion_snapshot():
                return False
            with self._agent_lock:
                if agent_generation != self._agent_session_generation:
                    return False
            live = self.config_loader()
            if (
                not self._look_camera_permitted(live)
                or camera_generation != getattr(self, "_everyday_camera_generation", 0)
                or (camera_deadline and time.monotonic() >= camera_deadline)
                or route
                != (
                    live.camera_enabled,
                    live.local_vision_enabled,
                    live.local_vision_url,
                    live.local_vision_model,
                    live.bridge_url,
                    live.api_key,
                )
            ):
                return False
            if self.stop_event.is_set() or self._privacy_requested.is_set():
                return False
            if self._effective_power_mode() in {"meeting", "sleep"}:
                return False
            with self._kids_lock:
                if self._kids_active or self._kids_locked:
                    return False
            if origin == "voice" and (not self._voice_activity_lock.locked() or self._turn_stop_requested()):
                return False
            try:
                return authority is None or bool(authority())
            except Exception:
                return False

        return CompanionAuthority(identity, origin, current, camera_deadline)

    def _companion_result_current(self, result: dict[str, object], authority: CompanionAuthority) -> bool:
        return authority() and result.get("request_token") == self._companion.current_token()

    def _companion_ticket_current(self, ticket: CompanionTicket, *, config: AppConfig | None = None) -> bool:
        """Every authority the original request was admitted under must still hold."""
        stop_generation, profile = self._companion_snapshot()
        if not self._companion.is_current(
            ticket,
            stop_generation=stop_generation,
            privacy=self._privacy_requested.is_set(),
            power_mode=self._effective_power_mode(),
            stop_event=self.stop_event.is_set(),
            capability_profile=profile,
        ):
            return False
        if ticket.kind == "look":
            live_config = self.config_loader()
            if not self._look_camera_permitted(live_config):
                return False
            with self._kids_lock:
                if self._kids_active or self._kids_locked:
                    return False
            if self._safety_gate.block_reason(CAMERA_CAPTURE_POLICY):
                return False
        if ticket.origin == "voice" and not self._voice_activity_lock.locked():
            return False
        if ticket.authority is not None:
            try:
                if not ticket.authority():
                    return False
            except Exception:
                _LOGGER.debug("Owner authority check failed closed", exc_info=True)
                return False
        return True

    def _require_current(self, ticket: CompanionTicket, stage: str) -> None:
        if not self._companion_ticket_current(ticket):
            raise LookCancelled(f"Look was cancelled {stage}")

    def _look_block_reason(self, config: AppConfig) -> str:
        if self.stop_event.is_set():
            return "Reachy is stopping"
        if not self._look_camera_permitted(config):
            return "Camera access is disabled in Reachy settings"
        with self._kids_lock:
            if self._kids_active or self._kids_locked:
                return "Looking is unavailable during a Kids session"
        reason = self._safety_gate.block_reason(CAMERA_CAPTURE_POLICY)
        if reason:
            return reason
        with self._camera_control_lock:
            if self._camera_control_session_live_locked():
                return "Camera control is active; stop it before asking Reachy to look"
        if not config.local_vision_enabled and not config.household_configured:
            return "Connect the Hermes bridge or enable local vision before asking Reachy to look"
        return ""

    def _schedule_companion_timer(self, seconds: float, callback: Callable[[], None]) -> None:
        timer = threading.Timer(seconds, callback)
        timer.daemon = True
        self._companion_timers = [item for item in self._companion_timers if item.is_alive()][-8:]
        self._companion_timers.append(timer)
        timer.start()

    def _expire_look_context(self, token: int) -> None:
        with self._look_context_lock:
            context = self._look_context
            if context is not None and token in {context.ticket_token, context.capture_token}:
                self._look_context = None

    def _companion_teardown(self, reason: str) -> None:
        """Clear every private companion item from RAM (Stop, privacy, Kids, profile change, owner loss)."""
        self._companion.invalidate(reason)
        with self._look_context_lock:
            self._look_context = None
        with self._private_details_lock:
            self._offer_epoch += 1
            self._private_details = []
        for timer in self._companion_timers:
            timer.cancel()
        self._companion_timers = []

    # --- the look -----------------------------------------------------------------------------

    def _end_companion_voice(self) -> None:
        """Close the voice look scope without erasing accepted private source deliveries."""
        self._companion.invalidate("voice_session_ended")
        with self._look_context_lock:
            self._look_context = None

    def look_at_this(
        self,
        question: str,
        *,
        origin: LookOrigin = "phone",
        authority: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """One explicit request -> one capture -> bounded interpretation -> text answer.

        ``origin="voice"`` must be called from the voice turn that owns the voice slot; ``phone``
        and ``offer`` acquire the voice slot themselves so no spoken turn, announcement or
        presence motion can run concurrently with the capture.
        """
        guard = self._companion_authority(origin, authority)
        clean = _clean_question(question)
        if origin not in {"phone", "voice", "offer"}:
            raise ValueError("Unsupported look origin")
        reason = self._look_block_reason(self.config_loader())
        if reason:
            raise RuntimeError(reason)
        if origin == "voice" and not self._voice_activity_lock.locked():
            raise RuntimeError("A spoken look needs the active voice turn")
        if not guard():
            raise LookCancelled("The original look request is no longer authorized")
        owns_voice_slot = False
        if origin == "voice":
            if not self._voice_activity_lock.locked():
                raise RuntimeError("A spoken look needs the active voice turn")
        else:
            if not self._voice_activity_lock.acquire(blocking=False):
                raise RuntimeError("Reachy is busy with a voice interaction; try again in a moment")
            owns_voice_slot = True
        try:
            if not self._look_lock.acquire(blocking=False):
                raise RuntimeError("Reachy is already looking; wait for that answer first")
            try:
                if not guard():
                    raise LookCancelled("The original look request is no longer authorized")
                stop_generation, profile = self._companion_snapshot()
                ticket = self._companion.admit(
                    "look",
                    origin=origin,
                    stop_generation=stop_generation,
                    capability_profile=profile,
                    authority=guard,
                )
                return self._run_look(ticket, clean)
            finally:
                self._look_lock.release()
        finally:
            if owns_voice_slot:
                self._voice_activity_lock.release()

    def _run_look(self, ticket: CompanionTicket, question: str) -> dict[str, object]:
        config = self.config_loader()
        try:
            reason = self._look_block_reason(config)
            if reason:
                raise RuntimeError(reason)
            self._require_current(ticket, "before capture")
            with self._look_context_lock:
                if self._look_context is not None and self._look_context.ticket_token < ticket.token:
                    self._look_context = None  # a new look replaces earlier analysis
            self._camera_in_use = True
            try:
                jpeg = self._capture_camera_jpeg(
                    is_current=lambda: self._companion_ticket_current(ticket),
                    adult_current=(lambda: self._require_current(ticket, "during temporary camera capture"))
                    if not config.camera_enabled
                    else None,
                )
            finally:
                self._camera_in_use = False
            with self._status_lock:
                self._status.camera_captures += 1
                self._status.camera_last_error = ""
            # Camera permission, owner, Stop, Kids or profile may have changed during the capture:
            # never upload a frame the request is no longer entitled to.
            if not self._companion_ticket_current(ticket):
                jpeg = b""
                raise LookCancelled("Look was cancelled before analysis")
            try:
                result = self._analyze_look(jpeg, question, config, ticket=ticket)
            finally:
                jpeg = b""
            self._require_current(ticket, "after analysis; the answer was discarded")
            answer = " ".join(str(result.get("answer") or "").split())
            if not answer:
                raise RuntimeError("The vision model returned an empty answer")
            now = time.monotonic()
            self._require_current(ticket, "before retaining its answer")
            context_ttl = LOOK_CONTEXT_TTL_SECONDS
            camera_deadline = (
                ticket.authority.camera_deadline if isinstance(ticket.authority, CompanionAuthority) else 0.0
            )
            if camera_deadline:
                context_ttl = min(context_ttl, max(0.0, camera_deadline - now))
            with self._look_context_lock:
                # Never acquire Kids/power locks beneath the context lock.
                if ticket.token != self._companion.current_token():
                    raise LookCancelled("Look was cancelled before its answer could be kept")
                self._look_context = LookContext.create(question, answer, ticket_token=ticket.token, now=now)
                self._look_context.authority = ticket.authority
                self._look_context.expires_at = min(self._look_context.expires_at, now + context_ttl)
            self._schedule_companion_timer(context_ttl, lambda: self._expire_look_context(ticket.token))
            self._companion.finish(ticket, "answered")
            self._require_current(ticket, "before returning its answer")
            self._look_last_failure = ""
            return {
                "ok": True,
                "answer": answer,
                "captures": 1,
                "processing": str(result.get("processing") or "unknown")[:64],
                "model": str(result.get("model") or "")[:200],
                "origin": ticket.origin,
                "follow_ups_remaining": LOOK_MAX_FOLLOW_UPS,
                "request_token": ticket.token,
            }
        except Exception as exc:
            category = _failure_category(exc)
            with self._look_context_lock:
                if self._look_context is not None and self._look_context.ticket_token == ticket.token:
                    self._look_context = None
                # A stale failure must never overwrite a newer interaction's state.
                if ticket.token >= self._look_last_failure_token and ticket.token == self._companion.current_token():
                    self._look_last_failure = category
                    self._look_last_failure_token = ticket.token
            self._companion.finish(ticket, "cancelled" if category == "cancelled" else "failed")
            _LOGGER.info("Look ended without an answer (%s)", category)
            if isinstance(exc, (RuntimeError, ValueError)):
                raise
            raise RuntimeError(f"Look failed ({category})") from exc

    def _analyze_look(
        self, jpeg: bytes, question: str, config: AppConfig, *, ticket: CompanionTicket
    ) -> dict[str, object]:
        """Exactly the configured route: local vision keeps the frame local; otherwise the bridge. No fallback."""
        if config.local_vision_enabled:
            try:
                with self._new_local_vision_client(config) as client:
                    self._require_current(ticket, "after local client preparation")
                    seen = client.describe(jpeg, question, is_current=lambda: self._companion_ticket_current(ticket))
            except LocalVisionError as exc:
                with self._status_lock:
                    self._status.local_vision_last_error = str(exc)
                raise RuntimeError(str(exc)) from exc
            with self._status_lock:
                self._status.local_vision_answers += 1
                self._status.local_vision_last_error = ""
            return {"answer": seen.get("answer"), "model": seen.get("model"), "processing": "local_vision"}
        client = self._new_bridge_client(config)
        try:
            self._require_current(ticket, "after bridge client preparation")
            seen = client.look(
                jpeg,
                question,
                session_id=f"look-{ticket.token}",
                is_current=lambda: self._companion_ticket_current(ticket),
            )
        finally:
            client.close()
        return {"answer": seen.get("text"), "model": seen.get("model"), "processing": seen.get("processing")}

    # --- follow-ups (text only, tool-free) ----------------------------------------------------

    def _take_follow_up(self, question: str) -> tuple[LookContext, str, int]:
        now = time.monotonic()
        with self._look_context_lock:
            context = self._look_context
            if context is None or not context.live(now=now, current_token=self._companion.current_token()):
                self._look_context = None
                raise RuntimeError("Ask Reachy to look first; the earlier look has ended or expired")
            context.follow_ups += 1
            return context, context.answer, LOOK_MAX_FOLLOW_UPS - context.follow_ups

    def follow_up_look(
        self,
        question: str,
        *,
        origin: LookOrigin = "phone",
        authority: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Tool-free text follow-up under both the original capture and current caller's scope."""
        guard = self._companion_authority(origin, authority)
        clean = _clean_question(question)
        if not guard():
            self._companion_teardown("follow_up_revoked")
            raise LookCancelled("The earlier look is no longer authorized")
        config = self.config_loader()
        if not config.household_configured:
            raise RuntimeError("Connect the household service before asking follow-up questions")
        owns_voice_slot = origin != "voice"
        if owns_voice_slot and not self._voice_activity_lock.acquire(blocking=False):
            raise RuntimeError("Reachy is busy with a voice interaction; try again in a moment")
        owns_look_slot = False
        try:
            owns_look_slot = self._look_lock.acquire(blocking=False)
            if not owns_look_slot:
                raise RuntimeError("Reachy is already handling a look")
            context, analysis, remaining = self._take_follow_up(clean)
            original = context.authority
            if original is None or original.identity != guard.identity or original.origin != origin:
                raise RuntimeError("This look belongs to a different owner or conversation")
            if not guard() or not original():
                self._companion_teardown("follow_up_revoked")
                raise LookCancelled("The earlier look is no longer authorized")
            stop_generation, profile = self._companion_snapshot()
            ticket = self._companion.admit(
                "follow_up",
                origin=origin,
                stop_generation=stop_generation,
                capability_profile=profile,
                authority=guard,
            )
            with self._look_context_lock:
                if self._look_context is not context:
                    raise LookCancelled("The earlier look was discarded")
                context.ticket_token = ticket.token
            self._require_current(ticket, "before the text follow-up")
            client = self._new_bridge_client(config)
            try:
                self._require_current(ticket, "after follow-up client preparation")
                answer = client.look_follow_up(
                    analysis,
                    clean,
                    is_current=lambda: self._companion_ticket_current(ticket) and original(),
                )
            except Exception as exc:
                self._companion.finish(ticket, "failed")
                raise RuntimeError("The visual follow-up provider is unavailable") from exc
            finally:
                client.close()
            self._require_current(ticket, "before returning the text follow-up")
            if not original():
                raise LookCancelled("The original look is no longer authorized")
            self._companion.finish(ticket, "answered")
            return {
                "ok": True,
                "answer": " ".join(str(answer).split())[:1200],
                "captures": 0,
                "follow_ups_remaining": remaining,
                "request_token": ticket.token,
            }
        finally:
            if owns_look_slot:
                self._look_lock.release()
            if owns_voice_slot:
                self._voice_activity_lock.release()

    def look_context_live(self) -> bool:
        with self._look_context_lock:
            context = self._look_context
        live = bool(
            context is not None
            and context.live(now=time.monotonic(), current_token=self._companion.current_token())
            and (context.authority is None or context.authority())
        )
        if not live:
            with self._look_context_lock:
                if self._look_context is context:
                    self._look_context = None
        return live

    def voice_look_turn(self, transcript: str) -> str:
        """Everyday voice entry: same orchestration as the phone; failures become a truthful spoken reply."""
        try:
            return str(self.look_at_this(transcript, origin="voice")["answer"])
        except LookCancelled:
            return ""
        except (RuntimeError, ValueError) as exc:
            return f"I couldn't look just now: {exc}"

    def voice_look_follow_up(self, transcript: str) -> str | None:
        """For an Everyday voice follow-up while a look is live: the tool-free answer, else None."""
        if not self.look_context_live():
            return None
        try:
            return str(self.follow_up_look(transcript, origin="voice")["answer"])
        except LookCancelled:
            return ""
        except (RuntimeError, ValueError):
            return "I couldn't answer about that look. Please ask me to take a new look."

    # --- stop and status ------------------------------------------------------------------------

    def companion_stop(self) -> dict[str, object]:
        """Stop the current companion interaction only: look, speech, pending offer. Project work stays."""
        stopped: list[str] = []
        with self._look_context_lock:
            if self._look_context is not None:
                stopped.append("look")
        self._companion_teardown("stop")
        self.stop_speaking()
        stopped.append("speech")
        if self.cancel_contextual_offer("stopped"):
            stopped.append("offer")
        return {"ok": True, "stopped": stopped}

    def _offer_completion_current(self, epoch: int, generation: int, source_revision: str = "") -> bool:
        return (
            self._offer_epoch == epoch
            and self._announcement_cancellation_generation == generation
            and not self.stop_event.is_set()
            and not self._privacy_requested.is_set()
            and (not source_revision or source_revision == self.config_loader().companion_sources_revision)
        )

    def _remember_private_detail(
        self,
        source: str,
        detail: str,
        *,
        offer_epoch: int | None = None,
        cancellation_generation: int | None = None,
        source_revision: str = "",
    ) -> bool:
        clean = " ".join(detail.split())[:240]
        if not clean:
            return False
        with self._announcement_state_lock:
            generation = (
                self._announcement_cancellation_generation
                if cancellation_generation is None
                else cancellation_generation
            )
            with self._private_details_lock:
                epoch = self._offer_epoch if offer_epoch is None else offer_epoch
                if not self._offer_completion_current(epoch, generation, source_revision):
                    return False
                now = time.monotonic()
                self._private_details = [item for item in self._private_details if float(item["expires_at"]) > now][
                    -3:
                ]
                self._private_details.append(
                    {"source": source, "detail": clean, "expires_at": now + PRIVATE_DETAIL_TTL_SECONDS}
                )
        self._schedule_companion_timer(PRIVATE_DETAIL_TTL_SECONDS, lambda: self._expire_private_details())
        return True

    def _expire_private_details(self) -> None:
        now = time.monotonic()
        with self._private_details_lock:
            self._private_details = [item for item in self._private_details if float(item["expires_at"]) > now]

    def companion_owner_status(self) -> dict[str, object]:
        """Owner-only details; snapshot under the same lock used by revocation."""
        self._expire_private_details()
        now = time.monotonic()
        with self._private_details_lock:
            return {
                "private_details": [
                    {
                        "source": item["source"],
                        "detail": item["detail"],
                        "seconds_remaining": max(0, int(float(item["expires_at"]) - now)),
                    }
                    for item in self._private_details
                ]
            }

    def companion_public_status(self, config: AppConfig) -> dict[str, object]:
        now = time.monotonic()
        presence = self._presence.public_status(enabled=config.proactive_presence_enabled)
        live = self.look_context_live()
        with self._look_context_lock:
            context = self._look_context
        with self._status_lock:
            state = self._status.state
            announcement_error = bool(self._status.announcement_last_error)
            tts_provider = str(getattr(self._status, "announcement_provider", "") or "")
        coordinator = self._companion.public_status()
        activity = str(coordinator["activity"])
        if activity == "quiet":
            if state in {"listening", "transcribing"}:
                activity = "listening"
            elif state in {"thinking", "synthesizing", "speaking", "announcing", "announcement_synthesizing"}:
                activity = "replying"
            elif state == "looking":
                activity = "looking"
            elif state in {"starting", "stopping", "error", "configuration_error", "power_transition_error"}:
                activity = "unavailable"
        block = self._look_block_reason(config)
        suggestions_level = (
            "off"
            if not (config.initiative_policy_enabled and config.contextual_offers_enabled)
            else {"quiet": "off", "balanced": "gentle", "engaged": "more_often"}.get(config.initiative_mode, "gentle")
        )
        if suggestions_level == "off":
            paused = "Suggestions are off until a source is enrolled and offers are enabled"
        elif announcement_error:
            paused = "Suggestions paused: voice unavailable (the last spoken announcement failed)"
        elif config.proactive_presence_enabled and presence["occupancy"] != "present":
            paused = f"Suggestions paused: room {presence['occupancy']}"
        else:
            reason = self._presence_suppression_reason(config)
            paused = f"Suggestions paused: {reason.replace('_', ' ')}" if reason else ""
        failure = self._look_last_failure if self._look_last_failure in FAILURE_CATEGORIES else "blocked"
        return {
            "activity": activity,
            "presence": {"occupancy": presence["occupancy"], "source_health": presence["source_health"]},
            "camera": {"permitted": self._look_camera_permitted(config), "in_use": bool(self._camera_in_use)},
            "microphone": {
                "permitted": (
                    not self._privacy_requested.is_set() and self._effective_power_mode() not in {"meeting", "sleep"}
                ),
                "in_use": bool(getattr(self, "_recording", False)),
            },
            "look": {
                "generation": self._companion.current_epoch(),
                "available": not block,
                "blocked_reason": block,
                "context_live": live,
                "context_seconds_remaining": max(0, int(context.expires_at - now)) if live and context else 0,
                "follow_ups_remaining": (LOOK_MAX_FOLLOW_UPS - context.follow_ups) if live and context else 0,
                "last_failure": failure,
                "processing": "local_vision" if config.local_vision_enabled else "hermes_host",
            },
            "speech": {"last_announcement_failed": announcement_error, "route": tts_provider[:32]},
            "suggestions": {"level": suggestions_level, "paused_reason": paused},
            "receipts": coordinator["receipts"],
        }
