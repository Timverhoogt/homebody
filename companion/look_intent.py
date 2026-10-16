"""Narrow, deliberate visual-intent detection shared by the robot runtime and the Hermes host bridge.

A match is camera consent for exactly one capture, so this is intentionally conservative:
anchored imperative/question shapes about a physical thing being shown, in English or Dutch.
Negations, quoted speech, explanations of a phrase and non-physical targets never match.
Model tool choice is never used as evidence of intent; only the person's own words are.
"""

from __future__ import annotations

import re

_SPACE_RE = re.compile(r"\s+")
# Quoted speech: double/typographic quotes anywhere, or a single quote that is not an apostrophe inside a word.
_QUOTE_RE = re.compile(r"[\"“”„‘’]|(?<![A-Za-z])'|'(?![A-Za-z])")
_NEGATION_RE = re.compile(r"(?i)\b(do not|don'?t|never|not|niet|geen|nooit|nee)\b")
# Talking *about* looking, not asking for a look.
_META_RE = re.compile(
    r"(?i)\b(explain|phrase|means?|meaning|said|says|told|yesterday|uitleggen|betekent|betekenis|zei|zegt)\b"
)
# Non-physical targets: reading these is a text/project request, never a camera request.
_NON_PHYSICAL_RE = re.compile(
    r"(?i)\b(project|projectplan|roadmap|repositor\w*|repo|plan|file|bestand|document|e-?mail|mail|website|"
    r"wiki|issue|ticket|code|calendar|agenda|weather|weer|news|nieuws)\b"
)
_LEAD_RE = (
    r"^(?:(?:hey|hi|ok|okay|hoi|hé|he)[, ]+)?(?:(?:hermes|reachy|homebody)[, ]+)?"
    r"(?:(?:please|alsjeblieft|even|eens)[, ]+)?"
    r"(?:(?:can|could|would|will) you[, ]+|(?:kun|kan|wil|zou) je[, ]+)?"
    r"(?:(?:please|even|eens)[, ]+)?"
)
_TAIL = r"(?:\s+\w+){0,3}\s*$"
_EN_PATTERNS = (
    r"(?:take a look|have a look|look)(?: at)? (?:this|that|it|these|what i(?:'m| am) (?:holding|showing))" + _TAIL,
    r"read (?:this|that|it)" + _TAIL,
    r"what does (?:this|that|it) say\s*$",
    r"what(?:'s| is) (?:written|on) (?:this|that|here|it)" + _TAIL,
    r"what am i (?:holding|showing)(?: you)?\s*$",
    r"look again\s*$",
    r"take another look\s*$",
)
_NL_PATTERNS = (
    r"kijk(?: eens| even)?(?: naar)? (?:dit|hier|deze|dat)" + _TAIL,
    r"kijk nog eens\s*$",
    r"lees (?:dit|dat|het|deze)" + _TAIL,
    r"wat staat (?:hier|er|daar)(?: op)?\s*$",
    r"wat (?:is|zie je) (?:dit|hier)\s*$",
)
_ENGAGED_PATTERNS = (r"and this one\s*$", r"en deze\s*$", r"this one too\s*$", r"deze ook\s*$")
_LOOK_RE = re.compile(_LEAD_RE + "(?:" + "|".join(_EN_PATTERNS + _NL_PATTERNS) + ")", re.IGNORECASE)
_ENGAGED_RE = re.compile(_LEAD_RE + "(?:" + "|".join(_ENGAGED_PATTERNS) + ")", re.IGNORECASE)


# Arming the owner-opted-in temporary camera window. It never captures or uploads by itself.
_DURATION = (
    r"(?: (?:for|voor) (?:\d{1,3}|a|one|two|half a|een|twee|half) (?:seconds?|minutes?|seconden|minuten|minuut))?"
)
_SESSION_PATTERNS = (
    r"(?:turn|switch) (?:on (?:the|your) camera|(?:the|your) camera on)" + _DURATION,
    r"(?:start|enable|open) (?:the|your) camera" + _DURATION,
    r"camera on" + _DURATION,
    r"(?:zet|doe) (?:de|je) camera(?: even| eens)? aan" + _DURATION,
    r"(?:start|open) (?:de|je) camera" + _DURATION,
    r"camera aan" + _DURATION,
)
_SESSION_RE = re.compile(_LEAD_RE + "(?:" + "|".join(_SESSION_PATTERNS) + r")\s*$", re.IGNORECASE)


def _clean(text: str) -> str:
    raw = str(text or "")
    if _QUOTE_RE.search(raw):
        return ""
    clean = _SPACE_RE.sub(" ", re.sub(r"[?!.,;:]+$", "", raw.strip())).strip()
    if not clean or len(clean) > 160:
        return ""
    if _NEGATION_RE.search(clean) or _META_RE.search(clean) or _NON_PHYSICAL_RE.search(clean):
        return ""
    return clean


def looks_like_look_request(text: str, *, engaged: bool = False) -> bool:
    """True only for a deliberate request to look at / read a physical thing being shown.

    ``engaged`` enables the short "and this one" refresh only while a look is already live.
    """
    clean = _clean(text)
    if not clean:
        return False
    if _LOOK_RE.match(clean):
        return True
    return bool(engaged and _ENGAGED_RE.match(clean))


def requests_camera_session(text: str) -> bool:
    """True only when the person's own words ask to switch the temporary camera window on.

    Same quote/negation/meta guards as a look. This is never consent for a capture: a frame is
    only taken for a separate explicit look request.
    """
    clean = _clean(text)
    return bool(clean and _SESSION_RE.match(clean))
