"""GPT-Live relay against a loopback fake Live server; no OpenAI, robot or credentials."""

import asyncio
import base64
import json

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer

from companion import live_transport
from companion.live_transport import LiveTranscript, utf8_clip


def delta(role, start, end, text):
    kind = "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta"
    return {"type": kind, "delta": text, "start_ms": start, "end_ms": end}


class FakeLive:
    """Scripted provider. ``script(ws, received)`` runs after session.started."""

    def __init__(self, script, *, start=True):
        self.script, self.start = script, start
        self.received = []
        self.headers = {}
        self.path = ""
        self.done = asyncio.Event()

    def app(self):
        app = web.Application()
        app.router.add_get("/v1/live/sessions", self.handle)
        return app

    async def handle(self, request):
        self.headers, self.path = dict(request.headers), request.path_qs
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        first = await ws.receive_json()
        self.received.append(first)
        if self.start:
            await ws.send_json({"type": "session.started"})
            reader = asyncio.create_task(self.read(ws))
            try:
                await self.script(ws, self.received)
                await self.done.wait()
            finally:
                reader.cancel()
        else:
            await asyncio.sleep(5)
        return ws

    async def read(self, ws):
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                return
            payload = json.loads(message.data)
            self.received.append(payload)
            if payload.get("type") == "session.close":
                await ws.send_json({"type": "session.closed", "usage": {"input_tokens": 3, "note": "x"}})
                self.done.set()
                return


def run_relay(script, client_steps, *, answer=None, allowed=None, start=True, grace=0.3):
    async def main():
        fake = FakeLive(script, start=start)
        upstream = TestServer(fake.app())
        await upstream.start_server()
        calls = []

        async def default_answer(text, context):
            calls.append((text, context))
            return f"Done: {text}"

        async def always(_config):
            return True

        async def handler(request):
            return await live_transport.serve_live(
                request, key="sk-test-secret", answer=lambda config: answer or default_answer,
                allowed=allowed or always, upstream_url=str(upstream.make_url("/v1/live/sessions")).replace(
                    "http", "ws", 1), started_timeout=1.0, transcript_grace=grace,
            )

        app = web.Application()
        app.router.add_get("/v1/live", handler)
        bridge = TestServer(app)
        await bridge.start_server()
        events = []
        try:
            async with ClientSession() as http:
                async with http.ws_connect(bridge.make_url("/v1/live")) as ws:
                    await ws.send_json({"type": "session.start", "model": "gpt-live-1", "voice": "marin",
                                        "timeout_seconds": 60, "assistant_mode": "everyday"})
                    await client_steps(ws, events)
        finally:
            await bridge.close()
            await upstream.close()
        return fake, events, calls

    return asyncio.run(main())


async def collect(ws, events, until, timeout=3.0):
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.receive()
            if message.type != WSMsgType.TEXT:
                return
            payload = json.loads(message.data)
            events.append(payload)
            if until(payload):
                return


def test_transcript_corrections_ordering_and_context():
    transcript = LiveTranscript()
    transcript.observe(delta("user", 100, 300, "turn on the"))
    transcript.observe(delta("user", 600, 800, " light"))  # delivered before the middle fragment
    transcript.observe(delta("user", 300, 600, " hall"))
    transcript.observe(delta("user", 300, 600, " desk"))  # correction of the same span replaces it
    transcript.observe(delta("user", 300, 600, " desk"))  # duplicate delivery is idempotent
    assert transcript.request(800)[0] == "turn on the desk light"
    transcript.consume(800)
    transcript.observe(delta("assistant", 900, 1000, "Done."))
    transcript.observe(delta("user", 1100, 1300, "and off"))
    transcript.observe(delta("user", 1100, 1500, "and the kettle off"))  # revision spanning the earlier one
    current, context = transcript.request(1500)
    assert current == "and the kettle off"
    assert "user: turn on the desk light" in context
    assert transcript.request(800)[0] == ""  # consumed text is not re-delegated


def test_utf8_clip_never_breaks_characters():
    clipped = utf8_clip("é" * 400)
    assert len(clipped.encode()) <= 480 and clipped == "é" * 240
    assert utf8_clip("ok") == "ok"


def test_start_shape_waits_for_started_and_answers_with_exact_delegation():
    async def script(ws, received):
        await ws.send_json(delta("user", 0, 400, "turn on the"))
        await ws.send_json({"type": "session.delegation.created", "offset_ms": 900,
                            "delegation": {"id": "del-1", "type": "delegation", "target": "client"}})
        await asyncio.sleep(0.05)
        await ws.send_json(delta("user", 400, 900, " desk light"))  # late delta inside the grace window
        await ws.send_json({"type": "session.debug", "secret": "sk-leak"})
        await ws.send_json({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01\x02\x03").decode()})
        await ws.send_json({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x04").decode()})

    async def steps(ws, events):
        await collect(ws, events, lambda p: p["type"] == "session.started")
        await ws.send_json({"type": "session.input_audio.append", "audio": base64.b64encode(b"\x00\x01").decode()})
        await ws.send_json({"type": "session.input_audio.append", "audio": base64.b64encode(b"\x00").decode()})
        await collect(ws, events, lambda p: p.get("state") == "answered")
        await asyncio.sleep(0.1)
        await ws.send_json({"type": "session.close"})
        await collect(ws, events, lambda p: p["type"] == "session.closed")

    fake, events, calls = run_relay(script, steps)
    start = fake.received[0]
    assert fake.path == "/v1/live/sessions" and fake.headers["Authorization"] == "Bearer sk-test-secret"
    assert start["type"] == "session.start"
    session = start["session"]
    assert session["model"] == "gpt-live-1" and session["store"] is False
    assert session["audio"] == {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "marin"}}
    assert session["delegation"] == {"type": "client"}
    assert calls == [("turn on the desk light", "")]
    commentary = [e for e in fake.received if e["type"] == "session.commentary.append"]
    assert commentary[0]["delegation_id"] == "del-1" and commentary[0]["content"] == "Done: turn on the desk light"
    audio_in = [e for e in fake.received if e["type"] == "session.input_audio.append"]
    assert [base64.b64decode(e["audio"]) for e in audio_in] == [b"\x00\x01"]  # odd chunk dropped
    out = b"".join(base64.b64decode(e["delta"]) for e in events if e["type"] == "session.output_audio.delta")
    assert out == b"\x01\x02\x03\x04"  # odd trailing byte carried, samples never shifted
    assert all(len(base64.b64decode(e["delta"])) % 2 == 0 for e in events if e["type"] == "session.output_audio.delta")
    assert fake.received[-1]["type"] == "session.close"
    assert not any(e["type"] == "session.debug" for e in events)
    assert "sk-" not in json.dumps(events)


def test_incomplete_request_asks_for_clarification_and_budget_refusal_keeps_session(monkeypatch):
    monkeypatch.setattr(live_transport, "MAX_DELEGATIONS", 1)

    async def script(ws, received):
        await ws.send_json({"type": "session.delegation.created", "offset_ms": 500,
                            "delegation": {"id": "d1", "target": "client"}})
        await asyncio.sleep(0.5)
        for identifier in ("d1", "d2"):  # duplicate id is ignored; d2 exceeds the budget
            await ws.send_json({"type": "session.delegation.created", "offset_ms": 900,
                                "delegation": {"id": identifier, "target": "client"}})
        await asyncio.sleep(0.1)
        await ws.send_json(delta("assistant", 1000, 1200, "still here"))

    async def steps(ws, events):
        await collect(ws, events, lambda p: p.get("type") == "session.output_transcript.delta")
        await ws.send_json({"type": "session.close"})
        await collect(ws, events, lambda p: p["type"] == "session.closed")

    fake, events, calls = run_relay(script, steps, grace=0.2)
    commentary = [e for e in fake.received if e["type"] == "session.commentary.append"]
    assert calls == []
    assert [c["delegation_id"] for c in commentary] == ["d1", "d2"]
    assert "repeat" in commentary[0]["content"] and "limit" in commentary[1]["content"]
    assert any(e.get("state") == "clarify" for e in events)


def test_stop_during_delegation_discards_late_result():
    release = asyncio.Event()

    async def slow_answer(text, context):
        await release.wait()
        return "too late"

    async def script(ws, received):
        await ws.send_json(delta("user", 0, 500, "what is the weather"))
        await ws.send_json({"type": "session.delegation.created", "offset_ms": 500,
                            "delegation": {"id": "d1", "target": "client"}})

    async def steps(ws, events):
        await collect(ws, events, lambda p: p.get("state") == "started")
        await ws.send_json({"type": "session.close"})
        await collect(ws, events, lambda p: p["type"] == "session.closed")
        release.set()

    fake, events, _ = run_relay(script, steps, answer=slow_answer)
    assert not [e for e in fake.received if e["type"] == "session.commentary.append"]
    assert fake.received[-1]["type"] == "session.close"


def test_boundary_loss_ends_session_and_unstarted_session_fails_without_secrets():
    state = {"allowed": True}

    async def allowed(_config):
        return state["allowed"]

    async def script(ws, received):
        state["allowed"] = False  # e.g. Kids Mode latched on the host

    async def steps(ws, events):
        await collect(ws, events, lambda p: False)

    fake, events, _ = run_relay(script, steps, allowed=allowed)
    assert fake.received[-1]["type"] == "session.close"

    async def noop(ws, received):
        return None

    _fake, events, _ = run_relay(noop, steps, start=False)
    assert events[-1]["type"] == "bridge.error" and "sk-" not in json.dumps(events)
