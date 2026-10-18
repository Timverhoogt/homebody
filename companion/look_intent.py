"""Narrow, deliberate visual-intent detection shared by the robot runtime and the Hermes host bridge.

A match is camera consent for exactly one capture, so this is intentionally conservative:
anchored imperative/question shapes about a physical thing being shown, in English or Dutch.
Negations, quoted speech, explanations of a phrase and non-physical targets never match.
Model tool choice is never used as evidence of intent; only the person's own words are.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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


# Direct spoken controls. Anchored whole-utterance commands only: a question about a mode, a
# negation, a quote, a condition ("if ...", "als ...") or any extra clause is not a command.
_CONTROL_TAIL = r"(?:[, ]+(?:now|nu|please|alsjeblieft|reachy|homebody|hermes|maar|then|dan|thanks|bedankt))*\s*$"
_CONTROL_PATTERNS = {
    "sleep": (
        r"(?:(?:go|switch|change)(?: back)? to |enter |into )?(?:sleep|sleep mode)",
        r"go (?:to )?sleep(?: mode)?",
        r"(?:ga |ga maar )?slapen",
        r"(?:ga )?(?:naar |in )?(?:de )?(?:slaapstand|slaapmodus)",
        r"slaap",
    ),
    "standby": (
        r"(?:(?:go|switch|change)(?: back)? to |enter )?stand ?-?by(?: mode)?",
        r"(?:ga )?(?:naar |in )?(?:de )?stand ?-?by(?: ?stand| ?modus)?",
    ),
    "stop_looking": (
        r"stop (?:looking|watching|using the camera|the camera|with the camera)",
        r"(?:turn|switch|shut) (?:off (?:the|your) camera|(?:the|your) camera off)",
        r"camera off",
        r"no more looking",
        r"stop (?:met )?kijken",
        r"(?:zet|doe) (?:de|je) camera (?:maar )?uit",
        r"camera uit",
    ),
    "stop": (r"stop(?: it| talking| now)?", r"stop maar", r"hou(?:d)? op", r"stoppen"),
}
_CONTROL_RES = {
    action: re.compile(_LEAD_RE + "(?:" + "|".join(patterns) + ")" + _CONTROL_TAIL, re.IGNORECASE)
    for action, patterns in _CONTROL_PATTERNS.items()
}
# Fixed negated camera-off phrasings; a reduction only, so these are the sole negations accepted.
_CAMERA_OFF_NEGATED = re.compile(
    _LEAD_RE + r"(?:don'?t look any ?more|do not look any ?more|niet meer kijken|kijk niet meer)" + _CONTROL_TAIL,
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"(?i)\b(if|when|whenever|unless|after|before|later|tomorrow|tonight|minutes?|als|wanneer|tenzij|zodra|"
    r"straks|morgen|vanavond|minuten|minuut)\b"
)


def direct_control_intent(text: str) -> str | None:
    """``sleep``, ``standby``, ``stop_looking`` or ``stop`` for a direct spoken command, else ``None``.

    Only the person's own current utterance may be passed here, never model output, a tool result
    or an image description. Ambiguous wording returns ``None``: no physical action.
    """
    raw = str(text or "")
    if _QUOTE_RE.search(raw):
        return None
    clean = _SPACE_RE.sub(" ", re.sub(r"[?!.,;:]+$", "", raw.strip())).strip()
    if not clean or len(clean) > 80 or _CONDITION_RE.search(clean) or _META_RE.search(clean):
        return None
    if _CAMERA_OFF_NEGATED.match(clean):
        return "stop_looking"
    if _NEGATION_RE.search(clean):
        return None
    for action in ("stop_looking", "sleep", "standby", "stop"):
        if _CONTROL_RES[action].match(clean):
            return action
    return None


_HEAD = {"left": "left", "right": "right", "up": "up", "down": "down", "links": "left", "rechts": "right",
         "omhoog": "up", "omlaag": "down", "naar boven": "up", "naar beneden": "down"}
_ACTIONS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[str, dict] | None]], ...] = tuple(
    (re.compile(_LEAD_RE + "(?:" + pattern + ")" + _CONTROL_TAIL, re.IGNORECASE), build)
    for pattern, build in (
        (r"(?:(?:set|turn|change|put) )?(?:the |your )?volume (?:to |at )?(?P<n>\d{1,3})(?: ?%| percent)?",
         lambda m: ("set_reachy_volume", {"volume": int(m["n"])}) if int(m["n"]) <= 100 else None),
        (r"(?:zet )?(?:het |je )?volume (?:op |naar )?(?P<n>\d{1,3})(?: ?%| procent)?",
         lambda m: ("set_reachy_volume", {"volume": int(m["n"])}) if int(m["n"]) <= 100 else None),
        (r"mute(?: yourself)?|(?:zet )?(?:het |je )?geluid uit|demp(?:en)?(?: je geluid)?",
         lambda m: ("set_reachy_volume", {"muted": True})),
        (r"unmute(?: yourself)?|(?:zet )?(?:het |je )?geluid (?:weer )?aan",
         lambda m: ("set_reachy_volume", {"muted": False})),
        (r"(?:do a |do a short )?dance|dans(?: eens| even)?", lambda m: ("dance_reachy", {"style": "short"})),
        (r"(?:do a )?groovy dance", lambda m: ("dance_reachy", {"style": "groovy"})),
        (r"(?:do an )?energetic dance", lambda m: ("dance_reachy", {"style": "energetic"})),
        (r"look (?P<d>left|right|up|down)|kijk (?:naar )?(?P<n>links|rechts|omhoog|omlaag|naar boven|naar beneden)",
         lambda m: ("move_reachy_head", {"direction": _HEAD[(m["d"] or m["n"]).lower()]})),
        (r"look straight(?: ahead)?|kijk (?:recht ?uit|rechtdoor)",
         lambda m: ("move_reachy_head", {"direction": "center"})),
        (r"nod|knik(?: eens| even)?", lambda m: ("express_reachy_emotion", {"emotion": "yes"})),
        (r"shake your head|schud (?:je|met je) hoofd", lambda m: ("express_reachy_emotion", {"emotion": "no"})),
    )
)


def direct_action_intent(text: str) -> tuple[str, dict] | None:
    """``(tool name, exact arguments)`` for a short, direct spoken robot command, else ``None``.

    Only the person's own current words may be passed, never model output or an image description.
    The same quote/negation/condition/meta guards as controls apply; anything else is not a command.
    """
    raw = str(text or "")
    if _QUOTE_RE.search(raw):
        return None
    clean = _SPACE_RE.sub(" ", re.sub(r"[?!.,;:]+$", "", raw.strip())).strip()
    if not clean or len(clean) > 80 or _CONDITION_RE.search(clean) or _META_RE.search(clean):
        return None
    if _NEGATION_RE.search(clean) or direct_control_intent(clean) is not None:
        return None
    for pattern, build in _ACTIONS:
        match = pattern.match(clean)
        if match:
            return build(match)
    return None


_LOOK_WORD_RE = re.compile(r"(?i)\b(look\w*|see|seeing|watch\w*|camera|photo|picture|kijk\w*|zie\w*|foto)\b")


def natural_visual_words_ok(text: str) -> bool:
    """Provenance guard for a model-selected one-frame look under household approval.

    The model decides whether fresh visual evidence helps the person's direct request; this only
    checks the trusted current words permit it: present, not quoted, not a negated/meta request
    about looking, not a non-physical (text/project) target and not a control command.
    """
    raw = str(text or "")
    clean = _SPACE_RE.sub(" ", raw.strip())
    if not clean or len(clean) > 400 or _QUOTE_RE.search(raw):
        return False
    if direct_control_intent(clean) is not None or _META_RE.search(clean) or _NON_PHYSICAL_RE.search(clean):
        return False
    return not (_NEGATION_RE.search(clean) and _LOOK_WORD_RE.search(clean))
