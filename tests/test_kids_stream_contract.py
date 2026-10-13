"""Executable streaming contract; refactoring helper names cannot satisfy or break it."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module


@pytest.mark.parametrize("override", [None, "configured-test-voice"])
def test_kids_stream_fixed_model_format_voice_and_single_use_approval(monkeypatch, override):
    module = load_bridge_module()
    bridge = module.Bridge(api_key="bridge-fixture", hermes_url="http://127.0.0.1:9999", profile=None)
    monkeypatch.setattr(
        module,
        "_resolve_secret",
        lambda name, profile: {
            "ELEVENLABS_API_KEY": "fixture-provider-key",
            "ELEVENLABS_KIDS_VOICE_ID": override,
        }.get(name),
    )
    calls = []
    pcm = b"\x00\x00\x01\x00"

    class Content:
        def __init__(self):
            self.done = False

        async def read(self, size):
            if self.done:
                return b""
            self.done = True
            return pcm

    class Upstream:
        status = 200

        def __init__(self):
            self.content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class HTTP:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Upstream()

    bridge.http = HTTP()

    async def exercise():
        app = web.Application()
        app.router.add_post("/v1/kids/speech/stream", bridge.kids_speech_stream)
        client = TestClient(TestServer(app, host="127.0.0.1"))
        await client.start_server()
        try:
            session = "kids-" + "c" * 32
            body = {
                "input": "Approved exact fixture",
                "session_id": session,
                "voice": "caller-voice",
                "model": "caller-model",
                "format": "mp3",
            }
            headers = {"Authorization": "Bearer bridge-fixture"}
            rejected = await client.post("/v1/kids/speech/stream", json=body, headers=headers)
            assert rejected.status == 403 and not calls
            body["speech_approval"] = bridge._issue_kids_speech_approval(session, body["input"])
            response = await client.post("/v1/kids/speech/stream", json=body, headers=headers)
            assert response.status == 200
            assert await response.read() == pcm
            assert response.headers["X-Reachy-Audio-Rate"] == "24000"
            assert response.headers["X-Reachy-TTS-Model"] == "eleven_flash_v2_5"
            replay = await client.post("/v1/kids/speech/stream", json=body, headers=headers)
            assert replay.status == 403
        finally:
            await client.close()

    asyncio.run(exercise())
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == f"https://api.elevenlabs.io/v1/text-to-speech/{override or 'cgSgspJ2msm6clMCkdW9'}/stream"
    assert payload["params"] == {"output_format": "pcm_24000"}
    assert payload["json"] == {"text": "Approved exact fixture", "model_id": "eleven_flash_v2_5"}
    assert payload["headers"]["xi-api-key"] == "fixture-provider-key"
