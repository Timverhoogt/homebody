"""Append-only, bounded, sanitized audit events for Reachy Agent Mode."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path

_SECRET_KEY = re.compile(r"(?i)(authorization|credential|password|secret|token|api.?key|cookie)")
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+[a-z0-9._~+/-]+|(?:sk|key|token)[-_][a-z0-9_-]{8,})")
_UNRESTRICTED_TOOL = re.compile(
    r"(?i)\b(terminal|process|execute_code|read_file|write_file|search_files|patch|shell|arbitrary filesystem)\b"
)
_ALLOWED_FIELDS = frozenset(
    {"capability_id", "risk_tier", "result_class", "reason", "session_generation", "approval_method", "summary"}
)


def sanitize_text(value: object, *, limit: int = 240) -> str:
    text = " ".join(str(value).split())[:limit]
    text = _SECRET_VALUE.sub("[redacted]", text)
    return _UNRESTRICTED_TOOL.sub("[restricted-capability]", text)


def sanitize_event(event_type: str, fields: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {"event": sanitize_text(event_type, limit=48)}
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS or _SECRET_KEY.search(key):
            continue
        if key == "session_generation":
            payload[key] = int(str(value))
        else:
            payload[key] = sanitize_text(value)
    return payload


class AgentAuditLog:
    """JSONL writer that rotates whole files and never stores prompt/result bodies."""

    def __init__(self, path: Path, *, max_bytes: int = 512_000, backups: int = 3) -> None:
        if max_bytes < 1_024 or not 0 <= backups <= 10:
            raise ValueError("Invalid audit retention bounds")
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def append(self, event_type: str, **fields: object) -> dict[str, object]:
        payload = {"timestamp": round(time.time(), 3), **sanitize_event(event_type, fields)}
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line.encode("utf-8")) > self.max_bytes:
                self._rotate_unlocked()
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
        return payload

    def recent(self, *, limit: int = 20) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-max(0, min(limit, 100)) :]
        events: list[dict[str, object]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def _rotate_unlocked(self) -> None:
        if self.backups == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_suffix(self.path.suffix + f".{self.backups}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            if source.exists():
                source.replace(self.path.with_suffix(self.path.suffix + f".{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
