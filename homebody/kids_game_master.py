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
# ES/DE/FR/IT/PT sessions add their own phrases to the shared set; other sessions never see them.
# Entries are already accent-folded like _normalise output ("aufhören" -> "aufhoren").
_EXTRA_PHRASES: dict[str, dict[str, frozenset[str]]] = {
    "es": {
        "stop": frozenset({
            "para", "parar", "para ya", "para por favor", "basta", "ya basta", "quiero parar", "he terminado",
            "ya termine", "termine", "terminado", "se acabo", "fin", "el fin", "no mas", "adios",
            "hasta luego", "chao", "deja de jugar", "terminar el juego",
        }),
        "help": frozenset({
            "que puedo decir", "que digo", "como funciona", "como se juega", "a que podemos jugar",
            "que juegos hay",
        }),
        "lobby_help": frozenset({"ayuda", "ayudame", "no se", "no lo se", "que"}),
        "again": frozenset({
            "otra vez", "otra", "otra ronda", "de nuevo", "una vez mas", "mas", "otra mas", "otra vez por favor",
        }),
        "different": frozenset({
            "otra cosa", "algo diferente", "otro juego", "un juego diferente", "el otro", "otro",
        }),
        "no": frozenset({"no gracias", "mejor no"}),
        "switch_cues": frozenset({
            "vamos a", "quiero", "juguemos", "jugar", "podemos", "cambiar", "mejor", "ahora", "prefiero",
        }),
        "interrogatives": frozenset({
            "te gusta", "te gustan", "sabes", "conoces", "que", "cual", "por que", "quien", "como", "donde",
            "cuando", "hay",
        }),
    },
    "de": {
        "stop": frozenset({
            "stopp", "stopp bitte", "bitte stopp", "aufhoren", "hor auf", "ich will aufhoren", "ich bin fertig",
            "wir sind fertig", "fertig", "ende", "das ende", "schluss", "nicht mehr", "tschuss",
            "auf wiedersehen", "spiel beenden", "bitte aufhoren",
        }),
        "help": frozenset({
            "was kann ich sagen", "was soll ich sagen", "wie geht das", "wie funktioniert das",
            "wie spielt man das", "was konnen wir spielen", "welche spiele gibt es",
        }),
        "lobby_help": frozenset({"hilfe", "hilf mir", "weiss nicht", "ich weiss nicht", "ich weiss es nicht", "was"}),
        "again": frozenset({
            "nochmal", "noch mal", "noch einmal", "nochmal bitte", "noch eine runde", "noch eins", "wieder",
        }),
        "different": frozenset({
            "etwas anderes", "was anderes", "ein anderes spiel", "anderes spiel", "das andere", "andere",
        }),
        "no": frozenset({"nein", "nein danke", "lieber nicht"}),
        "switch_cues": frozenset({
            "lass uns", "lasst uns", "konnen wir", "ich will", "ich mochte", "spielen", "machen", "lieber",
            "wechseln", "jetzt", "stattdessen",
        }),
        "interrogatives": frozenset({
            "magst du", "kennst du", "weisst du", "hast du", "bist du", "gibt es", "was", "welche", "welcher",
            "warum", "wer", "wie", "wo", "wann",
        }),
    },
    # French/Italian elisions are split into words first ("j'arrête" -> "j arrete").
    "fr": {
        "stop": frozenset({
            "arrete", "arrete toi", "arreter", "j arrete", "on arrete", "je veux arreter", "arrete le jeu",
            "c est fini", "c est termine", "fini", "termine", "j ai fini", "on a fini", "fin", "la fin",
            "ca suffit", "assez", "au revoir", "arrete s il te plait",
        }),
        "help": frozenset({
            "qu est ce que je peux dire", "qu est ce que je dis", "je dis quoi", "comment ca marche",
            "comment on joue", "a quoi on peut jouer", "on peut jouer a quoi", "quels jeux il y a",
        }),
        "lobby_help": frozenset({"aide", "aide moi", "je sais pas", "je ne sais pas", "sais pas", "quoi"}),
        "again": frozenset({
            "encore", "encore une fois", "encore une", "encore un", "une autre fois", "on recommence",
            "recommencer", "rejouer", "encore s il te plait",
        }),
        "different": frozenset({
            "autre chose", "quelque chose d autre", "un autre jeu", "autre jeu", "l autre", "l autre jeu",
            "un jeu different", "autre",
        }),
        "no": frozenset({"non", "non merci", "plutot pas"}),
        "switch_cues": frozenset({
            "on fait", "on joue", "je veux", "je voudrais", "jouons", "jouer", "faisons", "on peut", "changer",
            "plutot", "maintenant",
        }),
        "interrogatives": frozenset({
            "tu aimes", "tu connais", "tu sais", "tu as", "est ce que", "est ce qu", "qu est ce", "c est quoi",
            "pourquoi", "qui", "comment", "ou", "quand", "quel", "quelle", "quels", "quelles", "il y a",
        }),
    },
    "it": {
        "stop": frozenset({
            "basta", "basta cosi", "basta giocare", "ferma", "fermati", "smetti", "smettila", "voglio smettere",
            "ho finito", "abbiamo finito", "finito", "fine", "la fine", "ciao ciao", "arrivederci", "non piu",
        }),
        "help": frozenset({
            "cosa posso dire", "che cosa posso dire", "cosa dico", "che dico", "come funziona", "come si gioca",
            "a cosa possiamo giocare", "che giochi ci sono", "quali giochi ci sono",
        }),
        "lobby_help": frozenset({"aiuto", "aiutami", "non lo so", "non so", "cosa", "che"}),
        "again": frozenset({
            "ancora", "ancora una volta", "di nuovo", "un altra volta", "un altra", "un altro giro",
            "ancora per favore",
        }),
        "different": frozenset({
            "qualcos altro", "qualcosa di diverso", "un altro gioco", "altro gioco", "l altro", "un gioco diverso",
            "altro",
        }),
        "no": frozenset({"no grazie", "meglio di no"}),
        "switch_cues": frozenset({
            "facciamo", "giochiamo", "voglio", "vorrei", "possiamo", "cambiamo", "cambiare", "adesso", "ora",
            "invece", "preferisco", "giocare",
        }),
        "interrogatives": frozenset({
            "ti piace", "ti piacciono", "conosci", "sai", "hai", "cosa", "che cosa", "perche", "chi", "come",
            "dove", "quando", "quale", "quali", "c e",
        }),
    },
    "pt": {
        "stop": frozenset({
            "para", "pare", "parar", "chega", "basta", "quero parar", "acabei", "ja acabei", "terminei",
            "ja terminei", "acabou", "fim", "o fim", "nao quero mais", "tchau", "adeus", "ate logo",
            "para de jogar",
        }),
        "help": frozenset({
            "o que posso dizer", "o que eu posso dizer", "o que eu digo", "o que digo", "como funciona",
            "como se joga", "o que podemos jogar", "a que podemos jogar", "que jogos ha", "que jogos tem",
        }),
        "lobby_help": frozenset({"ajuda", "ajuda me", "me ajuda", "nao sei", "eu nao sei", "o que", "que"}),
        "again": frozenset({
            "outra vez", "de novo", "mais uma vez", "mais uma", "outra ronda", "outra rodada",
            "outra vez por favor", "mais",
        }),
        "different": frozenset({"outra coisa", "outro jogo", "um jogo diferente", "o outro", "outro"}),
        "no": frozenset({"nao", "nao obrigado", "nao obrigada", "melhor nao"}),
        "switch_cues": frozenset({
            "vamos", "quero", "queria", "podemos", "jogar", "brincar", "mudar", "trocar", "agora", "prefiro",
            "em vez disso",
        }),
        "interrogatives": frozenset({
            "voce gosta", "tu gostas", "gostas", "voce conhece", "conheces", "voce sabe", "sabes", "o que",
            "qual", "quais", "por que", "porque", "quem", "como", "onde", "quando", "tem", "ha",
        }),
    },
}
_EXTRA_ACTIVITY_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "es": {
        "ispy": ("veo veo",),
        "story": ("cuento", "cuentos", "historia", "historias"),
        "riddles": ("adivinanza", "adivinanzas", "acertijo", "acertijos"),
        "quiz": ("concurso", "preguntas"),
        "calm": ("calma", "respirar", "relajarse", "tranquilo", "tranquila"),
        "buddy": ("charlar", "hablar", "conversar"),
    },
    "de": {
        "ispy": ("ich sehe was", "ich sehe was was du nicht siehst"),
        "story": ("geschichte", "geschichten", "marchen"),
        "riddles": ("ratsel", "ratseln"),
        "quiz": ("fragen",),
        "calm": ("ruhig", "ruhe", "atmen", "entspannen", "entspannung"),
        "buddy": ("reden", "plaudern", "quatschen"),
    },
    "fr": {
        "ispy": ("je vois je vois", "devine ce que je vois"),
        "story": ("histoire", "histoires", "conte", "contes"),
        "riddles": ("devinette", "devinettes", "enigme", "enigmes"),
        "quiz": ("questions",),
        "calm": ("calme", "respirer", "respiration", "se detendre", "detente"),
        "buddy": ("discuter", "parler", "bavarder"),
    },
    "it": {
        "ispy": ("vedo vedo", "io vedo"),
        "story": ("storia", "storie", "favola", "favole", "racconto"),
        "riddles": ("indovinello", "indovinelli"),
        "quiz": ("domande",),
        "calm": ("calma", "respirare", "rilassarsi"),
        "buddy": ("chiacchierare", "parlare", "chiacchiere"),
    },
    "pt": {
        "ispy": ("eu vejo eu vejo", "vejo vejo", "adivinha o que eu vejo"),
        "story": ("historia", "historias", "conto", "contos"),
        "riddles": ("adivinhas", "adivinhacao", "adivinhacoes", "charada", "charadas"),
        "quiz": ("perguntas",),
        "calm": ("calma", "respirar", "relaxar"),
        "buddy": ("conversar", "falar", "conversa"),
    },
}
# NFKD has no decomposition for these letters; ASCII folding would drop them ("weiß" -> "wei").
_LETTER_FOLDS: dict[str, tuple[tuple[str, str], ...]] = {
    "de": (("ß", "ss"), ("ẞ", "SS")),
    "fr": (("œ", "oe"), ("Œ", "OE"), ("æ", "ae"), ("Æ", "AE")),
}
# Elided words ("l'histoire", "un'altra") are separate words; splitting only ever adds words.
_ELISION_LANGUAGES = frozenset({"fr", "it"})


def _phrases(base: frozenset[str], language: str, kind: str) -> frozenset[str]:
    return base | _EXTRA_PHRASES.get(language, {}).get(kind, frozenset())


def _activity_words(language: str) -> dict[str, tuple[str, ...]]:
    extra = _EXTRA_ACTIVITY_WORDS.get(language, {})
    return {activity: words + extra.get(activity, ()) for activity, words in _ACTIVITY_WORDS.items()}


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


def mentioned_activity(text: str, language: str = "") -> str:
    """Return the single activity a short utterance names, or '' when none/ambiguous."""
    found = {
        activity for activity, words in _activity_words(language).items() if any(_mentions(text, w) for w in words)
    }
    return next(iter(found)) if len(found) == 1 else ""


def parse_host_command(text: str, *, phase: str, language: str = "") -> HostCommand:
    """Recognise only direct, short child commands; quoted or long speech stays with the game.

    ``language`` is the immutable session language; ES/DE/FR/IT/PT add their own phrases.
    """
    if not isinstance(text, str) or any(mark in text for mark in "\"“”«»"):
        return HostCommand("none")
    for letter, folded in _LETTER_FOLDS.get(language, ()):
        text = text.replace(letter, folded)
    if language in _ELISION_LANGUAGES:
        text = re.sub(r"['’]", " ", text)
    clean = _normalise(text).strip(" '")
    if not clean or len(clean.split()) > _MAX_COMMAND_WORDS:
        return HostCommand("none")
    if clean in _phrases(_STOP_PHRASES, language, "stop"):
        return HostCommand("stop")
    if clean in _phrases(_HELP_PHRASES, language, "help"):
        return HostCommand("help")
    choosing = phase in {"lobby", "offer", "waiting"}
    activity = mentioned_activity(clean, language)
    different = _phrases(_DIFFERENT_PHRASES, language, "different")
    if choosing:
        if clean in _phrases(_LOBBY_HELP_WORDS, language, "lobby_help"):
            return HostCommand("help")
        if clean in _phrases(_AGAIN_PHRASES, language, "again"):
            return HostCommand("again")
        if clean in different:
            return HostCommand("different")
        if clean in _phrases(_NO_PHRASES, language, "no"):
            return HostCommand("no")
        if activity:
            return HostCommand("switch", activity)
        return HostCommand("none")
    # Mid-game: switching needs a named activity plus an explicit switch cue, or just the name.
    # Questions about a game ("do you like stories?") are conversation, never a switch.
    interrogatives = _INTERROGATIVE_STARTS + tuple(
        f"{opener} " for opener in sorted(_EXTRA_PHRASES.get(language, {}).get("interrogatives", ()))
    )
    cues = _SWITCH_CUES + tuple(sorted(_EXTRA_PHRASES.get(language, {}).get("switch_cues", ())))
    if activity and not clean.startswith(interrogatives) and (
        any(clean == cue or clean.startswith(cue + " ") for cue in cues)
        or clean in _activity_words(language)[activity]
    ):
        return HostCommand("switch", activity)
    if clean in different:
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
    "es": {
        "buddy": "charlar", "story": "un cuento", "quiz": "un concurso", "riddles": "adivinanzas",
        "calm": "respirar con calma", "ispy": "veo, veo",
    },
    "de": {
        "buddy": "plaudern", "story": "eine Geschichte", "quiz": "ein Quiz", "riddles": "Rätsel",
        "calm": "ruhig atmen", "ispy": "ich sehe was, was du nicht siehst",
    },
    "fr": {
        "buddy": "discuter", "story": "une histoire", "quiz": "un quiz", "riddles": "des devinettes",
        "calm": "respirer calmement", "ispy": "je vois, je vois",
    },
    "it": {
        "buddy": "chiacchierare", "story": "una storia", "quiz": "un quiz", "riddles": "gli indovinelli",
        "calm": "respirare con calma", "ispy": "vedo, vedo",
    },
    "pt": {
        "buddy": "conversar", "story": "uma história", "quiz": "um quiz", "riddles": "adivinhas",
        "calm": "respirar com calma", "ispy": "eu vejo, eu vejo",
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
    "es": {
        "lobby": "¡Hola{name}! Soy tu presentador de juegos. ¿Hacemos {a} o {b}? También puedes decir basta.",
        "offer": "Esa ronda terminó. ¿Otra ronda de {a}, o {b}? O di basta para terminar.",
        "offer_retry": "Di otra vez para {a}, o di {b}. O di basta.",
        "help": "Puedes decir {a} o {b}, o nombrar otro juego. Di basta para terminar.",
        "help_playing": "Contéstame para seguir jugando. Puedes nombrar otro juego, o decir basta.",
        "start": "¡Vale, {a}!",
        "camera_needed": (
            "Para veo, veo, una persona adulta tiene que permitir primero la cámara. Puede hacerlo en la "
            "página de Kids. ¿Seguimos jugando?"
        ),
        "waiting": "Te espero aquí. Puedes decir un juego, o una persona adulta puede elegir uno.",
        "unavailable": "Ese juego no está listo ahora. ¿Hacemos {a} o {b}?",
    },
    "de": {
        "lobby": "Hallo{name}! Ich bin dein Spielleiter. Wollen wir {a} machen oder {b}? Du kannst auch stopp sagen.",
        "offer": "Diese Runde ist vorbei. Noch eine Runde {a}, oder {b}? Oder sag stopp zum Aufhören.",
        "offer_retry": "Sag nochmal für {a}, oder sag {b}. Oder sag stopp.",
        "help": "Du kannst {a} oder {b} sagen, oder ein anderes Spiel nennen. Sag stopp zum Aufhören.",
        "help_playing": (
            "Antworte mir einfach, dann spielen wir weiter. Du kannst ein anderes Spiel nennen oder stopp sagen."
        ),
        "start": "Okay, {a}!",
        "camera_needed": (
            "Für ich sehe was, was du nicht siehst muss ein Erwachsener zuerst die Kamera erlauben. Das geht "
            "auf der Kids-Seite. Wollen wir weiterspielen?"
        ),
        "waiting": "Ich warte hier. Du kannst ein Spiel nennen, oder ein Erwachsener wählt eins aus.",
        "unavailable": "Das Spiel geht gerade nicht. Wollen wir {a} machen oder {b}?",
    },
    "fr": {
        "lobby": "Salut{name} ! Je suis ton animateur de jeux. On fait {a} ou {b} ? Tu peux aussi dire stop.",
        "offer": "Cette partie est finie. Encore une partie de {a}, ou {b} ? Ou dis stop pour finir.",
        "offer_retry": "Dis encore pour {a}, ou dis {b}. Ou dis stop.",
        "help": "Tu peux dire {a} ou {b}, ou nommer un autre jeu. Dis stop pour finir.",
        "help_playing": "Réponds-moi pour continuer à jouer. Tu peux nommer un autre jeu, ou dire stop.",
        "start": "D'accord, {a} !",
        "camera_needed": (
            "Pour je vois, je vois, un adulte doit d'abord autoriser la caméra. Il peut le faire sur la "
            "page Kids. On continue à jouer ?"
        ),
        "waiting": "Je t'attends ici. Tu peux dire un jeu, ou un adulte peut en choisir un.",
        "unavailable": "Ce jeu n'est pas prêt pour l'instant. On fait {a} ou {b} ?",
    },
    "it": {
        "lobby": "Ciao{name}! Sono il tuo presentatore di giochi. Facciamo {a} o {b}? Puoi anche dire basta.",
        "offer": "Questo turno è finito. Ancora un turno di {a}, o {b}? Oppure di' basta per finire.",
        "offer_retry": "Di' ancora per {a}, oppure di' {b}. Oppure di' basta.",
        "help": "Puoi dire {a} o {b}, oppure nominare un altro gioco. Di' basta per finire.",
        "help_playing": "Rispondimi per continuare a giocare. Puoi nominare un altro gioco, oppure dire basta.",
        "start": "Va bene, {a}!",
        "camera_needed": (
            "Per vedo, vedo, un adulto deve prima permettere la telecamera. Può farlo nella pagina Kids. "
            "Continuiamo a giocare?"
        ),
        "waiting": "Ti aspetto qui. Puoi dire un gioco, oppure un adulto può sceglierne uno.",
        "unavailable": "Questo gioco non è pronto adesso. Facciamo {a} o {b}?",
    },
    "pt": {
        "lobby": "Olá{name}! Sou o teu apresentador de jogos. Fazemos {a} ou {b}? Também podes dizer chega.",
        "offer": "Esta ronda terminou. Mais uma ronda de {a}, ou {b}? Ou diz chega para terminar.",
        "offer_retry": "Diz outra vez para {a}, ou diz {b}. Ou diz chega.",
        "help": "Podes dizer {a} ou {b}, ou dizer outro jogo. Diz chega para terminar.",
        "help_playing": "Responde-me para continuar a jogar. Podes dizer outro jogo, ou dizer chega.",
        "start": "Está bem, {a}!",
        "camera_needed": (
            "Para eu vejo, eu vejo, um adulto tem de autorizar primeiro a câmara. Pode fazê-lo na página "
            "Kids. Continuamos a jogar?"
        ),
        "waiting": "Fico à espera aqui. Podes dizer um jogo, ou um adulto pode escolher um.",
        "unavailable": "Esse jogo não está pronto agora. Fazemos {a} ou {b}?",
    },
}


def activity_name(language: str, activity: str) -> str:
    return _NAMES[language].get(activity, activity)  # an unsupported language raises; never English


def host_line(language: str, key: str, *, a: str = "", b: str = "", nickname: str = "") -> str:
    """Return short fixed host copy in the selected language; activity ids become child names."""
    lang = language
    return _COPY[lang][key].format(
        a=activity_name(lang, a) if a in ACTIVITIES else a,
        b=activity_name(lang, b) if b in ACTIVITIES else b,
        name=f" {nickname}" if nickname else "",
    )
