"""Story turn failures, provider-unenforced I Spy pool bounds and Kids-voice fixed lines.

Synthetic fixtures only: fake model/vision/moderation/speech HTTP, no provider, robot or media.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from test_announcements import FakeRobot
from test_bridge import load_bridge_module
from test_ispy_target_variety import CHAIR, CUSHION, LAMP, SESSION, Harness, Request, candidate

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, HermesBridgeError, SpeechAudio
from homebody.runtime import HermesVoiceRuntime

RUTH = "yO6w2xlECAQRFP6pX7Hw"
JESSICA = "cgSgspJ2msm6clMCkdW9"


# --- I Spy: a provider that ignores maxItems/minItems -------------------------------------------


def test_oversized_provider_pool_is_rejected_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = [candidate("mug", colour="red", frame_index=4), candidate("ball", colour="green", frame_index=4)]
    harness = Harness(monkeypatch, [CHAIR, LAMP, CUSHION, candidate("bookshelf", colour="brown"), *extra])
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert harness.rng.seen == [] and harness.moderated == []


def test_oversized_pool_never_promotes_entries_past_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = [candidate("person"), candidate("phone"), candidate("knife"), candidate("vase", confidence=0.4)]
    harness = Harness(monkeypatch, [*unsafe, CHAIR])
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()  # schema-violating pool: no arbitrary prevalidation window
    assert harness.moderated == []
    assert SESSION not in harness.bridge._kids_sessions


@pytest.mark.parametrize("size", [0, 17])
def test_empty_or_runaway_pool_fails_closed(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    harness = Harness(monkeypatch, [CHAIR] * size)
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert harness.moderated == [] and SESSION not in harness.bridge._kids_sessions


def test_vision_prompt_states_the_bound_for_instruction_only_schema_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    prompts: list[str] = []
    post = harness.vision.post

    def capture(url: str, *, json: dict[str, object], **kwargs: object) -> object:
        prompts.append(str(json["messages"][0]["content"][0]["text"]))  # copied before frames are cleared
        return post(url, json=json, **kwargs)

    harness.vision.post = capture  # type: ignore[method-assign]
    harness.run()
    [text] = prompts
    assert text.startswith(f"List at most {harness.module._ISPY_MAX_CANDIDATES} ")


# --- Story: bounded model turn, localized retry, held-result Stop ------------------------------


class ModelHttp:
    """Fake text model + moderation transport; a model turn can time out, fail, or be held."""

    def __init__(self, mode: str = "ok", content: str = "Once upon a time there was a kind fox.") -> None:
        self.mode = mode
        self.content = content
        self.gate: asyncio.Event | None = None
        self.model_kwargs: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> object:
        outer = self

        class Response:
            status = 200

            async def __aenter__(self) -> Response:
                if "moderations" in url:
                    return self
                outer.model_kwargs.append(kwargs)
                if outer.gate is not None:
                    await outer.gate.wait()
                if outer.mode == "timeout":
                    raise TimeoutError
                if outer.mode == "http":
                    self.status = 504
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def json(self, **_kwargs: object) -> dict[str, object]:
                if "moderations" in url:
                    return {"results": [{"flagged": False}]}
                return {"choices": [{"message": {"content": outer.content}}]}

        return Response()


def story_bridge(monkeypatch: pytest.MonkeyPatch, http: ModelHttp):
    harness = Harness(monkeypatch, [CHAIR])  # synthetic keys and no vision URL
    bridge = harness.bridge
    bridge.http = http
    bridge._moderation_flagged = _never_flagged  # type: ignore[assignment]
    return harness.module, bridge


async def _never_flagged(_text: str, _key: str) -> bool:
    return False


def chat(language: str = "nl", *, hosted: bool = False, text: str = "En toen?") -> Request:
    payload: dict[str, object] = {
        "input": text,
        "session_id": SESSION,
        "profile": {"age_band": "7-9", "activity": "story", "language": language},
    }
    if hosted:
        payload["hosted"] = True
    return Request(payload)


@pytest.mark.parametrize("mode", ["timeout", "http"])
@pytest.mark.parametrize("language,hosted", [("nl", False), ("en", True)])
def test_failed_story_turn_speaks_localized_retry_without_touching_history(
    monkeypatch: pytest.MonkeyPatch, mode: str, language: str, hosted: bool
) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    first = json.loads(asyncio.run(bridge.kids_chat(chat(language, hosted=hosted))).text) if not hosted else None
    history = list(bridge._kids_sessions.get(SESSION, {}).get("history") or [])
    bridge.http = ModelHttp(mode)
    payload = json.loads(asyncio.run(bridge.kids_chat(chat(language, hosted=hosted))).text)
    assert payload["text"] == module._KIDS_MODEL_RETRY_REPLY[language]
    assert payload["retry"] is True and payload["screened"] is True
    assert payload["speech_approval"] != payload["fallback_speech_approval"]
    if hosted:
        assert payload["game_state"] == "playing"  # a failed turn never finishes a hosted round
    else:
        assert first is not None and "game_state" not in payload
    assert bridge._kids_sessions[SESSION]["history"] == history
    approval = bridge._kids_speech_approvals[payload["speech_approval"]]
    assert approval["language"] == language


def test_story_model_turn_carries_an_explicit_total_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    asyncio.run(bridge.kids_chat(chat()))
    timeout = bridge.http.model_kwargs[0]["timeout"]
    assert timeout.total == module._KIDS_MODEL_TIMEOUT_SECONDS <= 20.0


def test_hosted_invalid_outcome_is_never_guessed_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp(content="The end. game_state: finished"))
    payload = json.loads(asyncio.run(bridge.kids_chat(chat("en", hosted=True))).text)
    assert payload["game_state"] == "playing" and payload["text"] == module._KIDS_MODEL_RETRY_REPLY["en"]


@pytest.mark.parametrize("mode", ["ok", "timeout"])
@pytest.mark.parametrize("interrupt", ["end", "expire"])
def test_held_story_result_after_stop_gets_no_speech_approval(
    monkeypatch: pytest.MonkeyPatch, mode: str, interrupt: str
) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp(mode))

    async def scenario() -> BaseException | None:
        bridge.http.gate = asyncio.Event()
        task = asyncio.create_task(bridge.kids_chat(chat()))
        for _ in range(50):
            await asyncio.sleep(0)
            if bridge.http.model_kwargs:
                break
        if interrupt == "end":
            await bridge.kids_session(Request({"session_id": SESSION, "state": "ended"}))
        else:
            bridge._kids_live[SESSION] = time.monotonic() - module._KIDS_LIVE_MAX_SECONDS - 1
        bridge.http.gate.set()
        try:
            await task
        except BaseException as exc:  # noqa: BLE001 - the stale result must surface as an error
            return exc
        return None

    error = asyncio.run(scenario())
    assert isinstance(error, module.web.HTTPConflict)
    assert not bridge._kids_speech_approvals


# --- Fixed robot-authored lines use the session's Kids voice -----------------------------------


def fixed_bridge(monkeypatch: pytest.MonkeyPatch, *, flagged: bool = False):
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    values = {"ELEVENLABS_KIDS_VOICE_ID": JESSICA, "ELEVENLABS_KIDS_NL_VOICE_ID": RUTH,
              "ELEVENLABS_API_KEY": "synthetic", "OPENAI_API_KEY": "synthetic"}
    monkeypatch.setattr(module, "_resolve_secret", lambda key, _profile: values.get(key, ""))
    screened: list[str] = []

    async def moderation(text: str, _key: str) -> bool:
        screened.append(text)
        return flagged

    bridge._moderation_flagged = moderation  # type: ignore[assignment]
    return module, bridge, screened


NL_STORY_GREETING = "Verhaaltijd staat klaar. Over wie zal ons verhaal gaan?"


def approve(
    lines: list[dict[str, str]] | None = None, language: str = "nl", *, age_band: str = "7-9",
    activity: str = "story", **extra: object,
) -> Request:
    return Request({
        "session_id": SESSION, "age_band": age_band, "activity": activity, "language": language,
        "lines": lines if lines is not None else [{"kind": "greeting"}], **extra,
    })


def stream_url(module, bridge, text: str, token: str) -> str:
    urls: list[str] = []

    class Seen(Exception):
        pass

    class HTTP:
        def post(self, url: str, **_kwargs: object) -> None:
            urls.append(url)
            raise Seen()

    bridge.http = HTTP()
    with pytest.raises(Seen):
        asyncio.run(bridge.kids_speech_stream(Request({"input": text, "session_id": SESSION, "speech_approval": token,
                                                       "voice": "caller_override", "language": "en"})))
    return urls[0]


def test_dutch_greeting_is_server_rendered_moderated_and_streams_in_ruth(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge, screened = fixed_bridge(monkeypatch)
    text = NL_STORY_GREETING
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve())).text)
    assert screened == [text] and payload["text"] == text
    assert stream_url(module, bridge, text, payload["speech_approval"]).endswith(f"/{RUTH}/stream")
    with pytest.raises(module.web.HTTPForbidden):  # single-use
        stream_url(module, bridge, text, payload["speech_approval"])
    with pytest.raises(module.web.HTTPForbidden):  # exact text only
        stream_url(module, bridge, text + " Extra", payload["fallback_speech_approval"])


def test_english_fixed_line_keeps_the_english_kids_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch)
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve(language="en"))).text)
    assert payload["text"] == "Story time is ready. Who should our story be about?"
    assert stream_url(module, bridge, payload["text"], payload["speech_approval"]).endswith(f"/{JESSICA}/stream")


@pytest.mark.parametrize(
    "change", [{"language": "en"}, {"age_band": "4-6"}, {"activity": "quiz"}],
)
def test_pinned_profile_cannot_change_later_in_the_session(monkeypatch: pytest.MonkeyPatch, change) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch)
    asyncio.run(bridge.kids_speech_approve(approve()))
    with pytest.raises(module.web.HTTPConflict):
        asyncio.run(bridge.kids_speech_approve(approve(**{"language": "nl", **change})))
    payload = dict(chat("nl").payload)
    payload["profile"] = {**payload["profile"], **change}  # type: ignore[dict-item]
    with pytest.raises(module.web.HTTPConflict):
        asyncio.run(bridge.kids_chat(Request(payload)))
    asyncio.run(bridge.kids_chat(chat("nl")))  # the same profile continues normally


@pytest.mark.parametrize(
    "request_payload",
    [
        # Arbitrary caller text is never accepted, alone or beside selectors.
        {"session_id": SESSION, "language": "nl", "text": "Hoi"},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting"}], "text": "Hoi"},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting", "text": "Hoi"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "host", "key": "lobby", "a": "story", "b": "anything you like"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "host", "key": "lobby", "a": "story", "b": "quiz", "nickname": "Sam"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "host", "key": "start"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "notice", "key": "unknown"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "ispy_clue"}]},  # only an I Spy session has a server target
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting"}] * 3},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl", "lines": []},
        {"session_id": SESSION, "age_band": "3-5", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "chess", "language": "nl",
         "lines": [{"kind": "greeting"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "fr",
         "lines": [{"kind": "greeting"}]},
        {"session_id": "kids-bad", "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting"}]},
        {"session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
         "lines": [{"kind": "greeting"}], "voice": RUTH},
    ],
)
def test_invalid_fixed_speech_requests_are_rejected(monkeypatch: pytest.MonkeyPatch, request_payload) -> None:
    module, bridge, screened = fixed_bridge(monkeypatch)
    with pytest.raises(module.web.HTTPBadRequest):
        asyncio.run(bridge.kids_speech_approve(Request(request_payload)))
    assert screened == [] and not bridge._kids_speech_approvals and not bridge._kids_fixed_profiles


def test_ispy_clue_is_rendered_only_from_the_server_target(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge, screened = fixed_bridge(monkeypatch)
    ispy = {"age_band": "7-9", "activity": "ispy"}
    with pytest.raises(module.web.HTTPConflict):  # no validated target yet
        asyncio.run(bridge.kids_speech_approve(approve([{"kind": "ispy_clue"}], "en", **ispy)))
    bridge._kids_sessions[SESSION] = {
        "profile": ("7-9", "ispy", "en"), "history": [], "updated_at": time.monotonic(),
        "ispy_role": "reachy_picker", "ispy_target": dict(LAMP, colour="yellow"),
    }
    lines = [{"kind": "host", "key": "start", "a": "ispy"}, {"kind": "ispy_clue"}]
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve(lines, "en", **ispy))).text)
    assert payload["text"] == "Okay, I Spy! I spy with my little eye, something that is yellow."
    assert screened == [payload["text"]]


def test_flagged_or_ended_fixed_speech_gets_no_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch, flagged=True)
    with pytest.raises(module.web.HTTPForbidden):
        asyncio.run(bridge.kids_speech_approve(approve()))
    module, bridge, screened = fixed_bridge(monkeypatch)
    asyncio.run(bridge.kids_session(Request({"session_id": SESSION, "state": "ended"})))
    with pytest.raises(module.web.HTTPConflict):
        asyncio.run(bridge.kids_speech_approve(approve()))
    assert screened == [] and not bridge._kids_speech_approvals


def test_fixed_speech_held_across_end_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch)
    gate = asyncio.Event()

    async def held(_text: str, _key: str) -> bool:
        await gate.wait()
        return False

    bridge._moderation_flagged = held  # type: ignore[assignment]

    async def scenario() -> BaseException | None:
        task = asyncio.create_task(bridge.kids_speech_approve(approve()))
        await asyncio.sleep(0)
        await bridge.kids_session(Request({"session_id": SESSION, "state": "ended"}))
        gate.set()
        try:
            await task
        except BaseException as exc:  # noqa: BLE001
            return exc
        return None

    assert isinstance(asyncio.run(scenario()), module.web.HTTPConflict)
    assert not bridge._kids_speech_approvals and SESSION not in bridge._kids_fixed_profiles


def test_fixed_speech_route_is_registered() -> None:
    app = load_bridge_module().create_app(api_key="secret", hermes_url="http://127.0.0.1:8642")
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}
    assert ("POST", "/v1/kids/speech/approve") in routes


# --- Robot: Kids announcements never use generic speech ----------------------------------------


def kids_config(*, enabled: bool = True) -> AppConfig:
    return AppConfig(
        api_key="synthetic", language="nl", kids_mode_enabled=enabled, kids_session_id=SESSION,
        kids_age_band="7-9", kids_activity="story",
    )


def test_client_sends_selectors_never_text_and_speaks_the_bridge_text() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/kids/speech/approve"
        seen.append(json.loads(request.content))
        approvals = {"speech_approval": "a", "fallback_speech_approval": "b"}
        return httpx.Response(200, json={"text": NL_STORY_GREETING, **approvals})

    client = HermesBridgeClient(kids_config())
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert client.approve_kids_fixed_speech(({"kind": "greeting"},)) == NL_STORY_GREETING
        assert seen == [{
            "session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl",
            "lines": [{"kind": "greeting"}],
        }]
        assert (client._kids_speech_approval, client._kids_fallback_speech_approval) == ("a", "b")
    finally:
        client.close()
    with pytest.raises(HermesBridgeError):
        HermesBridgeClient(AppConfig(api_key="synthetic")).approve_kids_fixed_speech(({"kind": "greeting"},))


class KidsClient:
    instances: list[KidsClient] = []
    stream_fails = False

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.calls: list[str] = []
        self.last_tts_provider = "elevenlabs-flash-stream"
        KidsClient.instances.append(self)

    def approve_kids_fixed_speech(self, lines) -> str:
        self.calls.append("approve")
        self.lines = lines
        return NL_STORY_GREETING

    def iter_kids_speech(self, text: str, *, should_stop=None):
        assert text == NL_STORY_GREETING  # the bridge's exact approved text, not the robot preview
        self.calls.append("stream")
        if KidsClient.stream_fails:
            raise HermesBridgeError("synthetic stream failure")
        yield b"\x00\x01" * 2400

    def synthesize(self, text: str) -> SpeechAudio:
        # Only reachable as the Kids-mode client, whose synthesize uses /v1/kids/speech/fallback.
        assert self.config.kids_mode_enabled and text == NL_STORY_GREETING
        self.calls.append("fallback")
        return SpeechAudio(b"audio", "audio/mpeg", ".mp3", "configured")

    def close(self) -> None:
        pass


def kids_runtime(monkeypatch: pytest.MonkeyPatch, *, current: bool = True):
    stop_event = threading.Event()
    runtime = HermesVoiceRuntime(FakeRobot(), stop_event, config_loader=lambda: AppConfig(api_key="synthetic"))
    runtime._audio_ready = True
    KidsClient.instances = []
    monkeypatch.setattr("homebody.runtime.HermesBridgeClient", KidsClient)
    runtime._kids_voice_config = lambda base: kids_config(enabled=current)  # type: ignore[method-assign]
    runtime._kids_session_is_current = lambda session_id: current and session_id == SESSION  # type: ignore[method-assign]
    runtime._kids_callback_is_current = lambda generation: current and generation == 7  # type: ignore[method-assign]
    played: list[SpeechAudio] = []
    runtime._play_announcement_audio = lambda item, speech, text: played.append(speech)  # type: ignore[method-assign]
    worker = threading.Thread(target=runtime._run_announcement_worker, daemon=True)
    runtime._announcement_worker = worker
    worker.start()
    return runtime, stop_event, worker, played


def drain(runtime, stop_event, worker) -> None:
    deadline = time.monotonic() + 2
    while (runtime._announcement_queue.unfinished_tasks or runtime.status()["announcement_busy"]) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)


@pytest.mark.parametrize("stream_fails", [False, True])
def test_kids_announcement_uses_exact_approval_and_kids_voice_route(
    monkeypatch: pytest.MonkeyPatch, stream_fails: bool
) -> None:
    KidsClient.stream_fails = stream_fails
    runtime, stop_event, worker, played = kids_runtime(monkeypatch)
    runtime.queue_announcement(
        "Local preview", behavior="voice_only", kids_voice_session_id=SESSION,
        kids_speech_lines=({"kind": "greeting"},), kids_generation=7,
    )
    drain(runtime, stop_event, worker)
    KidsClient.stream_fails = False
    [client] = KidsClient.instances
    assert client.config.kids_mode_enabled and client.config.kids_session_id == SESSION
    assert client.lines == ({"kind": "greeting"},)
    assert client.calls == (["approve", "stream", "fallback"] if stream_fails else ["approve", "stream"])
    [speech] = played
    assert speech.content_type == ("audio/mpeg" if stream_fails else "audio/wav")


def test_kids_announcement_for_a_stopped_session_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, stop_event, worker, played = kids_runtime(monkeypatch, current=False)
    runtime.queue_announcement(
        "Hoi!", behavior="voice_only", kids_voice_session_id=SESSION,
        kids_speech_lines=({"kind": "greeting"},), kids_generation=7,
    )
    drain(runtime, stop_event, worker)
    assert KidsClient.instances == [] and played == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"voice": JESSICA, "provider": "elevenlabs", "kids_speech_lines": ({"kind": "greeting"},),
         "kids_generation": 1},
        {"kids_generation": 1},  # Kids voice needs catalog lines
        {"kids_speech_lines": ({"kind": "greeting"},)},  # and a generation
    ],
)
def test_kids_announcement_refuses_voice_settings_or_missing_selectors(kwargs) -> None:
    runtime = HermesVoiceRuntime(FakeRobot(), threading.Event())
    runtime._audio_ready = True
    runtime._announcement_worker = object()  # type: ignore[assignment]
    with pytest.raises(ValueError):
        runtime.queue_announcement("Hoi", kids_voice_session_id=SESSION, **kwargs)


def test_kids_runtime_no_longer_hardcodes_a_generic_voice() -> None:
    source = (Path(__file__).resolve().parents[1] / "homebody" / "kids_runtime.py").read_text()
    assert JESSICA not in source and source.count("kids_voice_session_id=") == 3
