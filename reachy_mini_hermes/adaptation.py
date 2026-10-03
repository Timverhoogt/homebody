"""Transparent personal adaptation for Agent 0.6.5: coarse preference signals, timing only.

Reachy learns four owner signals per initiative category: welcomed, dismissed, snoozed, and
disabled. They adapt *when and how often* a category may take initiative, never what it is
allowed to do: confidence thresholds, budgets, permissions, and risk tiers are untouched.

The ledger stores only decayed counters, a snooze deadline, a disabled flag and one timestamp
per category, so the owner can read everything Reachy "knows" and reset it. There is no
transcript, text, identity, or raw interaction archive.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)

PreferenceSignal = Literal["welcomed", "dismissed", "snoozed"]
DayBand = Literal["morning", "afternoon", "evening", "night"]

CATEGORIES: tuple[str, ...] = (
    "presence",
    "calendar",
    "reminder",
    "timer",
    "home_assistant",
    "weather",
    "project",
    "presentation",
)
CATEGORY_LABELS = {
    "presence": "Silent presence acknowledgement",
    "calendar": "Calendar offers",
    "reminder": "Reminder offers",
    "timer": "Timer offers",
    "home_assistant": "Home offers",
    "weather": "Weather offers",
    "project": "Project offers",
    "presentation": "Shared-object offers",
}
DAY_BANDS: tuple[DayBand, ...] = ("morning", "afternoon", "evening", "night")

# Old signals fade: a dismissal from a month ago should not keep Reachy quiet forever.
HALF_LIFE_SECONDS = 14 * 86400.0
SNOOZE_SECONDS = 4 * 3600.0
MIN_COOLDOWN_MULTIPLIER = 0.5
MAX_COOLDOWN_MULTIPLIER = 4.0
# A part of the day becomes a learned quiet time after this many (decayed) dismissals there,
# as long as the owner has not welcomed that category more often overall.
QUIET_BAND_DISMISSALS = 3.0


def day_band(moment: datetime) -> DayBand:
    hour = moment.hour
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def _require_category(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError("Unsupported initiative category")
    return category


@dataclass(slots=True)
class CategoryPreference:
    """Everything Reachy remembers about one category; small enough to show verbatim."""

    welcomed: float = 0.0
    dismissed: float = 0.0
    snoozed: float = 0.0
    band_dismissals: dict[str, float] = field(default_factory=lambda: dict.fromkeys(DAY_BANDS, 0.0))
    snoozed_until: float = 0.0
    disabled: bool = False
    updated_at: float = 0.0

    def decay_to(self, now: float) -> None:
        if self.updated_at <= 0.0 or now <= self.updated_at:
            self.updated_at = max(self.updated_at, now)
            return
        factor = 0.5 ** ((now - self.updated_at) / HALF_LIFE_SECONDS)
        self.welcomed *= factor
        self.dismissed *= factor
        self.snoozed *= factor
        self.band_dismissals = {band: value * factor for band, value in self.band_dismissals.items()}
        self.updated_at = now

    @property
    def is_default(self) -> bool:
        return (
            not self.disabled
            and self.snoozed_until <= 0.0
            and max(self.welcomed, self.dismissed, self.snoozed, *self.band_dismissals.values()) < 0.05
        )


class PreferenceLedger:
    """Thread-safe, owner-visible preference counters with optional JSON persistence."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        wall_clock: Callable[[], datetime] = datetime.now,
        epoch_clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._wall_clock = wall_clock
        self._epoch_clock = epoch_clock
        self._lock = threading.RLock()
        self._preferences: dict[str, CategoryPreference] = {category: CategoryPreference() for category in CATEGORIES}
        self._load()

    # -- signals -----------------------------------------------------------------------------

    def record(self, category: str, signal: PreferenceSignal) -> None:
        _require_category(category)
        if signal not in {"welcomed", "dismissed", "snoozed"}:
            raise ValueError("Unsupported preference signal")
        with self._lock:
            now = self._epoch_clock()
            preference = self._preferences[category]
            preference.decay_to(now)
            if signal == "welcomed":
                preference.welcomed += 1.0
                preference.snoozed_until = 0.0
            elif signal == "dismissed":
                preference.dismissed += 1.0
                band = day_band(self._wall_clock())
                preference.band_dismissals[band] = preference.band_dismissals.get(band, 0.0) + 1.0
            else:
                preference.snoozed += 1.0
                preference.snoozed_until = now + SNOOZE_SECONDS
            self._save_unlocked()

    def set_disabled(self, category: str, disabled: bool) -> None:
        _require_category(category)
        with self._lock:
            preference = self._preferences[category]
            preference.decay_to(self._epoch_clock())
            preference.disabled = bool(disabled)
            self._save_unlocked()

    def reset(self, category: str | None = None) -> None:
        with self._lock:
            targets = CATEGORIES if category is None else (_require_category(category),)
            for target in targets:
                self._preferences[target] = CategoryPreference()
            self._save_unlocked()

    # -- effects (timing and frequency only) ----------------------------------------------------

    def suppression_reason(self, category: str) -> str:
        """Return why this category must stay silent right now, or "" when timing allows it."""
        _require_category(category)
        with self._lock:
            now = self._epoch_clock()
            preference = self._preferences[category]
            preference.decay_to(now)
            if preference.disabled:
                return "category_disabled"
            if preference.snoozed_until > now:
                return "snoozed"
            if self._quiet_band_unlocked(preference, day_band(self._wall_clock())):
                return "learned_quiet_time"
            return ""

    def cooldown_multiplier(self, category: str) -> float:
        """Scale the topic cooldown: welcomed categories may return sooner, dismissed ones later."""
        _require_category(category)
        with self._lock:
            preference = self._preferences[category]
            preference.decay_to(self._epoch_clock())
            net = preference.dismissed - preference.welcomed
            return min(MAX_COOLDOWN_MULTIPLIER, max(MIN_COOLDOWN_MULTIPLIER, 2.0 ** (net / 2.0)))

    # -- transparency ---------------------------------------------------------------------------

    def public_status(self) -> list[dict[str, object]]:
        """Everything the ledger holds, with a plain-language explanation per category."""
        with self._lock:
            now = self._epoch_clock()
            band = day_band(self._wall_clock())
            rows: list[dict[str, object]] = []
            for category in CATEGORIES:
                preference = self._preferences[category]
                preference.decay_to(now)
                state, explanation = self._describe_unlocked(preference, band, now)
                rows.append(
                    {
                        "category": category,
                        "label": CATEGORY_LABELS[category],
                        "state": state,
                        "explanation": explanation,
                        "disabled": preference.disabled,
                        "welcomed": round(preference.welcomed, 1),
                        "dismissed": round(preference.dismissed, 1),
                        "snoozed": round(preference.snoozed, 1),
                        "snoozed_minutes_remaining": max(0, math.ceil((preference.snoozed_until - now) / 60.0)),
                        "quiet_bands": [
                            name for name in DAY_BANDS if self._quiet_band_unlocked(preference, name)
                        ],
                        "cooldown_multiplier": round(self._multiplier(preference), 2),
                    }
                )
            return rows

    def _describe_unlocked(self, preference: CategoryPreference, band: DayBand, now: float) -> tuple[str, str]:
        if preference.disabled:
            return "disabled", "Turned off by you. Reachy will not take this initiative."
        if preference.snoozed_until > now:
            minutes = max(1, math.ceil((preference.snoozed_until - now) / 60.0))
            return "snoozed", f"You said “later”, so Reachy waits about {minutes} more minutes."
        if self._quiet_band_unlocked(preference, band):
            return "quiet_now", f"You often said no in the {band}, so Reachy stays quiet at this time of day."
        multiplier = self._multiplier(preference)
        if multiplier >= 1.4:
            declined = max(1, round(preference.dismissed))
            return "less_often", f"You declined this about {declined} times recently, so it comes up less often."
        if multiplier <= 0.75:
            welcomed = max(1, round(preference.welcomed))
            return "more_often", f"You welcomed this about {welcomed} times recently, so it may come up sooner."
        return "normal", "No learned preference yet. Your initiative settings apply as configured."

    @staticmethod
    def _multiplier(preference: CategoryPreference) -> float:
        net = preference.dismissed - preference.welcomed
        return min(MAX_COOLDOWN_MULTIPLIER, max(MIN_COOLDOWN_MULTIPLIER, 2.0 ** (net / 2.0)))

    @staticmethod
    def _quiet_band_unlocked(preference: CategoryPreference, band: str) -> bool:
        dismissals = preference.band_dismissals.get(band, 0.0)
        return dismissals >= QUIET_BAND_DISMISSALS and dismissals > preference.welcomed

    # -- persistence ----------------------------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("preference ledger root must be an object")
            for category, raw in payload.items():
                if category not in CATEGORIES or not isinstance(raw, dict):
                    continue
                bands = raw.get("band_dismissals") or {}
                self._preferences[category] = CategoryPreference(
                    welcomed=_finite(raw.get("welcomed")),
                    dismissed=_finite(raw.get("dismissed")),
                    snoozed=_finite(raw.get("snoozed")),
                    band_dismissals={
                        band: _finite(bands.get(band) if isinstance(bands, dict) else 0.0) for band in DAY_BANDS
                    },
                    snoozed_until=_finite(raw.get("snoozed_until")),
                    disabled=raw.get("disabled") is True,
                    updated_at=_finite(raw.get("updated_at")),
                )
        except Exception as exc:
            # Fail safe to "no learned preference" rather than refusing to start.
            _LOGGER.warning("Ignoring unreadable initiative preferences at %s: %s", self._path, exc)
            self._preferences = {category: CategoryPreference() for category in CATEGORIES}

    def _save_unlocked(self) -> None:
        if self._path is None:
            return
        payload = {
            category: asdict(preference)
            for category, preference in self._preferences.items()
            if not preference.is_default
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self._path)
        except OSError as exc:
            _LOGGER.warning("Could not persist initiative preferences: %s", exc)


def _finite(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0.0 else 0.0
