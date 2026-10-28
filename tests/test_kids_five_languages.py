"""Spanish/German/French/Italian/Portuguese Kids sessions on the strict catalog and repaired baseline.

Synthetic transports, fake moderation/provider/runtime fixtures only; no robot media, provider,
credentials or network. Voice identifiers below are synthetic test strings, not provider voices.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest
from test_ispy_target_variety import CHAIR, FRAMES, SESSION, Request
from test_kids_game_master import load_bridge, run_kids_chat
from test_kids_mode import FakeRobot
from test_kids_story_ispy_speech import fixed_bridge, stream_url

from companion import kids_fixed_copy as catalog
from homebody import kids_acknowledgements as acks
from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeError
from homebody.ispy import validate_ispy_target
from homebody.kids_game_master import activity_name, host_line, parse_host_command
from homebody.kids_mode import KidsProfile, build_kids_prompt, kids_greeting, kids_message
from homebody.runtime import HermesVoiceRuntime
from homebody.voice_pipeline import kids_ack_voice_proof

NEW = ("es", "de", "fr", "it", "pt")
NAMES = {"es": "Spanish", "de": "German", "fr": "French", "it": "Italian", "pt": "Portuguese"}
JESSICA = "cgSgspJ2msm6clMCkdW9"
RUTH = "yO6w2xlECAQRFP6pX7Hw"
SYNTH = {language: f"synth{language.upper()}Voice01" for language in NEW}
FLASH = "eleven_flash_v2_5"
GOOD_PROOF = {"kids_voice_sha256": "b" * 64, "kids_tts_model": FLASH}


def digest(voice: str) -> str:
    return hashlib.sha256(voice.encode()).hexdigest()


def secrets_for(**voices: str) -> dict[str, str]:
    values = {"ELEVENLABS_KIDS_VOICE_ID": JESSICA, "ELEVENLABS_KIDS_NL_VOICE_ID": RUTH,
              "ELEVENLABS_API_KEY": "synthetic", "OPENAI_API_KEY": "synthetic"}
    values.update({f"ELEVENLABS_KIDS_{code.upper()}_VOICE_ID": voice for code, voice in voices.items()})
    return values


def voiced_bridge(monkeypatch, **voices: str):
    module, bridge, screened = fixed_bridge(monkeypatch)
    values = secrets_for(**voices)
    monkeypatch.setattr(module, "_resolve_secret", lambda key, _profile: values.get(key, ""))
    return module, bridge, screened


def approve(language: str, lines=None, **extra: object) -> Request:
    return Request({"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": language,
                    "lines": lines if lines is not None else [{"kind": "greeting"}], **extra})


# ---- Catalog completeness and robot/bridge parity ----------------------------------------------


def test_catalog_is_complete_for_all_seven_languages_and_never_interpolates_free_text() -> None:
    assert catalog.LANGUAGES == ("en", "nl", "es", "de", "fr", "it", "pt")
    for table in (catalog.GREETINGS, catalog.NOTICES, catalog.ACTIVITY_NAMES, catalog.HOST_COPY):
        assert set(table) == set(catalog.LANGUAGES)
    for language in catalog.LANGUAGES:
        assert set(catalog.GREETINGS[language]) == set(catalog.ACTIVITIES)
        assert set(catalog.ACTIVITY_NAMES[language]) == set(catalog.ACTIVITIES)
        assert set(catalog.HOST_COPY[language]) == set(catalog.HOST_SLOTS)
        assert set(catalog.NOTICES[language]) == {"five_minutes"}
        for line in catalog.HOST_COPY[language].values():
            assert "{name}" not in line  # the caregiver-typed nickname is never part of approved speech
    for language in NEW:
        for activity in catalog.ACTIVITIES:
            assert catalog.GREETINGS[language][activity] != catalog.GREETINGS["en"][activity]


@pytest.mark.parametrize("language", NEW)
@pytest.mark.parametrize(
    "segment",
    [{"kind": "text", "text": "Hola"}, {"kind": "greeting", "nickname": "Ana"}, {"kind": "greeting", "voice": JESSICA},
     {"kind": "host", "key": "lobby", "a": "story", "b": "a dragon"}, {"kind": "notice", "key": "other"}],
)
def test_catalog_refuses_free_text_nickname_voice_and_unknown_selectors(language, segment) -> None:
    with pytest.raises(ValueError):
        catalog.render_segment(segment, activity="story", language=language)


@pytest.mark.parametrize("language", ["sv", "EN", "pt-BR", ""])
def test_catalog_refuses_unsupported_languages(language) -> None:
    with pytest.raises(ValueError):
        catalog.render_segment({"kind": "greeting"}, activity="story", language=language)


@pytest.mark.parametrize("language", NEW)
def test_robot_copy_prompt_and_names_exist_and_never_fall_back_to_english(language) -> None:
    assert NAMES[language] in build_kids_prompt(KidsProfile(language=language))
    assert kids_message(language, "five_minutes") != kids_message("en", "five_minutes")
    for activity in catalog.ACTIVITIES:
        assert activity_name(language, activity) != activity_name("en", activity)
    for key in catalog.HOST_SLOTS:
        assert host_line(language, key, a="story", b="riddles") != host_line("en", key, a="story", b="riddles")
    for unsupported in ("sv", ""):
        with pytest.raises(KeyError):
            kids_message(unsupported, "five_minutes")
        with pytest.raises(KeyError):
            host_line(unsupported, "lobby", a="story", b="riddles")
    with pytest.raises(ValueError):
        KidsProfile(language="sv")


# ---- Bridge voices: own voice only, proofs, refusal ----------------------------------------------


def test_new_languages_never_fall_back_to_english_and_en_nl_are_unchanged(monkeypatch) -> None:
    module, bridge, _ = voiced_bridge(monkeypatch, es=SYNTH["es"])
    assert module._kids_stream_voice(None, "es") == SYNTH["es"]
    for language in ("de", "fr", "it", "pt"):
        assert module._kids_stream_voice(None, language) == ""  # unset: no Jessica/Ruth/default substitute
        assert module._kids_voice_proof(None, language) == {}
    assert module._kids_stream_voice(None, "en") == JESSICA
    assert module._kids_stream_voice(None, "nl") == RUTH
    assert module._kids_stream_voice(None) == JESSICA
    assert module._kids_legacy_voice_proof(None) == {}  # EN != NL (Ruth): unchanged legacy rule
    proofs = {language: module._kids_voice_proof(None, language) for language in catalog.LANGUAGES}
    assert proofs["es"] == {"kids_voice_sha256": digest(SYNTH["es"]), "kids_tts_model": FLASH}
    assert proofs["nl"]["kids_voice_sha256"] == digest(RUTH)


@pytest.mark.parametrize("language", NEW)
def test_invalid_new_language_voice_id_fails_closed(monkeypatch, language) -> None:
    module, _bridge, _ = voiced_bridge(monkeypatch, **{language: "bad/../voice"})
    assert module._kids_stream_voice(None, language) == "" and module._kids_voice_proof(None, language) == {}


@pytest.mark.parametrize("language", NEW)
def test_fresh_proof_endpoint_proves_only_the_requested_language(monkeypatch, language) -> None:
    module, bridge, _ = voiced_bridge(monkeypatch, **{language: SYNTH[language]})
    bridge.require_auth = lambda _request: None
    response = asyncio.run(bridge.kids_voice_proof(type("R", (), {"query": {"language": language}})()))
    assert json.loads(response.text) == {"kids_voice_sha256": digest(SYNTH[language]), "kids_tts_model": FLASH}
    with pytest.raises(module.web.HTTPBadRequest):
        asyncio.run(bridge.kids_voice_proof(type("R", (), {"query": {"language": language, "voice": JESSICA}})()))


# ---- Fixed speech: selectors only, pinned profile, own voice -------------------------------------


@pytest.mark.parametrize("language", NEW)
def test_fixed_greeting_is_catalog_rendered_moderated_and_streams_only_in_its_own_voice(monkeypatch, language):
    module, bridge, screened = voiced_bridge(monkeypatch, **{language: SYNTH[language]})
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve(language))).text)
    text = catalog.GREETINGS[language]["story"]
    assert payload["text"] == text and screened == [text]
    assert stream_url(module, bridge, text, payload["speech_approval"]).endswith(f"/{SYNTH[language]}/stream")
    # The configured (adult/English) fallback is never used for this language.
    spoken: list[object] = []

    async def speech_response(body):
        spoken.append(body)
        return module.web.Response(body=b"x")

    bridge._speech_response = speech_response
    with pytest.raises(module.web.HTTPServiceUnavailable):
        asyncio.run(bridge.kids_speech_fallback(Request({
            "input": text, "session_id": SESSION, "speech_approval": payload["fallback_speech_approval"]})))
    assert spoken == []


@pytest.mark.parametrize("language", NEW)
def test_unconfigured_language_voice_refuses_stream_and_consumes_the_approval(monkeypatch, language):
    module, bridge, _ = voiced_bridge(monkeypatch)  # no voice for any new language
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve(language))).text)
    with pytest.raises(module.web.HTTPServiceUnavailable):
        stream_url(module, bridge, payload["text"], payload["speech_approval"])
    assert payload["speech_approval"] not in bridge._kids_speech_approvals  # single use, never retried


@pytest.mark.parametrize("language", NEW)
def test_fixed_speech_profile_language_is_immutable_and_caller_fields_are_refused(monkeypatch, language):
    module, bridge, screened = voiced_bridge(monkeypatch, **SYNTH)
    asyncio.run(bridge.kids_speech_approve(approve(language)))
    for other in ("en", "nl", "es" if language != "es" else "de"):
        with pytest.raises(module.web.HTTPConflict):
            asyncio.run(bridge.kids_speech_approve(approve(other)))
    for extra in ({"voice": JESSICA}, {"input": "Hola"}, {"nickname": "Ana"}, {"model": "eleven_v3"}):
        with pytest.raises(module.web.HTTPBadRequest):
            asyncio.run(bridge.kids_speech_approve(approve(language, **extra)))
    with pytest.raises(module.web.HTTPBadRequest):
        asyncio.run(bridge.kids_speech_approve(approve(language, [{"kind": "text", "text": "Hola"}])))
    assert len(screened) == 1


@pytest.mark.parametrize("language", NEW)
def test_ended_session_gets_no_fixed_approval_after_moderation_returns(monkeypatch, language):
    module, bridge, _ = voiced_bridge(monkeypatch, **SYNTH)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def moderation(_text, _key):
            entered.set()
            await release.wait()
            return False

        bridge._moderation_flagged = moderation
        pending = asyncio.create_task(bridge.kids_speech_approve(approve(language)))
        await asyncio.wait_for(entered.wait(), 2)
        await bridge.kids_session(Request({"session_id": SESSION, "state": "ended"}))
        release.set()
        with pytest.raises(module.web.HTTPConflict):
            await asyncio.wait_for(pending, 2)
        assert not bridge._kids_speech_approvals and SESSION not in bridge._kids_live

    asyncio.run(scenario())


# ---- Generated turns: prompt, safety, retry and language immutability ---------------------------


def chat_body(language: str, activity: str = "story", **extra: object) -> dict[str, object]:
    return {"input": "un dragón", "session_id": "kids-" + "e" * 32,
            "profile": {"age_band": "7-9", "activity": activity, "language": language}, **extra}


@pytest.mark.parametrize("language", NEW)
def test_generated_turn_uses_language_prompt_and_binds_approval_language(monkeypatch, language):
    _module, bridge = load_bridge(monkeypatch)
    response, requests = run_kids_chat(bridge, chat_body(language, hosted=True),
                                       json.dumps({"reply": "Vale.", "game_state": "playing"}))
    payload = json.loads(response.text)
    assert f"Speak in {NAMES[language]}" in requests[0]["messages"][0]["content"]
    assert bridge._kids_speech_approvals[payload["speech_approval"]]["language"] == language


@pytest.mark.parametrize("language", NEW)
def test_flagged_input_and_output_get_catalog_language_safety_copy(monkeypatch, language):
    module, bridge = load_bridge(monkeypatch)
    response, requests = run_kids_chat(bridge, chat_body(language, input="unsafe"), "x", flagged=("unsafe",))
    assert json.loads(response.text)["text"] == module._KIDS_COPY[language]["safety"] and requests == []
    _module, bridge = load_bridge(monkeypatch)
    response, _ = run_kids_chat(bridge, chat_body(language), "bad words", flagged=("bad words",))
    assert json.loads(response.text)["text"] == module._KIDS_COPY[language]["safety"]


@pytest.mark.parametrize("language", NEW)
def test_malformed_model_turn_retries_in_the_session_language_without_history(monkeypatch, language):
    module, bridge = load_bridge(monkeypatch)
    response, _ = run_kids_chat(bridge, chat_body(language, hosted=True), "not json")
    payload = json.loads(response.text)
    assert payload["retry"] is True and payload["game_state"] == "playing"
    assert payload["text"] == module._KIDS_MODEL_RETRY_REPLY[language]
    assert payload["text"] != module._KIDS_MODEL_RETRY_REPLY["en"]
    assert bridge._kids_sessions["kids-" + "e" * 32]["history"] == []


def test_turn_rejects_unsupported_language_and_any_language_change(monkeypatch):
    module, bridge = load_bridge(monkeypatch)
    with pytest.raises(module.web.HTTPBadRequest):
        run_kids_chat(bridge, chat_body("sv"), "x")
    run_kids_chat(bridge, chat_body("fr"), "D'accord.")
    for other in ("de", "en", "nl"):
        with pytest.raises(module.web.HTTPConflict):
            run_kids_chat(bridge, chat_body(other), "Okay.")


# ---- I Spy: per-language native schema, hints, classification -----------------------------------


@pytest.mark.parametrize("language", NEW)
@pytest.mark.parametrize("refuse_hint", [False, True])
def test_native_subscription_wire_schema_requires_session_language_hints(monkeypatch, language, refuse_hint):
    from test_kids_turn_deadlines_lifecycle import subscription_harness

    with_hints = dict(CHAIR, **{f"hints_{language}": ["Ok"]})
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [with_hints]})
    request = Request({"session_id": SESSION, "age_band": "7-9", "language": language, "frames_jpeg": list(FRAMES)})
    target = json.loads(asyncio.run(harness.bridge.kids_ispy_select(request)).text)["target"]
    assert target["object_name"] == "chair"
    [wire] = calls
    items = wire["text"]["format"]["schema"]["properties"]["candidates"]["items"]
    assert f"hints_{language}" in items["required"] and f"hints_{language}" in items["properties"]
    assert wire["text"]["format"]["strict"] is True
    assert f"hints_{language}" not in harness.module._ISPY_TARGET_SCHEMA["required"]  # original untouched
    assert target[f"hints_{language}"] == ["Ok"]
    # Object selection trusts the model; the actual outgoing hint is screened before speech approval.
    assert harness.moderated == []
    hint_request = Request({
        "session_id": SESSION, "input": "hint",
        "profile": {"age_band": "7-9", "activity": "ispy", "language": language},
    })
    harness.flagged = ("Ok",) if refuse_hint else ()
    reply = json.loads(asyncio.run(harness.bridge.kids_chat(hint_request)).text)
    assert any("Ok" in text for text in harness.moderated)
    assert reply["screened"] is True and reply["speech_approval"]
    if refuse_hint:
        assert reply["text"] == harness.module._kids_copy(language, "safety")
        assert "Ok" not in reply["text"]
        assert harness.bridge._kids_sessions[SESSION]["ispy_hints_given"] == 0
    else:
        assert "Ok" in reply["text"] and reply["text"] in harness.moderated
        assert harness.bridge._kids_sessions[SESSION]["ispy_hints_given"] == 1
    # The single-use capability is bound to the approved final text, not the rejected hint.
    harness.bridge._consume_kids_speech_approval(reply["speech_approval"], SESSION, reply["text"])
    with pytest.raises(harness.module.web.HTTPForbidden):
        harness.bridge._consume_kids_speech_approval(reply["speech_approval"], SESSION, reply["text"])


@pytest.mark.parametrize("language", NEW)
def test_native_pool_without_session_language_hints_fails_closed(monkeypatch, language):
    from test_kids_turn_deadlines_lifecycle import subscription_harness

    harness, _calls, _ = subscription_harness(monkeypatch, {"candidates": [CHAIR]})
    request = Request({"session_id": SESSION, "age_band": "7-9", "language": language, "frames_jpeg": list(FRAMES)})
    with pytest.raises(harness.module.web.HTTPBadGateway):
        asyncio.run(harness.bridge.kids_ispy_select(request))
    assert SESSION not in harness.bridge._kids_sessions and harness.moderated == []


@pytest.mark.parametrize("language", NEW)
def test_outgoing_vision_schema_survives_the_strict_native_projection(monkeypatch, language):
    """Runs everywhere: the bridge's real request format through the repaired subscription projection."""
    from test_ispy_target_variety import Harness

    from companion.subscription_reasoning import native_text_format

    harness = Harness(monkeypatch, [dict(CHAIR, **{f"hints_{language}": ["Ok"]})])
    request = Request({"session_id": SESSION, "age_band": "7-9", "language": language, "frames_jpeg": list(FRAMES)})
    assert json.loads(asyncio.run(harness.bridge.kids_ispy_select(request)).text)["target"]["object_name"] == "chair"
    [sent] = harness.vision.requests
    wire = native_text_format(sent["response_format"])
    items = wire["schema"]["properties"]["candidates"]["items"]
    assert wire["strict"] is True and items["additionalProperties"] is False
    assert f"hints_{language}" in items["required"] and set(items["required"]) == set(items["properties"])
    assert sent["messages"][0]["content"] == []  # prompt and frames are cleared after the request
    other = next(code for code in NEW if code != language)
    assert f"hints_{other}" not in items["properties"]


def test_en_nl_keep_original_fields_and_unsafe_new_language_hints_are_dropped(monkeypatch):
    module, _bridge = load_bridge(monkeypatch)
    original = module._ISPY_TARGET_SCHEMA
    for language, name in (("en", "English"), ("nl", "Dutch")):
        target = module._ispy_response_schema(language)["properties"]["candidates"]["items"]
        assert target["required"] == original["required"]
        assert set(target["properties"]) == set(original["properties"])
        assert name in target["properties"]["object_name"]["description"]
    unsafe = {"de": "neben dem Telefon", "fr": "près du téléphone", "it": "vicino al telefono",
              "pt": "ao lado do telemóvel", "es": "junto al teléfono"}
    for language, hint in unsafe.items():
        pool = module._ispy_candidate_pool(
            {"candidates": [dict(CHAIR, **{f"hints_{language}": [hint]})]}, frame_count=5, language=language)
        assert pool[0][f"hints_{language}"] == [hint]


def test_ispy_reply_classification_and_clues_are_localized(monkeypatch):
    module, _bridge = load_bridge(monkeypatch)
    target = dict(CHAIR, colour="red", hints_es=["Rebota"], hints_de=["Es springt"], hints_pt=["Salta"])
    answer = module._ispy_reply(target, language="es", matched=False, previous_count=0)[0]
    assert answer == "Buen intento, pero todavía no. Nueva pista: Rebota. Te quedan 2 intentos."
    assert module._ispy_reply(target, language="de", matched=False, previous_count=0, intent="hint")[0].startswith(
        "Hier ist ein Tipp: Es springt.")
    # A French session with no French hints never reads the English hints; it gets the colour hint.
    assert module._ispy_hint_ladder(target, "fr") == ["Cherche quelque chose de rouge."]
    assert module._ispy_intent("me rindo", "es") == "give_up" and module._ispy_intent("me rindo", "en") == "answer"
    assert module._ispy_intent("Ich weiß es nicht", "de") == "give_up"
    assert module._ispy_intent("j'abandonne", "fr") == "give_up"
    assert module._ispy_intent("dammi un indizio", "it") == "hint"
    assert module._ispy_confirmation("sí", "es") == "yes" and module._ispy_confirmation("si", "en") == "unknown"
    assert module._ispy_confirmation("não", "pt") == "no"
    assert module._ispy_colour_clue(target, "de") == "Ich sehe was, was du nicht siehst, und das ist rot."
    assert module._ispy_reply(target, language="nl", matched=True, previous_count=0)[0] == "Ja, goed gezien!"
    assert module._ispy_colour_clue(target, "en") == "I spy with my little eye, something that is red."


@pytest.mark.parametrize("language", NEW)
def test_robot_ispy_target_requires_and_speaks_its_session_language(language):
    with pytest.raises(ValueError):
        validate_ispy_target(CHAIR, frame_count=5, language=language)
    target = validate_ispy_target(dict(CHAIR, **{f"hints_{language}": ["Ok"]}), frame_count=5, language=language)
    assert target.hints(language) == ("Ok",) and target.hints("en") == tuple(CHAIR["hints_en"])
    assert target.clue(language) != target.clue("en")


# ---- Host commands --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "language,text,phase,kind,activity",
    [
        ("es", "basta", "playing", "stop", ""), ("es", "¡Para!", "playing", "stop", ""),
        ("es", "¿Qué puedo decir?", "playing", "help", ""), ("es", "otra vez", "offer", "again", ""),
        ("es", "quiero un cuento", "playing", "switch", "story"),
        ("es", "¿te gustan los cuentos?", "playing", "none", ""),
        ("de", "Hör auf!", "playing", "stop", ""), ("de", "Ich weiß es nicht", "lobby", "help", ""),
        ("de", "Ich weiss es nicht", "offer", "help", ""), ("de", "ICH WEIẞ ES NICHT", "waiting", "help", ""),
        ("de", "Ich weiß es nicht", "playing", "none", ""),
        ("de", "lass uns Rätsel machen", "playing", "switch", "riddles"),
        ("de", "magst du Geschichten", "playing", "none", ""),
        ("fr", "J’arrête", "playing", "stop", ""), ("fr", "l'histoire", "lobby", "switch", "story"),
        ("fr", "on fait des devinettes", "playing", "switch", "riddles"),
        ("fr", "est-ce que tu connais une devinette", "playing", "none", ""),
        ("fr", "le dragon a dit stop et il est parti", "playing", "none", ""),
        ("it", "un'altra volta", "offer", "again", ""),
        ("it", "facciamo gli indovinelli", "playing", "switch", "riddles"),
        ("it", "Perché basta?", "playing", "none", ""),
        ("pt", "Chega!", "playing", "stop", ""), ("pt", "não, obrigado", "offer", "no", ""),
        ("pt", "vamos fazer adivinhas", "playing", "switch", "riddles"),
        ("pt", "a princesa disse chega e foi embora", "playing", "none", ""),
    ],
)
def test_new_language_host_commands(language, text, phase, kind, activity):
    command = parse_host_command(text, phase=phase, language=language)
    assert (command.kind, command.activity) == (kind, activity)


@pytest.mark.parametrize("language", NEW)
def test_quoted_and_long_narration_stay_with_the_game(language):
    stop = {"es": "basta", "de": "stopp", "fr": "arrête", "it": "basta", "pt": "chega"}[language]
    assert parse_host_command(f'"{stop}"', phase="playing", language=language).kind == "none"
    narration = f"and then the brave little dragon said {stop} very loudly to the moon tonight"
    assert parse_host_command(narration, phase="playing", language=language).kind == "none"


def test_new_language_phrases_do_not_change_english_or_dutch_sessions():
    for language in ("en", "nl", ""):
        for text in ("basta", "Tschüss", "chega", "arrête", "Ich weiß es nicht"):
            assert parse_host_command(text, phase="playing", language=language).kind == "none"
        assert parse_host_command("adivinanzas", phase="lobby", language=language).kind == "none"
    assert parse_host_command("stoppen", phase="playing", language="nl").kind == "stop"


# ---- Robot runtime: admission before every side effect, Stop/End win -------------------------------


class ProofClient:
    def __init__(self, proof, calls):
        self.proof, self.calls = proof, calls

    def kids_voice_proof(self, *, timeout, language):
        self.calls.append(language)
        if isinstance(self.proof, Exception):
            raise self.proof
        return self.proof

    def close(self):
        pass


def runtime_with(monkeypatch, proof):
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    calls, queued, effects = [], [], []
    monkeypatch.setattr(runtime, "config_loader", lambda: AppConfig())
    monkeypatch.setattr(runtime, "_new_bridge_client", lambda _config: ProofClient(proof, calls))
    monkeypatch.setattr(runtime, "queue_announcement", lambda text, **kwargs: queued.append((text, kwargs)))
    monkeypatch.setattr(runtime, "_notify_bridge_kids_session_async", lambda *_a, **_k: effects.append("bridge"))
    original_presentation, original_camera = runtime.stop_presentation_window, runtime.revoke_camera_control
    monkeypatch.setattr(runtime, "stop_presentation_window",
                        lambda *a, **k: effects.append("presentation") or original_presentation(*a, **k))
    monkeypatch.setattr(runtime, "revoke_camera_control",
                        lambda *a, **k: effects.append("camera") or original_camera(*a, **k))
    return runtime, calls, queued, effects


@pytest.mark.parametrize("language", NEW)
@pytest.mark.parametrize(
    "proof",
    [{}, None, {"kids_voice_sha256": "a" * 64, "kids_tts_model": "eleven_multilingual_v2"},
     {"kids_voice_sha256": "A" * 64, "kids_tts_model": FLASH}, HermesBridgeError("offline")],
)
def test_start_without_own_voice_proof_has_no_side_effects(monkeypatch, language, proof):
    runtime, calls, queued, effects = runtime_with(monkeypatch, proof)
    with pytest.raises(RuntimeError, match=NAMES[language]):
        runtime.start_kids_mode(KidsProfile(activity="story", language=language, host=True))
    assert calls == [language] and queued == [] and effects == []
    kids = runtime.status()["kids_mode"]
    assert kids["active"] is False and kids["locked"] is False
    assert runtime._kids_session_id == "" and runtime._kids_timer is None and runtime._kids_first_listen is None


@pytest.mark.parametrize("language", NEW)
def test_admitted_start_speaks_catalog_selectors_bound_to_the_session(monkeypatch, language):
    runtime, calls, queued, _effects = runtime_with(monkeypatch, GOOD_PROOF)
    runtime.start_kids_mode(KidsProfile(activity="story", language=language, nickname="Ana", host=True))
    try:
        assert calls == [language]
        text, kwargs = queued[0]
        assert text == host_line(language, "lobby", a="story", b="riddles")  # nickname never spoken
        assert "Ana" not in text
        assert kwargs["kids_voice_session_id"] == runtime._kids_session_id
        assert kwargs["kids_speech_lines"] == ({"kind": "host", "key": "lobby", "a": "story", "b": "riddles"},)
        assert not {"voice", "provider", "model"} & set(kwargs)
    finally:
        runtime.stop_kids_mode(fold=False)


@pytest.mark.parametrize("language", ["en", "nl"])
def test_en_nl_start_needs_no_per_language_admission(monkeypatch, language):
    runtime, calls, queued, _effects = runtime_with(monkeypatch, {})
    runtime.start_kids_mode(KidsProfile(activity="story", language=language))
    try:
        assert calls == [] and queued
    finally:
        runtime.stop_kids_mode(fold=False)


@pytest.mark.parametrize("language", NEW)
@pytest.mark.parametrize("boundary", ["stop", "end"])
def test_stop_or_end_wins_while_voice_admission_is_held(monkeypatch, language, boundary):
    runtime, _calls, queued, effects = runtime_with(monkeypatch, GOOD_PROOF)
    entered, release = threading.Event(), threading.Event()
    original = ProofClient.kids_voice_proof
    errors: list[Exception] = []

    def held(client, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(client, **kwargs)

    monkeypatch.setattr(ProofClient, "kids_voice_proof", held)

    def start():
        try:
            runtime.start_kids_mode(KidsProfile(language=language, activity="story"))
        except RuntimeError as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    try:
        worker.start()
        assert entered.wait(2)
        if boundary == "stop":
            runtime.stop_speaking()
        else:
            runtime.stop_kids_mode(reason="parent", fold=False)
        release.set()
        worker.join(4)
        assert not worker.is_alive() and errors, "a late voice proof resurrected the cancelled Start"
        assert runtime.status()["kids_mode"]["active"] is False
        assert queued == [] and runtime._kids_timer is None and runtime._kids_first_listen is None
        assert "bridge" not in effects
        # A deliberate later caregiver Start still works: no permanent admission latch.
        runtime.start_kids_mode(KidsProfile(language=language, activity="story"), greet=False)
        assert runtime.status()["kids_mode"]["active"] is True
    finally:
        release.set()
        worker.join(4)
        runtime.stop_kids_mode(fold=False)


# ---- Recognition transport and waiting clips ------------------------------------------------------


def test_recognition_transport_accepts_supported_codes_only():
    from homebody import asr_worker, stt_routing
    from homebody.robot_stt import RobotSttClient

    for language in NEW:
        assert asr_worker._LANGUAGES[language] == language
        kids = AppConfig(kids_mode_enabled=True, kids_age_band="7-9", kids_activity="story", language=language,
                         stt_provider="robot")
        assert stt_routing.robot_language(kids) == language
    assert "sv" not in asr_worker._LANGUAGES
    client = RobotSttClient.__new__(RobotSttClient)
    with pytest.raises(ValueError):
        client.transcribe([], session="s", generation=1, language="sv", cancelled=lambda: False)


def test_missing_new_language_waiting_bundles_stay_silent_and_originals_are_untouched(monkeypatch, tmp_path):
    from test_kids_stt_acknowledgements import write_catalog

    monkeypatch.setattr(acks, "_catalog", None)
    loaded = acks.catalog()
    assert all(loaded[language] == () for language in NEW)  # no clips are shipped for new languages
    assert len(loaded["en"]) == 3 and len(loaded["nl"]) == 6
    # A synthetic bundle placed in a new-language directory may not carry English/Dutch clips.
    assert acks.load_catalog(write_catalog(tmp_path / "fr"), ("fr",)) == {"fr": ()}
    legacy = {"kids_voice_sha256": "a" * 64, "kids_tts_model": FLASH}
    for language in NEW:
        assert kids_ack_voice_proof(legacy, language) == (None, None)
    assert kids_ack_voice_proof(legacy, "nl") == ("a" * 64, FLASH)


def test_greeting_parity_holds_with_a_nickname_only_on_the_robot_preview():
    for language in NEW:
        profile = KidsProfile(activity="story", language=language, nickname="Ana")
        assert kids_greeting(profile).endswith(catalog.GREETINGS[language]["story"])
        assert "Ana" not in catalog.GREETINGS[language]["story"]
