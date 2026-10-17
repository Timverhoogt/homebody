"""Server-authored fixed Kids copy: the only robot-initiated lines the bridge approves for the Kids voice.

Reachy sends selectors (kind, key, activity ids), never text. The bridge renders the line from this
catalog, so an approval can only ever cover deterministic copy chosen here. Nothing here interpolates
caller-supplied free text: host-line slots accept activity ids only, and the I Spy clue is rendered by
the bridge from its own validated target. The robot keeps matching local copy for its status preview;
tests pin both catalogs together.
"""

from __future__ import annotations

ACTIVITIES = ("buddy", "story", "quiz", "riddles", "calm", "ispy")
LANGUAGES = ("en", "nl", "es", "de", "fr", "it", "pt")
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
    # ES/DE/FR/IT/PT (Portuguese in provisional European forms) await native-speaker review.
    "es": {
        "buddy": "El modo infantil está listo. ¿De qué te gustaría hablar?",
        "story": "Es la hora del cuento. ¿De quién será nuestro cuento?",
        "quiz": "Es la hora del concurso. ¿Cuál es tu asignatura favorita?",
        "riddles": "Es la hora de las adivinanzas. ¿Te digo la primera adivinanza?",
        "calm": "Es la hora de la calma. Siéntate cómodo. ¿Estás listo para empezar?",
        "ispy": (
            "Veo, veo está listo. Voy a mirar un momento solo un objeto seguro de la casa, "
            "y luego la cámara se apaga."
        ),
    },
    "de": {
        "buddy": "Der Kindermodus ist bereit. Worüber möchtest du reden?",
        "story": "Geschichtenzeit ist bereit. Um wen soll unsere Geschichte gehen?",
        "quiz": "Quizzeit ist bereit. Was ist dein Lieblingsfach?",
        "riddles": "Rätselzeit ist bereit. Soll ich dir das erste Rätsel erzählen?",
        "calm": "Ruhezeit ist bereit. Setz dich gemütlich hin. Bist du bereit anzufangen?",
        "ispy": (
            "Ich sehe was, was du nicht siehst ist bereit. Ich schaue kurz nur nach einem sicheren "
            "Gegenstand im Haus, dann geht die Kamera aus."
        ),
    },
    "fr": {
        "buddy": "Le mode enfant est prêt. De quoi veux-tu parler ?",
        "story": "C'est l'heure de l'histoire. De qui parlera notre histoire ?",
        "quiz": "C'est l'heure du quiz. Quelle est ta matière préférée ?",
        "riddles": "C'est l'heure des devinettes. Je te dis la première devinette ?",
        "calm": "C'est l'heure du calme. Installe-toi bien. Tu es prêt à commencer ?",
        "ispy": (
            "Je vois, je vois est prêt. Je vais regarder un petit moment un seul objet sûr de la maison, "
            "puis la caméra s'éteint."
        ),
    },
    "it": {
        "buddy": "La modalità bambini è pronta. Di cosa vuoi parlare?",
        "story": "È l'ora della storia. Di chi parlerà la nostra storia?",
        "quiz": "È l'ora del quiz. Qual è la tua materia preferita?",
        "riddles": "È l'ora degli indovinelli. Ti dico il primo indovinello?",
        "calm": "È l'ora della calma. Mettiti comodo. Sei pronto per cominciare?",
        "ispy": (
            "Vedo, vedo è pronto. Guarderò un momento solo un oggetto sicuro della casa, "
            "poi la telecamera si spegne."
        ),
    },
    "pt": {
        "buddy": "O modo infantil está pronto. Sobre o que queres conversar?",
        "story": "É a hora da história. Sobre quem vai ser a nossa história?",
        "quiz": "É a hora do quiz. Qual é a tua disciplina favorita?",
        "riddles": "É a hora das adivinhas. Posso dizer a primeira adivinha?",
        "calm": "É a hora da calma. Senta-te confortavelmente. Estás pronto para começar?",
        "ispy": (
            "Eu vejo, eu vejo está pronto. Vou olhar um momento só para um objeto seguro da casa, "
            "e depois a câmara desliga-se."
        ),
    },
}
NOTICES = {
    "en": {"five_minutes": "Five minutes left in Kids Mode. Let's finish this activity soon."},
    "nl": {"five_minutes": "Nog vijf minuten in de kindermodus. Laten we deze activiteit bijna afronden."},
    "es": {"five_minutes": "Quedan cinco minutos en el modo infantil. Vamos a terminar pronto esta actividad."},
    "de": {"five_minutes": "Noch fünf Minuten im Kindermodus. Lass uns diese Aktivität bald beenden."},
    "fr": {"five_minutes": "Plus que cinq minutes en mode enfant. Finissons bientôt cette activité."},
    "it": {"five_minutes": "Restano cinque minuti nella modalità bambini. Finiamo presto questa attività."},
    "pt": {"five_minutes": "Faltam cinco minutos no modo infantil. Vamos terminar esta atividade em breve."},
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
    "es": {
        "lobby": "¡Hola! Soy tu presentador de juegos. ¿Hacemos {a} o {b}? También puedes decir basta.",
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
        "lobby": "Hallo! Ich bin dein Spielleiter. Wollen wir {a} machen oder {b}? Du kannst auch stopp sagen.",
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
        "lobby": "Salut ! Je suis ton animateur de jeux. On fait {a} ou {b} ? Tu peux aussi dire stop.",
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
        "lobby": "Ciao! Sono il tuo presentatore di giochi. Facciamo {a} o {b}? Puoi anche dire basta.",
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
        "lobby": "Olá! Sou o teu apresentador de jogos. Fazemos {a} ou {b}? Também podes dizer chega.",
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
# Exactly which activity-id slots each host line takes.
HOST_SLOTS = {
    "lobby": ("a", "b"), "offer": ("a", "b"), "offer_retry": ("a", "b"), "help": ("a", "b"),
    "help_playing": (), "start": ("a",), "camera_needed": (), "waiting": (), "unavailable": ("a", "b"),
}


def selector_ids(segments: list[dict[str, object]]) -> str:
    """Catalog ids of already-rendered selectors for a log line, e.g. ``host:offer[a=story,b=quiz]``.

    Only call after rendering succeeded: every value is then a catalog key or activity id, never text.
    """
    ids = []
    for segment in segments:
        name = str(segment["kind"]) + (f":{segment['key']}" if "key" in segment else "")
        slots = ",".join(f"{slot}={segment[slot]}" for slot in ("a", "b") if slot in segment)
        ids.append(f"{name}[{slots}]" if slots else name)
    return "+".join(ids)


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
