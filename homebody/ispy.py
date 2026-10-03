"""Deterministic, child-safe I Spy rules with shared standalone-project lineage.

Review the related standalone safety contract when changing a shared target-policy
principle. Each app retains independent runtime, release, and acceptance authority:
https://github.com/Timverhoogt/reachy-mini-i-spy/blob/main/docs/SAFETY_CONTRACT.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COLOURS = {
    "red": {"en": "red", "nl": "rood"},
    "orange": {"en": "orange", "nl": "oranje"},
    "yellow": {"en": "yellow", "nl": "geel"},
    "green": {"en": "green", "nl": "groen"},
    "blue": {"en": "blue", "nl": "blauw"},
    "purple": {"en": "purple", "nl": "paars"},
    "pink": {"en": "pink", "nl": "roze"},
    "brown": {"en": "brown", "nl": "bruin"},
    "black": {"en": "black", "nl": "zwart"},
    "white": {"en": "white", "nl": "wit"},
    "grey": {"en": "grey", "nl": "grijs"},
}
DISALLOWED_TERMS = {
    "face", "person", "people", "child", "body", "skin", "hair", "eye", "hand",
    "shirt", "dress", "clothing", "screen", "monitor", "television", "phone", "tablet",
    "document", "paper", "letter", "photo", "medicine", "pill", "drug", "weapon", "gun",
    "knife", "private", "underwear", "passport", "credit card", "password", "address",
    "gezicht", "persoon", "mensen", "kind", "lichaam", "kleding", "scherm", "medicijn",
    "wapen", "mes", "privé", "telefoon",
}


@dataclass(frozen=True, slots=True)
class ISpyTarget:
    object_name: str
    colour: str
    category: str
    location: str
    frame_index: int
    bbox: tuple[float, float, float, float]
    confidence: float
    visible_frame_count: int
    hints_en: tuple[str, ...]
    hints_nl: tuple[str, ...]

    def clue(self, language: str) -> str:
        colour = COLOURS[self.colour][language]
        if language == "nl":
            return f"Ik zie, ik zie wat jij niet ziet, en de kleur is {colour}."
        return f"I spy with my little eye, something that is {colour}."

    def hints(self, language: str) -> tuple[str, ...]:
        return self.hints_nl if language == "nl" else self.hints_en



def _clean(value: object, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise ValueError("I Spy target text is invalid")
    result = " ".join(value.strip().split())
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError("I Spy target text is invalid")
    return result



def _unsafe(text: str) -> bool:
    return bool(set(re.findall(r"[\wÀ-ÿ]+", text.casefold())) & DISALLOWED_TERMS)



def validate_ispy_target(payload: object, *, frame_count: int = 3) -> ISpyTarget:
    """Accept only stable, visible, unambiguously safe household targets."""
    if not isinstance(payload, dict) or payload.get("stable") is not True:
        raise ValueError("I Spy target is not stable")
    if not 2 <= frame_count <= 5:
        raise ValueError("I Spy frame count is outside the bounded contract")
    visible_frame_count = int(payload.get("visible_frame_count", 0))
    if visible_frame_count < min(2, frame_count) or visible_frame_count > frame_count:
        raise ValueError("I Spy target is not visible in enough viewpoints")
    name = _clean(payload.get("object_name"), 60)
    category = _clean(payload.get("category"), 40)
    location = _clean(payload.get("location"), 100)
    colour = _clean(payload.get("colour"), 16).casefold()
    if colour == "gray":
        colour = "grey"
    if colour not in COLOURS:
        raise ValueError("I Spy target colour is not approved")
    if _unsafe(" ".join((name, category, location))):
        raise ValueError("I Spy target belongs to a disallowed class")
    confidence = float(payload.get("confidence", 0.0))
    if not 0.78 <= confidence <= 1.0:
        raise ValueError("I Spy target confidence is too low")
    frame_index = int(payload.get("frame_index", -1))
    if not 0 <= frame_index < frame_count:
        raise ValueError("I Spy target frame is invalid")
    bbox_raw = payload.get("bbox")
    if not isinstance(bbox_raw, list) or len(bbox_raw) != 4:
        raise ValueError("I Spy target bounding box is invalid")
    x, y, width, height = (float(value) for value in bbox_raw)
    bbox = (x, y, width, height)
    if min(bbox) < 0 or x + width > 1 or y + height > 1:
        raise ValueError("I Spy target bounding box is outside the image")
    area = width * height
    if area < 0.025 or area > 0.65 or min(width, height) < 0.12:
        raise ValueError("I Spy target is too small or too broad")

    def hints(language: str) -> tuple[str, ...]:
        raw = payload.get(f"hints_{language}")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
            raise ValueError("I Spy target needs bounded bilingual hints")
        values = tuple(_clean(item) for item in raw)
        if any(_unsafe(item) for item in values):
            raise ValueError("I Spy target hint is unsafe")
        return values

    return ISpyTarget(
        name,
        colour,
        category,
        location,
        frame_index,
        bbox,
        confidence,
        visible_frame_count,
        hints("en"),
        hints("nl"),
    )
