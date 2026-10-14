"""Dutch-only voice routing with synthetic approvals/transport; no robot media."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from test_kids_ack_review_regressions import bare_bridge

from homebody import kids_acknowledgements as acks
from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient

RUTH = "yO6w2xlECAQRFP6pX7Hw"
JESSICA = "cgSgspJ2msm6clMCkdW9"
SESSION = "kids-" + "a" * 32


def configured_bridge(monkeypatch, tmp_path, nl=RUTH):
    module, bridge = bare_bridge(monkeypatch, tmp_path, {"id": JESSICA})
    values = {
        "ELEVENLABS_KIDS_VOICE_ID": JESSICA,
        "ELEVENLABS_KIDS_NL_VOICE_ID": nl,
        "ELEVENLABS_API_KEY": "synthetic-key",
    }
    monkeypatch.setattr(module, "_resolve_secret", lambda key, _profile: values.get(key, ""))
    bridge._kids_speech_approvals = {}
    bridge._kids_sessions = {}
    bridge.require_auth = lambda _request: None
    return module, bridge


def test_dutch_override_does_not_change_english_or_legacy_default(monkeypatch, tmp_path):
    module, bridge = configured_bridge(monkeypatch, tmp_path)
    assert module._kids_stream_voice(None, "nl") == RUTH
    assert module._kids_stream_voice(None, "en") == JESSICA
    assert module._kids_stream_voice(None) == JESSICA
    health = json.loads(asyncio.run(bridge.health(None)).text)
    assert "kids_voice_sha256" not in health  # legacy proof cannot authorise the wrong NL filler
    assert health["kids_voice_proofs"]["nl"]["kids_voice_sha256"] == hashlib.sha256(RUTH.encode()).hexdigest()
    assert health["kids_voice_proofs"]["en"]["kids_voice_sha256"] == hashlib.sha256(JESSICA.encode()).hexdigest()
    assert RUTH not in json.dumps(health) and JESSICA not in json.dumps(health)


@pytest.mark.parametrize("language,voice", [("nl", RUTH), ("en", JESSICA)])
def test_stream_uses_approval_language_not_caller_override(monkeypatch, tmp_path, language, voice):
    _module, bridge = configured_bridge(monkeypatch, tmp_path)
    bridge._kids_sessions[SESSION] = {"profile": ("4-6", "story", language)}
    token = bridge._issue_kids_speech_approval(SESSION, "fixed synthetic text")
    urls = []

    class Seen(Exception):
        pass

    class HTTP:
        def post(self, url, **_kwargs):
            urls.append(url)
            raise Seen()

    bridge.http = HTTP()

    class Request:
        async def json(self):
            return {
                "input": "fixed synthetic text",
                "session_id": SESSION,
                "speech_approval": token,
                "language": "en" if language == "nl" else "nl",
                "voice": "caller_override",
            }

    with pytest.raises(Seen):
        asyncio.run(bridge.kids_speech_stream(Request()))
    assert urls == [f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"]
    assert token not in bridge._kids_speech_approvals
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(bridge.kids_speech_stream(Request()))
    assert len(urls) == 1


@pytest.mark.parametrize("language", ["en", "nl"])
def test_fresh_proof_endpoint_proves_requested_language(monkeypatch, tmp_path, language):
    module, bridge = configured_bridge(monkeypatch, tmp_path)
    response = asyncio.run(bridge.kids_voice_proof(SimpleNamespace(query={"language": language})))
    assert json.loads(response.text) == module._kids_voice_proof(None, language)
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(asyncio.run(bridge.kids_voice_proof(SimpleNamespace(query={}))).text) == {}


@pytest.mark.parametrize("query", [{"language": "fr"}, {"language": "nl", "voice": RUTH}])
def test_invalid_proof_request_is_rejected(monkeypatch, tmp_path, query):
    _module, bridge = configured_bridge(monkeypatch, tmp_path)
    with pytest.raises(web.HTTPBadRequest):
        asyncio.run(bridge.kids_voice_proof(SimpleNamespace(query=query)))


def test_client_fresh_proof_carries_immutable_kids_language():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"kids_voice_sha256": "a" * 64, "kids_tts_model": "eleven_flash_v2_5"})

    client = HermesBridgeClient(AppConfig(api_key="synthetic", language="nl"))
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert client.kids_voice_proof()["kids_voice_sha256"] == "a" * 64
        assert seen == [{"language": "nl"}]
    finally:
        client.close()


def test_first_turn_language_proof_does_not_fall_back_to_legacy_on_missing_map_entry():
    from homebody.voice_pipeline import kids_ack_voice_proof

    legacy = {"kids_voice_sha256": "a" * 64, "kids_tts_model": "eleven_flash_v2_5"}
    nl = {**legacy, "kids_voice_sha256": "b" * 64}
    assert kids_ack_voice_proof({**legacy, "kids_voice_proofs": {"nl": nl}}, "nl") == ("b" * 64, "eleven_flash_v2_5")
    assert kids_ack_voice_proof({**legacy, "kids_voice_proofs": {"en": legacy}}, "nl") == (None, None)
    assert kids_ack_voice_proof(legacy, "nl") == ("a" * 64, "eleven_flash_v2_5")
    assert kids_ack_voice_proof({**legacy, "kids_voice_proofs": []}, "nl") == (None, None)


def test_bundled_ruth_catalog_matches_only_ruth_dutch(monkeypatch):
    monkeypatch.setattr(acks, "_catalog", None)
    catalog = acks.catalog()
    rh, jh = (hashlib.sha256(voice.encode()).hexdigest() for voice in (RUTH, JESSICA))
    assert len(acks.eligible_clips(catalog["nl"], rh, "eleven_flash_v2_5")) == 3
    assert len(acks.eligible_clips(catalog["en"], rh, "eleven_flash_v2_5")) == 0
    assert len(acks.eligible_clips(catalog["en"], jh, "eleven_flash_v2_5")) == 3
    assert len(acks.eligible_clips(catalog["nl"], jh, "eleven_flash_v2_5")) == 3
    assert not acks.eligible_clips(catalog["nl"], rh, "eleven_multilingual_v2")


@pytest.mark.parametrize("language,prefix", [("nl", "nl-ruth-"), ("en", "en-")])
def test_real_pipeline_first_turn_selects_language_and_voice_matched_bundle(monkeypatch, tmp_path, language, prefix):
    from test_kids_stt_acknowledgements import Harness

    from homebody import voice_pipeline

    monkeypatch.setattr(acks, "_catalog", None)
    packaged = acks.catalog()
    module, bridge = configured_bridge(monkeypatch, tmp_path)
    health = json.loads(asyncio.run(bridge.health(None)).text)
    harness = Harness(monkeypatch, tmp_path / "h", language=language, stt_seconds=0.5)
    monkeypatch.setattr(voice_pipeline, "kids_ack_catalog", lambda: packaged)
    harness.health_proof = {"kids_voice_proofs": health["kids_voice_proofs"]}
    harness.run()
    assert harness.media.pushes()
    assert harness.runtime._kids_ack_selector._last_name.startswith(prefix)
    assert "kids_voice_proofs" not in json.dumps(harness.runtime.status())


def test_invalid_dutch_override_fails_closed_without_affecting_english(monkeypatch, tmp_path):
    module, bridge = configured_bridge(monkeypatch, tmp_path, nl="bad voice/../id")
    assert module._kids_voice_proof(None, "nl") == {}
    assert module._kids_stream_voice(None, "en") == JESSICA
    assert json.loads(asyncio.run(bridge.health(None)).text)["kids_voice_proofs"]["nl"] == {}
