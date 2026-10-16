"""Host bridge: one-frame look route, presence heartbeats/unavailability, enrolled source producers.

All HTTP is a fake; no Home Assistant, provider, robot or network is contacted.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from test_bridge import load_bridge_module


def jpeg() -> bytes:
    output = io.BytesIO()
    Image.fromarray(np.full((32, 32, 3), 120, dtype=np.uint8), "RGB").save(output, format="JPEG")
    return output.getvalue()


class Response:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self._body

    async def read(self) -> bytes:
        return b""

    async def text(self) -> str:
        return json.dumps(self._body)


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.gets: list[str] = []
        self.states: dict[str, object] = {}
        self.calendars: dict[str, object] = {}
        self.vision_answer: object = {
            "choices": [{"message": {"content": "A dentist letter for Friday."}}],
            "model": "fake-vision",
        }
        self.vision_status = 200
        self.fail_get = False
        self.source_revision = "f" * 32

    def post(self, url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
        self.posts.append((url, json))
        if "chat/completions" in url:
            return Response(self.vision_status, self.vision_answer)
        return Response(200, {"ok": True, "queued": True, "reason": "committed"})

    def get(self, url: str, **_kwargs: object) -> Response:
        self.gets.append(url)
        if self.fail_get:
            raise RuntimeError("connection refused")
        if url.endswith("/api/initiative/source-state"):
            return Response(200, {"source_revision": self.source_revision})
        for key, value in self.states.items():
            if url.endswith(f"/api/states/{key}"):
                return Response(200, value)
        for key, value in self.calendars.items():
            if f"/api/calendars/{key}" in url:
                return Response(200, value)
        return Response(404, {})


class FakeRequest:
    headers = {"Authorization": "Bearer bridge-secret"}

    def __init__(self, payload: object) -> None:
        self.payload = payload

    async def json(self) -> object:
        return self.payload


@pytest.fixture
def bridge(monkeypatch):
    module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("REACHY_LOOK_VISION_URL", raising=False)
    monkeypatch.setattr(
        module,
        "_resolve_secret",
        lambda name, _profile: {
            "HASS_URL": "http://ha.local:8123",
            "HASS_TOKEN": "hass-secret",
            "OPENAI_API_KEY": "test-openai-key",
        }.get(name, ""),
    )
    instance = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:8642", profile=None)
    instance.http = FakeHttp()
    return module, instance


def test_look_route_sends_one_frame_once_and_returns_bounded_text(bridge) -> None:
    module, instance = bridge
    frame = base64.b64encode(jpeg()).decode("ascii")
    request = FakeRequest({"question": "What does this say?", "frame_jpeg": frame})
    response = asyncio.run(instance.everyday_look(request))
    body = json.loads(response.text)
    assert body["text"] == "A dentist letter for Friday." and body["processing"] == "provider:openai"
    assert len(instance.http.posts) == 1
    sent = instance.http.posts[0][1]
    assert sent["messages"][1]["content"][0]["text"] == "What does this say?"
    assert sent["messages"][1]["content"][1]["image_url"]["detail"] == "low"
    assert sum(1 for part in sent["messages"][1]["content"] if part["type"] == "image_url") == 1
    assert "store" in sent and sent["store"] is False

    with pytest.raises(module.web.HTTPForbidden):
        asyncio.run(instance.everyday_look(FakeRequest({"question": "x", "frame_jpeg": frame, "history": []})))
    not_a_jpeg = base64.b64encode(b"nope").decode()
    with pytest.raises(module.web.HTTPBadRequest):
        asyncio.run(instance.everyday_look(FakeRequest({"question": "x", "frame_jpeg": not_a_jpeg})))
    with pytest.raises(module.web.HTTPUnauthorized):
        bad = FakeRequest({"question": "x", "frame_jpeg": frame})
        bad.headers = {"Authorization": "Bearer wrong"}
        asyncio.run(instance.everyday_look(bad))
    instance.http.vision_status = 500
    with pytest.raises(module.web.HTTPBadGateway):
        asyncio.run(instance.everyday_look(FakeRequest({"question": "x", "frame_jpeg": frame})))
    instance.http.vision_status = 200
    instance.http.vision_answer = {"choices": [{"message": {"content": ""}}]}
    with pytest.raises(module.web.HTTPBadGateway):
        asyncio.run(instance.everyday_look(FakeRequest({"question": "x", "frame_jpeg": frame})))


def test_presence_forwarder_heartbeats_and_reports_source_loss_without_false_arrival(bridge, monkeypatch) -> None:
    module, instance = bridge
    instance._presence_entity_id = "binary_sensor.office"
    instance._presence_url = "https://reachy.local/api/presence/signal"
    instance._presence_heartbeat_seconds = 5.0
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    instance.http.states["binary_sensor.office"] = {"state": "on"}

    async def scenario() -> None:
        assert await instance._forward_presence_once() is True  # transition
        assert await instance._forward_presence_once() is True  # unchanged, recently sent: no POST
        clock[0] += 6
        assert await instance._forward_presence_once() is True  # heartbeat
        instance.http.fail_get = True
        with pytest.raises(RuntimeError):
            await instance._forward_presence_once()
        await instance._report_presence_unavailable("connection refused")
        await instance._report_presence_unavailable("connection refused")  # once per streak
        instance.http.fail_get = False
        assert await instance._forward_presence_once() is True  # recovery re-sends the state

    asyncio.run(scenario())
    bodies = [body for url, body in instance.http.posts if url.endswith("/presence/signal")]
    assert bodies[0] == {"source": "home_assistant", "occupied": True, "attentive": False, "confidence": 1.0}
    assert bodies[1]["heartbeat"] is True and bodies[1]["occupied"] is True
    assert bodies[2]["unavailable"] is True and "refused" in bodies[2]["unavailable_reason"]
    assert bodies[3]["occupied"] is True and "heartbeat" not in bodies[3]
    assert len(bodies) == 4


def test_sources_disabled_until_enrolled_then_one_offer_per_moment(bridge, tmp_path: Path) -> None:
    _module, instance = bridge
    instance._offer_url = "https://reachy.local/api/initiative/offers"
    instance._sources_path = ""
    assert asyncio.run(instance._poll_sources_once()) == []
    assert instance.http.gets == [] and instance.http.posts == []

    enrollment = tmp_path / "sources.json"
    enrollment.write_text(
        json.dumps(
            {
                "source_revision": "f" * 32,
                "home_assistant": [
                    {
                        "id": "washer",
                        "label": "Washing machine",
                        "entity_id": "binary_sensor.washer_done",
                        "condition": "state_equals",
                        "value": "on",
                        "offer_text": "The washing machine seems finished; would you like to know when it stopped?",
                        "accepted_text": "Okay. The washer reported done just now; nothing else was changed.",
                        "min_interval_seconds": 1800,
                    }
                ],
                "calendar": [
                    {"id": "family", "label": "Family", "calendar_entity_id": "calendar.family", "lead_minutes": 10}
                ],
            }
        ),
        encoding="utf-8",
    )
    instance._sources_path = str(enrollment)
    instance.http.states["binary_sensor.washer_done"] = {"state": "on", "last_changed": "2026-10-16T14:00:00+02:00"}
    instance.http.calendars["calendar.family"] = []
    delivered = asyncio.run(instance._poll_sources_once())
    assert len(delivered) == 1
    offers = [body for url, body in instance.http.posts if url.endswith("/initiative/offers")]
    assert offers[0]["source"] == "home_assistant" and offers[0]["private_detail"] == ""
    assert "washing machine" in offers[0]["text"].lower()
    assert "remind" not in offers[0]["accepted_text"]  # read-only help, truthfully worded
    # Same moment again (delivered): silent. New change: one more offer.
    assert asyncio.run(instance._poll_sources_once()) == []
    instance.http.states["binary_sensor.washer_done"] = {"state": "on", "last_changed": "2026-10-16T18:00:00+02:00"}
    assert len(asyncio.run(instance._poll_sources_once())) == 1
    # Only the exact enrolled entity and calendar are read; nothing else on the host is touched.
    assert all(
        "washer_done" in url or "calendar.family" in url or url.endswith("/source-state") for url in instance.http.gets
    )


def test_source_removal_serializes_with_inflight_delivery_and_needs_no_ha_setup(bridge, tmp_path, monkeypatch):
    module, instance = bridge
    enrollment = tmp_path / "sources.json"
    document = {
        "source_revision": "f" * 32,
        "home_assistant": [
            {
                "id": "washer",
                "label": "Washer",
                "entity_id": "binary_sensor.washer_done",
                "condition": "state_equals",
                "value": "on",
                "offer_text": "The washer is finished; would you like its status?",
                "accepted_text": "The washer reports done. Nothing was changed.",
                "min_interval_seconds": 1800,
            }
        ],
        "calendar": [],
    }
    enrollment.write_text(json.dumps(document))
    instance._sources_path = str(enrollment)
    instance._offer_url = "https://reachy.local/api/initiative/offers"
    instance.http.states["binary_sensor.washer_done"] = {
        "state": "on",
        "last_changed": "2026-10-16T14:00:00+02:00",
    }

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        original_post = instance.http.post

        class HeldResponse(Response):
            async def __aenter__(self):
                started.set()
                await release.wait()
                return self

        def held_post(url, **kwargs):
            original_post(url, **kwargs)
            assert kwargs["timeout"].total == 5
            return HeldResponse(200, {"ok": True, "queued": True})

        instance.http.post = held_post
        poll = asyncio.create_task(instance._poll_sources_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        # Removal remains permitted after credentials or catalog setup disappear.
        monkeypatch.setattr(module, "_resolve_secret", lambda *_args: "")
        remove = asyncio.create_task(
            instance.companion_sources_enroll(
                FakeRequest({"home_assistant": [], "calendar": [], "source_revision": "e" * 32})
            )
        )
        await asyncio.sleep(0)
        assert not remove.done()
        assert json.loads(enrollment.read_text())["home_assistant"]
        release.set()
        delivered, response = await asyncio.wait_for(asyncio.gather(poll, remove), timeout=2)
        assert delivered and response.status == 200
        assert json.loads(enrollment.read_text()) == {
            "home_assistant": [],
            "calendar": [],
            "source_revision": "e" * 32,
        }
        assert await instance._poll_sources_once() == []
        assert len(instance.http.posts) == 1

    asyncio.run(scenario())
