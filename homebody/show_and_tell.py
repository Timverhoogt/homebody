"""Show and Tell authority gate and bounded single-frame transaction for Kids Chat (Buddy).

This module grants no camera access by itself. ``ShowAndTellMixin`` supplies the trusted
current-session/turn authority, including the Owner's standing permission (the existing
"Camera access for AI" approval plus the persistent Show and Tell opt-out), the bridge permit
and the existing exact-text Kids speech. The bridge, not Reachy, validates provider output,
renders the description, moderates it and keeps the approved enums for follow-up chat.
"""
from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

MAX_FRAME_BYTES = 1_000_000
TURN_SECONDS = 25.0
MIN_LOOK_INTERVAL = 5.0
LANGUAGES = frozenset({"en", "nl"})

# A closed grammar: narratives, negation, quotations and arbitrary suffixes cannot
# become camera instructions. Broaden only with explicit negative regression tests.
_REQUESTS = {
    "en": frozenset({
        "look now", "please look now", "look at my toy", "look at my drawing",
        "look at my build", "can you look at my toy", "can you look at my drawing",
        "can you look at my build", "i want to show you my toy",
        "i want to show you my drawing", "i want to show you my build",
    }),
    "nl": frozenset({
        "kijk nu", "kijk nu alsjeblieft", "kijk naar mijn speelgoed",
        "kijk naar mijn tekening", "kijk naar mijn bouwwerk",
        "kun je naar mijn speelgoed kijken", "kun je naar mijn tekening kijken",
        "kun je naar mijn bouwwerk kijken", "ik wil je mijn speelgoed laten zien",
        "ik wil je mijn tekening laten zien", "ik wil je mijn bouwwerk laten zien",
    }),
}


def explicit_look_request(text: object, language: str) -> bool:
    if not isinstance(text, str) or len(text) > 160:
        return False
    clean = " ".join(text.casefold().strip().split())
    clean = re.sub(r"[.!?]+$", "", clean)
    return clean in _REQUESTS.get(language, ())


class Unavailable(RuntimeError):
    """Categorical error only: no child/provider payload in exceptions."""

    def __init__(self) -> None:
        super().__init__("Show and Tell is unavailable for this turn")


@dataclass(frozen=True, slots=True, repr=False)
class Authority:
    """Trusted adapter snapshot, never reconstructed from public status, user text or a client flag.

    ``permitted`` is the Owner's standing permission at ``permission_generation``; any Owner
    camera revoke or Show and Tell opt-out advances that generation.
    """

    session: str
    session_generation: int
    stop_generation: int
    turn_generation: int
    expires: float
    activity: str
    language: str
    permitted: bool = False
    permission_generation: int = 0
    private: bool = False
    stopped: bool = False

    def __repr__(self) -> str:
        return "Authority(<private>)"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class Grant:
    """Ephemeral, internally derived session authorization; identity-compared, never a boolean."""

    authority: Authority
    token: str = field(repr=False)
    serial: int

    def __repr__(self) -> str:
        return "Grant(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class Permit:
    authority: Authority
    grant: Grant
    nonce: str = field(repr=False)
    deadline: float

    def __repr__(self) -> str:
        return "Permit(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class LookResult:
    """Bridge-approved exact speech; ``recognized`` is False for an honest decline."""

    text: str
    recognized: bool

    def __repr__(self) -> str:
        return "LookResult(<private>)"


class SessionGate:
    """Derived session authority under the Owner's standing Show and Tell permission.

    There is no per-session approval. While the Owner permission allows it, an explicit look in
    an eligible session derives one internal grant bound to that exact session, Kids generation,
    expiry, language and permission generation; losing any of them revokes the grant (and calls
    ``on_revoke``). Each look also binds the Stop and turn generations, so Stop cancels it
    without touching the Owner's saved setting. A later explicit look derives fresh authority
    only from the then-current permission; stale work never resurrects. Grants never derive from
    enabling, startup or a status read, and nothing here captures, wakes or listens.

    Lock order: the authority snapshot is read before this gate's lock is taken, so the gate
    lock never waits on runtime locks and may be used by callers already holding them.
    """

    def __init__(self, current: Callable[[], Authority], *, clock: Callable[[], float] = time.monotonic,
                 on_revoke: Callable[[Grant], None] | None = None):
        self._current = current
        self._clock = clock
        self._on_revoke = on_revoke
        self._lock = threading.RLock()
        self._serial = 0
        self._grant: Grant | None = None
        self._inflight: Permit | None = None
        self._last_look = -math.inf

    def _session_eligible(self, authority: Authority) -> bool:
        return (
            bool(authority.session) and authority.activity == "buddy"
            and authority.language in LANGUAGES and not authority.private and not authority.stopped
            and self._clock() < authority.expires
        )

    def _eligible(self, authority: Authority) -> bool:
        return authority.permitted is True and self._session_eligible(authority)

    @staticmethod
    def _grant_scope(authority: Authority) -> tuple[object, ...]:
        # Stop and turn generations are per look; they are deliberately not part of the grant.
        return (
            authority.session, authority.session_generation, authority.expires,
            authority.activity, authority.language, authority.permission_generation,
        )

    def _expire(self, now: Authority) -> None:
        grant = self._grant
        if grant is not None and (
            not self._eligible(now) or self._grant_scope(grant.authority) != self._grant_scope(now)
        ):
            self._revoke()

    def _revoke(self) -> None:
        revoked = self._grant
        self._serial += 1  # a pending permit or grant from before can never match again
        self._grant = None
        self._inflight = None
        if revoked is not None and self._on_revoke is not None:
            self._on_revoke(revoked)  # must not block: it only schedules bridge clean-up

    def revoke(self) -> None:
        with self._lock:
            self._revoke()

    def begin(self) -> Permit:
        now = self._current()
        with self._lock:
            self._expire(now)
            if (
                not self._eligible(now) or self._inflight is not None
                or self._clock() - self._last_look < MIN_LOOK_INTERVAL
            ):
                raise Unavailable()
            if self._grant is None:
                self._serial += 1
                self._grant = Grant(now, secrets.token_hex(32), self._serial)
            permit = Permit(
                now, self._grant, secrets.token_hex(32), min(now.expires, self._clock() + TURN_SECONDS),
            )
            self._inflight = permit
            self._last_look = self._clock()
            return permit

    def require(self, permit: Permit) -> None:
        now = self._current()
        with self._lock:
            self._expire(now)
            if (
                self._grant is None or permit.grant is not self._grant or self._inflight is not permit
                or now != permit.authority or not self._eligible(now) or self._clock() >= permit.deadline
            ):
                raise Unavailable()

    def finish(self, permit: Permit) -> None:
        with self._lock:
            if self._inflight is permit:
                self._inflight = None

    def public_state(self) -> dict[str, bool]:
        now = self._current()
        with self._lock:
            self._expire(now)
            return {
                "session": self._session_eligible(now),
                "available": self._eligible(now),
                "looking": self._inflight is not None,
            }


class KidsRoutes(Protocol):
    """Adapters use bridge-verifiable permits and exact approved Kids speech.

    Each adapter checks require_current before and after its own provider/TTS boundaries and
    immediately before every PCM playback handoff. Speech returns only after playback
    completed. Nothing here may wake, listen, move, retry or fall back to other speech.
    """

    def authorize(self, permit: Permit, require_current: Callable[[], None]) -> object: ...
    def speak_fixed(self, bridge_permit: object, require_current: Callable[[], None]) -> None: ...
    def capture_once(self, require_current: Callable[[], None]) -> bytes: ...
    def describe(self, frame: bytes, bridge_permit: object,
                 require_current: Callable[[], None]) -> LookResult: ...
    def speak_approved(self, text: str, bridge_permit: object,
                       require_current: Callable[[], None]) -> None: ...
    def release(self, bridge_permit: object, *, completed: bool) -> None: ...


def run_look(gate: SessionGate, routes: KidsRoutes, voice_owner: threading.Lock | None) -> bool:
    """Execute exactly one ephemeral look as the single audio owner.

    ``voice_owner`` is acquired without blocking; ``None`` means the calling voice turn
    already owns the voice slot. No retry, movement, general camera route, background task,
    file, log, listening restart or unmoderated fallback. Exceptions fail closed. Adapters bound
    their blocking I/O; authoritative checks quarantine every late completion.
    """
    if voice_owner is not None and not voice_owner.acquire(blocking=False):
        raise Unavailable()
    permit = None
    bridge_permit = None
    frame = None
    completed = False
    try:
        permit = gate.begin()

        def require() -> None:
            gate.require(permit)

        require()
        bridge_permit = routes.authorize(permit, require)
        require()
        if bridge_permit is None or isinstance(bridge_permit, bool):
            raise Unavailable()
        routes.speak_fixed(bridge_permit, require)  # announced before any capture
        require()
        frame = routes.capture_once(require)
        require()
        if not isinstance(frame, bytes) or not 0 < len(frame) <= MAX_FRAME_BYTES:
            raise Unavailable()
        try:
            result = routes.describe(frame, bridge_permit, require)
        finally:
            frame = None
        require()
        if not isinstance(result, LookResult) or not isinstance(result.text, str) or not result.text.strip():
            raise Unavailable()
        routes.speak_approved(result.text, bridge_permit, require)
        require()
        completed = True
        return result.recognized is True
    except Exception:
        raise Unavailable() from None
    finally:
        frame = None
        if permit is not None:
            gate.finish(permit)
        try:
            if bridge_permit is not None:
                routes.release(bridge_permit, completed=completed)
        except Exception:
            raise Unavailable() from None
        finally:
            if voice_owner is not None:
                voice_owner.release()
