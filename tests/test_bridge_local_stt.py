"""Request-bound local fallback must never invoke globally configured cloud transcription."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from test_bridge import load_bridge_module


class Part:
    def __init__(self, name, data, filename=None):
        self.name = name
        self.data = data
        self.filename = filename

    async def read_chunk(self, size):
        out = self.data[:size]
        self.data = self.data[size:]
        return out

    async def release(self):
        self.data = b""


class Request:
    content_type = "multipart/form-data"

    def __init__(self):
        self.parts = [
            Part("provider", b"local"),
            Part("model", b"caller-must-not-select-model"),
            Part("kids", b"1"),
            Part("language", b"nl"),
            Part("file", b"RIFFsynthetic", "audio.wav"),
        ]

    async def multipart(self):
        return self

    async def next(self):
        return self.parts.pop(0) if self.parts else None


def invoke(monkeypatch, *, local=None, available=True):
    bridge = load_bridge_module()
    server = object.__new__(bridge.Bridge)
    server.require_auth = lambda request: None
    server._require_hermes_speech = lambda: None
    monkeypatch.setattr(bridge, "_ensure_hermes_imports", lambda: None)
    calls = []
    paths = []
    tools = types.ModuleType("tools.transcription_tools")

    def configured(path):
        calls.append("CLOUD_CONFIGURED")
        paths.append(Path(path))
        return {"success": True, "transcript": "must never surface", "provider": "openai"}

    monkeypatch.setattr(tools, "transcribe_audio", configured, raising=False)
    if available:

        def local_only(path):
            calls.append("LOCAL_ONLY")
            paths.append(Path(path))
            assert "homebody-stt-nl-" in str(path)
            return local or {"success": True, "transcript": "stop", "provider": "local_command"}

        monkeypatch.setattr(tools, "transcribe_audio_local_fallback", local_only, raising=False)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", tools)
    try:
        response = asyncio.run(server.transcribe(Request()))
        return response, calls, paths
    finally:
        assert all(not p.exists() for p in paths), "temporary upload is not retained"


def test_explicit_local_ignores_configured_cloud_and_caller_model(monkeypatch):
    response, calls, _ = invoke(monkeypatch)
    assert calls == ["LOCAL_ONLY"]
    assert json.loads(response.text) == {"text": "stop", "provider": "local_command"}


def test_missing_local_only_api_fails_closed_without_configured_dispatch(monkeypatch):
    bridge = load_bridge_module()
    with pytest.raises(bridge.web.HTTPServiceUnavailable):
        invoke(monkeypatch, available=False)


@pytest.mark.parametrize("receipt", ["openai", "groq", None])
def test_local_only_helper_cannot_return_unconfirmed_or_cloud_receipt(monkeypatch, receipt):
    bridge = load_bridge_module()
    with pytest.raises(bridge.web.HTTPServiceUnavailable):
        invoke(monkeypatch, local={"success": True, "transcript": "PRIVATE_MARKER", "provider": receipt})
