"""Real HTTPX submission-boundary checks; no sockets, media, or provider calls."""

from __future__ import annotations

import httpx
import pytest

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient
from homebody.local_vision import LocalVisionClient


@pytest.mark.parametrize("operation", ["image", "analysis", "local_image"])
def test_revocation_during_actual_request_preparation_never_reaches_transport(monkeypatch, operation):
    allowed, sent = [True], []

    def transport(request):
        sent.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "text": "Monday",
                "tool_free": True,
                "choices": [{"message": {"content": "Monday"}}],
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(transport))
    if operation == "local_image":
        client = LocalVisionClient("http://vision.invalid/v1", "fixture", transport=httpx.MockTransport(transport))
        http.close()
        http = client._client
    else:
        client = HermesBridgeClient(AppConfig(bridge_url="http://bridge.invalid", api_key="fixture"), client=http)
    build = http.build_request

    def prepared_then_revoked(*args, **kwargs):
        request = build(*args, **kwargs)
        allowed[0] = False
        return request

    monkeypatch.setattr(http, "build_request", prepared_then_revoked)
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            if operation == "image":
                client.look(b"fixture-jpeg", "Read this", is_current=lambda: allowed[0])
            elif operation == "analysis":
                client.look_follow_up("A letter", "What day?", is_current=lambda: allowed[0])
            else:
                client.describe(b"fixture-jpeg", "Read this", is_current=lambda: allowed[0])
        assert sent == []
    finally:
        client.close()
        http.close()


def test_current_request_guard_is_process_local_and_preserves_success():
    sent = []

    def transport(request):
        assert "homebody_request_current" not in request.extensions
        assert "homebody_request_current" not in request.content.decode()
        sent.append(request.url.path)
        return httpx.Response(200, json={"text": "Monday", "model": "fixture", "processing": "fixture"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = HermesBridgeClient(AppConfig(bridge_url="http://bridge.invalid", api_key="fixture"), client=http)
        assert client.look(b"fixture-jpeg", "Read this", is_current=lambda: True)["text"] == "Monday"
        assert sent == ["/v1/everyday/look"]


def test_companion_honors_an_active_temporary_camera_grant_and_its_revocation():
    import time

    from test_companion import make_runtime

    runtime, client, captures = make_runtime(camera_enabled=False, everyday_camera_enabled=True)
    runtime._everyday_camera_until = time.monotonic() + 60
    try:
        assert runtime.companion_public_status(runtime.config_loader())["camera"]["permitted"] is True
        assert runtime.look_at_this("Read this letter")["ok"] is True
        assert captures == [1] and len(client.looks) == 1
        runtime.revoke_everyday_camera()
        assert runtime._look_context is None
        with pytest.raises(RuntimeError):
            runtime.follow_up_look("What day?")
        assert client.follow_ups == []
        assert runtime._look_context is None
        assert runtime.config_loader().camera_enabled is False
    finally:
        runtime._companion_teardown("test_cleanup")
