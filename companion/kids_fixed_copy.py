"""Server-authored fixed Kids copy: the only robot-initiated lines the bridge approves for the Kids voice.

Reachy sends selectors (kind, key, activity ids), never text. The bridge renders the line from this
catalog, so an approval can only ever cover deterministic copy chosen here. Nothing here interpolates
caller-supplied free text: host-line slots accept activity ids only, and the I Spy clue is rendered by
the bridge from its own validated target. The robot keeps matching local copy for its status preview;
tests pin both catalogs together.
"""

from __future__ import annotations

ACTIVITIES = ("buddy", "story", "quiz", "riddles", "calm", "ispy")
LANGUAGES = ("en", "nl")
AGE_BANDS = ("4-6", "7-9", "10-12")
MAX_SEGMENTS = 2

GREETINGS = {
    "en": {
        "buddy": "Kids Mode is ready. What would you like to talk about?",
        "story": "Story time is ready. Who should our story be about?",
        "quiz": "Quiz time is ready. What is your favourite subject?",
        "riddles": "Riddle time is ready. Shall I tell you the first riddle?",
        "calm": "Calm time is ready. Sit comfortably. Are you ready to begin?",
        "ispy": "I Spy is ready. I will briefly look only for a safe household object, then the camera turns off.",
    },
    "nl": {
        "buddy": "Kindermodus staat klaar. Waar wil je over praten?",
        "story": "Verhaaltijd staat klaar. Over wie zal ons verhaal gaan?",
        "quiz": "Quiztijd staat klaar. Wat is je lievelingsvak?",
        "riddles": "Raadseltijd staat klaar. Zal ik het eerste raadsel vertellen?",
        "calm": "Rusttijd staat klaar. Ga lekker zitten. Ben je klaar om te beginnen?",
        "ispy": (
            "Ik zie, ik zie staat klaar. Ik kijk heel even rond naar een veilig voorwerp, "
            "daarna gaat de camera uit."
        ),
    },
}
NOTICES = {
    "en": {"five_minutes": "Five minutes left in Kids Mode. Let's finish this activity soon."},
    "nl": {"five_minutes": "Nog vijf minuten in de kindermodus. Laten we deze activiteit bijna afronden."},
}
ACTIVITY_NAMES = {
    "en": {
        "buddy": "a chat", "story": "a story", "quiz": "a quiz", "riddles": "riddles",
        "calm": "calm breathing", "ispy": "I Spy",
    },
    "nl": {
        "buddy": "kletsen", "story": "een verhaal", "quiz": "een quiz", "riddles": "raadsels",
        "calm": "rustig ademen", "ispy": "ik zie, ik zie",
    },
}
# The spoken lobby line carries no nickname: a caregiver-typed name is free text and never approved.
HOST_COPY = {
    "en": {
        "lobby": "Hi! I'm your game host. Shall we do {a} or {b}? You can also say stop.",
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
        "lobby": "Hoi! Ik ben je spelleider. Zullen we {a} doen of {b}? Je mag ook stop zeggen.",
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
# Exactly which activity-id slots each host line takes.
HOST_SLOTS = {
    "lobby": ("a", "b"), "offer": ("a", "b"), "offer_retry": ("a", "b"), "help": ("a", "b"),
    "help_playing": (), "start": ("a",), "camera_needed": (), "waiting": (), "unavailable": ("a", "b"),
}


def render_segment(segment: object, *, activity: str, language: str) -> str:
    """Render one strict selector; raise ValueError for anything outside the catalog.

    ``ispy_clue`` is not rendered here: the bridge renders it from its own validated target.
    """
    if language not in LANGUAGES or activity not in ACTIVITIES or not isinstance(segment, dict):
        raise ValueError("invalid fixed-speech selector")
    kind = segment.get("kind")
    if kind == "greeting" and set(segment) == {"kind"}:
        return GREETINGS[language][activity]
    if kind == "notice" and set(segment) == {"kind", "key"} and segment["key"] in NOTICES[language]:
        return NOTICES[language][str(segment["key"])]
    if kind == "host" and isinstance(segment.get("key"), str) and segment["key"] in HOST_SLOTS:
        slots = HOST_SLOTS[str(segment["key"])]
        if set(segment) != {"kind", "key", *slots} or any(segment[slot] not in ACTIVITIES for slot in slots):
            raise ValueError("invalid fixed-speech selector")
        names = {slot: ACTIVITY_NAMES[language][str(segment[slot])] for slot in slots}
        return HOST_COPY[language][str(segment["key"])].format(**{"a": "", "b": "", **names})
    raise ValueError("invalid fixed-speech selector")
