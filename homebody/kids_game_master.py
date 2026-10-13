"""Deterministic Kids game master: session phases, child host commands, and fixed host copy.

The game master owns one supervised play session; each activity (buddy, story, quiz, riddles,
calm, I Spy) remains a game adapter on the moderated bridge. Nothing here calls a model: commands
are recognised only from short, direct child utterances, so story prose or a game-local "stop"
inside a longer sentence is passed through to the active game unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .kids_mode import ACTIVITIES

# Categorical host phases exposed in owner-safe status (never answers, transcripts or targets).
HOST_PHASES = frozenset({"lobby", "transition", "playing", "offer", "waiting"})
# Order used for deterministic suggestions. I Spy is offered only with caregiver camera consent.
ACTIVITY_ORDER = ("story", "riddles", "quiz", "buddy", "calm", "ispy")
# A round ends on the bridge's strict finish outcome, or at this deterministic child-turn bound.
ROUND_TURN_LIMITS = {"buddy": 10, "story": 12, "quiz": 8, "riddles": 8, "calm": 6, "ispy": 16}
# Unrecognised replies to a choice are re-asked at most this often, then the host waits quietly.
MAX_CHOICE_REPROMPTS = 2
# Host commands are recognised only in short utterances; longer speech belongs to the game.
_MAX_COMMAND_WORDS = 7

_ACTIVITY_WORDS: dict[str, tuple[str, ...]] = {
    "ispy": ("i spy", "ispy", "ik zie ik zie", "ik zie", "zie ik zie"),
    "story": ("story", "stories", "verhaal", "verhaaltje", "verhaaltjes", "sprookje"),
    "riddles": ("riddle", "riddles", "raadsel", "raadsels", "raadseltje"),
    "quiz": ("quiz", "quizje", "quiz quest", "questions", "vragen", "vragenspel"),
    "calm": ("calm", "breathing", "breathe", "relax", "rustig", "rust", "ademen", "ademhalen", "ontspannen"),
    "buddy": ("chat", "talk", "talking", "buddy", "kletsen", "praten", "babbelen"),
}
_SWITCH_CUES = (
    "let's", "lets", "let us", "can we", "could we", "i want", "i'd like", "play", "do", "switch",
    "change", "instead", "now", "laten we", "mogen we", "kunnen we", "ik wil", "spelen", "doen",
    "liever", "wisselen", "nu",
)
_INTERROGATIVE_STARTS = tuple(f"{opener} " for opener in (
    "do you", "did you", "does", "are you", "is it", "is there", "have you", "what", "which", "why", "who",
    "how", "where", "when", "can you", "could you", "would you", "ken je", "ken jij", "weet je", "weet jij",
    "vind je", "vind jij", "hou je", "houd je", "heb je", "heb jij", "ben je", "is er", "wat", "welke",
    "waarom", "wie", "hoe", "waar", "wanneer",
))
_STOP_PHRASES = frozenset({
    "stop", "stop please", "please stop", "stop now", "stop playing", "stop the game", "i want to stop",
    "i'm done", "im done", "we're done", "were done", "all done", "finish", "finished", "the end",
    "end the game", "no more", "goodbye", "bye", "bye bye", "stoppen", "stop maar", "stop alsjeblieft",
    "ik wil stoppen", "ik ben klaar", "we zijn klaar", "klaar", "het is klaar", "einde", "niet meer",
    "doei", "dag", "tot ziens", "stop het spel", "stop met spelen", "ophouden",
})
_HELP_PHRASES = frozenset({
    "what can i say", "what do i say", "how does this work", "how do i play", "what can we play",
    "what games are there", "wat kan ik zeggen", "wat moet ik zeggen",
    "hoe werkt het", "hoe werkt dit", "hoe moet het", "wat kunnen we spelen", "welke spelletjes zijn er",
})
# Only while choosing: mid-game these are game moves (an I Spy give-up, a quiz "don't know").
_LOBBY_HELP_WORDS = frozenset({
    "help", "hulp", "help me", "help mij", "huh", "what", "wat",
    "i don't know", "i dont know", "i don't know what to say", "ik weet het niet", "weet ik niet",
})
_AGAIN_PHRASES = frozenset({
    "again", "again please", "play again", "one more", "one more time", "another round", "another one",
    "more", "same again", "nog een keer", "nog eens", "nog een", "nog een rondje", "nog een ronde",
    "opnieuw", "weer", "meer", "nog een keertje",
})
_DIFFERENT_PHRASES = frozenset({
    "something else", "something different", "a different game", "different game", "another game",
    "other game", "the other one", "the other", "iets anders", "een ander spel", "ander spel",
    "een ander spelletje", "andere", "de andere", "het andere",
})
_NO_PHRASES = frozenset({
    "no", "no thanks", "no thank you", "nope", "nee", "nee dank je", "nee bedankt", "liever niet",
})


@dataclass(frozen=True, slots=True)
class HostCommand:
    """A recognised direct child command; ``kind`` 'none' means the game handles the input."""

    kind: str  # none | stop | help | again | switch | different | no
    activity: str = ""


def _normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    folded = folded.replace("’", "'")
    folded = re.sub(r"[^a-z' ]+", " ", folded)
    return " ".join(folded.split())


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", text) is not None


def mentioned_activity(text: str) -> str:
    """Return the single activity a short utterance names, or '' when none/ambiguous."""
    found = {activity for activity, words in _ACTIVITY_WORDS.items() if any(_mentions(text, w) for w in words)}
    return next(iter(found)) if len(found) == 1 else ""


def parse_host_command(text: str, *, phase: str) -> HostCommand:
    """Recognise only direct, short child commands; quoted or long speech stays with the game."""
    if not isinstance(text, str) or any(mark in text for mark in "\"“”«»"):
        return HostCommand("none")
    clean = _normalise(text).strip(" '")
    if not clean or len(clean.split()) > _MAX_COMMAND_WORDS:
        return HostCommand("none")
    if clean in _STOP_PHRASES:
        return HostCommand("stop")
    if clean in _HELP_PHRASES:
        return HostCommand("help")
    choosing = phase in {"lobby", "offer", "waiting"}
    activity = mentioned_activity(clean)
    if choosing:
        if clean in _LOBBY_HELP_WORDS:
            return HostCommand("help")
        if clean in _AGAIN_PHRASES:
            return HostCommand("again")
        if clean in _DIFFERENT_PHRASES:
            return HostCommand("different")
        if clean in _NO_PHRASES:
            return HostCommand("no")
        if activity:
            return HostCommand("switch", activity)
        return HostCommand("none")
    # Mid-game: switching needs a named activity plus an explicit switch cue, or just the name.
    # Questions about a game ("do you like stories?") are conversation, never a switch.
    if activity and not clean.startswith(_INTERROGATIVE_STARTS) and (
        any(clean == cue or clean.startswith(cue + " ") for cue in _SWITCH_CUES)
        or clean in _ACTIVITY_WORDS[activity]
    ):
        return HostCommand("switch", activity)
    if clean in _DIFFERENT_PHRASES:
        return HostCommand("different")
    return HostCommand("none")


def available_activities(*, camera_consent: bool) -> tuple[str, ...]:
    return tuple(a for a in ACTIVITY_ORDER if a in ACTIVITIES and (a != "ispy" or camera_consent))


def suggest_other(current: str, *, camera_consent: bool, turn: int = 0) -> str:
    """One deterministic different activity; rotates so offers do not always repeat."""
    options = [a for a in available_activities(camera_consent=camera_consent) if a != current]
    return options[turn % len(options)]


def lobby_choices(*, camera_consent: bool) -> tuple[str, str]:
    return ("story", "riddles") if not camera_consent else ("ispy", "story")


_NAMES = {
    "en": {
        "buddy": "a chat", "story": "a story", "quiz": "a quiz", "riddles": "riddles",
        "calm": "calm breathing", "ispy": "I Spy",
    },
    "nl": {
        "buddy": "kletsen", "story": "een verhaal", "quiz": "een quiz", "riddles": "raadsels",
        "calm": "rustig ademen", "ispy": "ik zie, ik zie",
    },
}
_COPY = {
    "en": {
        "lobby": "Hi{name}! I'm your game host. Shall we do {a} or {b}? You can also say stop.",
        "offer": "That round is finished. Another round of {a}, or {b}? Or say stop to finish.",
        "offer_retry": "Say again for {a}, or say {b}. Or say stop.",
        "help": "You can say {a} or {b}, or name another game. Say stop to finish.",
        "help_playing": "Just answer me to keep playing. You can name another game, or say stop.",
        "start": "Okay, {a}!",
        "camera_needed": (
            "I Spy needs a grown-up to allow the camera first. A grown-up can do that on the Kids page. "
            "Shall we keep going?"
        ),
        "waiting": "I'll wait here. You can say a game, or a grown-up can choose one.",
        "unavailable": "That game isn't ready right now. Shall we do {a} or {b}?",
    },
    "nl": {
        "lobby": "Hoi{name}! Ik ben je spelleider. Zullen we {a} doen of {b}? Je mag ook stop zeggen.",
        "offer": "Die ronde is klaar. Nog een keer {a}, of {b}? Of zeg stop om te stoppen.",
        "offer_retry": "Zeg nog een keer voor {a}, of zeg {b}. Of zeg stop.",
        "help": "Je kunt {a} of {b} zeggen, of een ander spel noemen. Zeg stop om te stoppen.",
        "help_playing": "Geef gewoon antwoord om door te spelen. Je mag een ander spel noemen, of stop zeggen.",
        "start": "Oké, {a}!",
        "camera_needed": (
            "Voor ik zie, ik zie moet een volwassene eerst de camera toestaan. Dat kan op de Kids-pagina. "
            "Zullen we verder spelen?"
        ),
        "waiting": "Ik wacht hier. Je mag een spel noemen, of een volwassene kiest er een.",
        "unavailable": "Dat spel kan nu even niet. Zullen we {a} doen of {b}?",
    },
}


def activity_name(language: str, activity: str) -> str:
    return _NAMES["nl" if language == "nl" else "en"].get(activity, activity)


def host_line(language: str, key: str, *, a: str = "", b: str = "", nickname: str = "") -> str:
    """Return short fixed host copy in the selected language; activity ids become child names."""
    lang = "nl" if language == "nl" else "en"
    return _COPY[lang][key].format(
        a=activity_name(lang, a) if a in ACTIVITIES else a,
        b=activity_name(lang, b) if b in ACTIVITIES else b,
        name=f" {nickname}" if nickname else "",
    )
