"""Short-lived, one-time approvals bound to exact agent requests."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .agent_policy import CapabilityId, RiskTier


def canonical_arguments(arguments: Mapping[str, object]) -> str:
    """Return deterministic JSON suitable for exact approval binding."""
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Approval arguments must be finite JSON values") from exc


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    capability_id: CapabilityId
    arguments_digest: str
    risk_tier: RiskTier
    session_generation: int
    expires_at: float
    approval_method: str


class ApprovalStore:
    """In-memory approval store with lock-protected atomic consumption."""

    def __init__(self, *, ttl_seconds: float = 120.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not 1.0 <= ttl_seconds <= 600.0:
            raise ValueError("Approval expiry must be between 1 and 600 seconds")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    @staticmethod
    def _digest(arguments: Mapping[str, object]) -> str:
        return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()

    def issue(
        self,
        capability_id: CapabilityId,
        arguments: Mapping[str, object],
        *,
        risk_tier: RiskTier,
        session_generation: int,
        approval_method: str,
    ) -> str:
        method = approval_method.strip().lower()
        if method not in {"phone", "pin", "phone+pin"}:
            raise ValueError("Unsupported approval method")
        token = secrets.token_urlsafe(32)
        record = ApprovalRecord(
            capability_id=capability_id,
            arguments_digest=self._digest(arguments),
            risk_tier=risk_tier,
            session_generation=session_generation,
            expires_at=self._clock() + self._ttl_seconds,
            approval_method=method,
        )
        with self._lock:
            self._purge_expired_unlocked()
            self._records[token] = record
        return token

    def consume(
        self,
        token: str,
        capability_id: CapabilityId,
        arguments: Mapping[str, object],
        *,
        session_generation: int,
    ) -> ApprovalRecord | None:
        digest = self._digest(arguments)
        now = self._clock()
        with self._lock:
            record = self._records.get(token)
            if record is None:
                return None
            if record.expires_at <= now:
                self._records.pop(token, None)
                return None
            if (
                record.capability_id != capability_id
                or record.session_generation != session_generation
                or not secrets.compare_digest(record.arguments_digest, digest)
            ):
                return None
            self._records.pop(token, None)
            return record

    def invalidate_all(self) -> int:
        with self._lock:
            count = len(self._records)
            self._records.clear()
            return count

    def _purge_expired_unlocked(self) -> None:
        now = self._clock()
        self._records = {token: record for token, record in self._records.items() if record.expires_at > now}
