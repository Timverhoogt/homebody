"""Regressions for the 0.5.31 Live start failure and Kids fixed-speech 403 incident.

Loopback fake Live provider and fake moderation only; no OpenAI, robot, credentials or network.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import replace

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer
from test_ispy_target_variety import SESSION, Request
from test_kids_story_ispy_speech import fixed_bridge

from companion import kids_fixed_copy as catalog
from companion import live_transport
from homebody.config import AppConfig
from homebody.kids_game_master import host_line
from homebody.kids_mode import KidsProfile, kids_greeting
from homebody.live_client import LiveBridgeSession
from homebody.realtime_client import RealtimeBridgeError, RealtimeEvent

SECRET_MESSAGE = "Incorrect API key provided: sk-live-SECRET1234. Prompt: private plan for Marieke"


def run_start(provider, *, handshake_status: bool = False):
    """Start one relay against ``provider(ws)``; return the robot-facing events."""

    async def main():
        async def handle(request):
            if handshake_status:
                raise web.HTTPUnauthorized(text=SECRET_MESSAGE)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive_json()  # session.start
            await provider(ws)
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/v1/live/sessions", handle)
        upstream = TestServer(upstream_app)
        await upstream.start_server()

        async def always(_config):
            return True

        async def answer(_text, _context):
            return "unused"

        async def handler(request):
            return await live_transport.serve_live(
                request, key="sk-live-SECRET1234", answer=lambda _config: answer, allowed=always,
                upstream_url=str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1),
                started_timeout=1.0,
            )

        app = web.Application()
        app.router.add_get("/v1/live", handler)
        bridge = TestServer(app)
        await bridge.start_server()
        events = []
        try:
            async with ClientSession() as http, http.ws_connect(bridge.make_url("/v1/live")) as ws:
                await ws.send_json({"type": "session.start", "model": "gpt-live-1", "voice": "marin",
                                    "timeout_seconds": 60, "assistant_mode": "everyday"})
                async with asyncio.timeout(3):
                    async for message in ws:
                        if message.type != WSMsgType.TEXT:
                            break
                        events.append(json.loads(message.data))
        finally:
            await bridge.close()
            await upstream.close()
        return events

    return asyncio.run(main())


def _assert_safe(caplog, events):
    dumped = caplog.text + json.dumps(events)
    assert "sk-" not in dumped and "Marieke" not in dumped and "Incorrect API key" not in dumped


def test_provider_error_before_started_is_categorized_with_its_code_but_no_message(caplog):
    async def provider(ws):
        await ws.send_json({"type": "error", "error": {
            "type": "invalid_request_error", "code": "invalid_api_key", "message": SECRET_MESSAGE}})
        await asyncio.sleep(0.2)

    caplog.set_level(logging.INFO)
    events = run_start(provider)
    errors = [e for e in events if e["type"] == "bridge.error"]
    assert len(errors) == 1 and not any(e["type"] == "session.started" for e in events)
    assert errors[0]["category"] == "provider_error" and errors[0]["detail"] == "invalid_api_key"
    assert "Live relay ended (LiveStartRejected category=provider_error detail=invalid_api_key)" in caplog.text
    _assert_safe(caplog, events)


def test_provider_close_before_started_reports_the_close_code(caplog):
    async def provider(ws):
        await ws.close(code=4003, message=SECRET_MESSAGE.encode()[:100])

    caplog.set_level(logging.INFO)
    events = run_start(provider)
    assert [(e["category"], e["detail"]) for e in events if e["type"] == "bridge.error"] == [
        ("provider_closed", "4003")
    ]
    assert "category=provider_closed detail=4003" in caplog.text
    _assert_safe(caplog, events)


def test_unexpected_first_event_names_only_a_sanitized_event_type(caplog):
    async def provider(ws):
        await ws.send_json({"type": "session.created", "secret": SECRET_MESSAGE})
        await ws.send_json({"type": "session.started"})  # too late: start shape stays strict
        await asyncio.sleep(0.2)

    caplog.set_level(logging.INFO)
    events = run_start(provider)
    assert [(e["category"], e["detail"]) for e in events if e["type"] == "bridge.error"] == [
        ("unexpected_event", "session.created")
    ]

    async def hostile(ws):
        await ws.send_json({"type": SECRET_MESSAGE})
        await asyncio.sleep(0.2)

    caplog.clear()
    events = run_start(hostile)
    assert [(e["category"], e["detail"]) for e in events if e["type"] == "bridge.error"] == [
        ("unexpected_event", "unknown")
    ]
    _assert_safe(caplog, events)


def test_handshake_rejection_reports_http_status_not_body(caplog):
    async def provider(_ws):
        raise AssertionError("not reached")

    caplog.set_level(logging.INFO)
    events = run_start(provider, handshake_status=True)
    assert [(e["category"], e["detail"]) for e in events if e["type"] == "bridge.error"] == [
        ("provider_handshake", "401")
    ]
    _assert_safe(caplog, events)


def _session_with(payload):
    session = LiveBridgeSession(AppConfig(api_key="k", bridge_url="https://h.example"))
    session._events.put(RealtimeEvent(payload["type"], payload))
    return session


def test_robot_start_error_keeps_fixed_text_and_appends_only_valid_categories():
    with pytest.raises(RealtimeBridgeError) as caught:
        _session_with({"type": "bridge.error", "error": "Live provider did not accept the session",
                       "category": "provider_error", "detail": "insufficient_quota"}).wait_started(timeout=0.5)
    assert str(caught.value) == (
        "Live provider did not accept the session (category=provider_error, detail=insufficient_quota)"
    )
    with pytest.raises(RealtimeBridgeError) as caught:
        _session_with({"type": "bridge.error", "error": "Live transport unavailable or session invalid",
                       "category": "Bad Category!", "detail": "sk-x y"}).wait_started(timeout=0.5)
    assert str(caught.value) == "Live transport unavailable or session invalid"
    with pytest.raises(RealtimeBridgeError) as caught:  # older bridges: unchanged text
        _session_with({"type": "bridge.error", "error": "Live is blocked in the current state"}).wait_started(
            timeout=0.5
        )
    assert str(caught.value) == "Live is blocked in the current state"
    with pytest.raises(RealtimeBridgeError) as caught:  # a normal close stays distinct from bridge.error
        _session_with({"type": "session.closed"}).wait_started(timeout=0.5)
    assert str(caught.value) == "Live session was not accepted"


@pytest.mark.parametrize("private", ["sk-proj-private-token-123", "child-private-name", "session-private123"])
@pytest.mark.parametrize("field", ["code", "event"])
def test_private_protocol_shaped_values_never_reach_logs_or_robot(private, field, caplog):
    async def provider(ws):
        payload = ({"type": "error", "error": {"code": private}} if field == "code" else {"type": private})
        await ws.send_json(payload)
        await asyncio.sleep(0.05)

    caplog.set_level(logging.INFO)
    events = run_start(provider)
    assert private not in caplog.text + json.dumps(events)
    assert next(e for e in events if e["type"] == "bridge.error")["detail"] == "unknown"
    from homebody.live_client import bridge_error_text
    assert private not in bridge_error_text({"category": "provider_error", "detail": private}, "Failed")
    assert private not in bridge_error_text({"category": private, "detail": "401"}, "Failed")


def test_diagnostic_vocabularies_match_and_protocol_numbers_are_bounded():
    from homebody import live_client
    assert live_client._SAFE_CODES == live_transport._SAFE_CODES
    for module in (live_client, live_transport):
        for value in (True, False, 999999, "１２３", "12345", [], {}, "private"):
            assert module._safe_detail(value) == "unknown"
        for value in (401, 429, 4003, "insufficient_quota"):
            assert module._safe_detail(value) == str(value)


# ---- Kids fixed copy -----------------------------------------------------------------------------


def _host_selectors():
    for key, slots in catalog.HOST_SLOTS.items():
        for values in itertools.product(catalog.ACTIVITIES, repeat=len(slots)):
            yield key, dict(zip(slots, values, strict=True))


@pytest.mark.parametrize("language", catalog.LANGUAGES)
def test_robot_preview_copy_matches_every_bridge_rendered_fixed_line(language):
    """The robot's queued text and the bridge's approved text must be the same line."""
    for key, slots in _host_selectors():
        rendered = catalog.render_segment({"kind": "host", "key": key, **slots}, activity="buddy", language=language)
        assert host_line(language, key, **slots) == rendered, (language, key, slots)
    for activity in catalog.ACTIVITIES:
        profile = KidsProfile(language=language, activity=activity, camera_consent=activity == "ispy",
                              nickname="Ana")
        greeting = catalog.render_segment({"kind": "greeting"}, activity=activity, language=language)
        assert kids_greeting(replace(profile, nickname="")) == greeting
        # Hosted game start: robot "start" intro + greeting equals the bridge's two-segment join.
        start = catalog.render_segment({"kind": "host", "key": "start", "a": activity}, activity=activity,
                                       language=language)
        assert f"{host_line(language, 'start', a=activity)} {kids_greeting(replace(profile, nickname=''))}" == (
            " ".join([start, greeting])
        )


def test_flagged_fixed_line_stays_refused_and_logs_only_catalog_selectors(monkeypatch, caplog):
    module, bridge, screened = fixed_bridge(monkeypatch, flagged=True)
    lines = [{"kind": "host", "key": "offer_retry", "a": "story", "b": "riddles"}]
    caplog.set_level(logging.INFO)
    with pytest.raises(module.web.HTTPForbidden):
        asyncio.run(bridge.kids_speech_approve(Request({
            "session_id": SESSION, "age_band": "7-9", "activity": "story", "language": "nl", "lines": lines})))
    text = catalog.render_segment(lines[0], activity="story", language="nl")
    assert screened == [text]
    assert not getattr(bridge, "_kids_speech_approvals", {})  # no approval of any kind was issued
    assert SESSION not in getattr(bridge, "_kids_fixed_profiles", {})
    assert (
        "Kids fixed speech was refused by moderation (language=nl activity=story "
        "selectors=host:offer_retry[a=story,b=riddles])"
    ) in caplog.text
    assert text not in caplog.text and SESSION not in caplog.text
