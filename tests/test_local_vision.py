from __future__ import annotations

import json
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_gate_1r_3_realtime_lifecycle import make_runtime as make_realtime_runtime

import reachy_mini_hermes.main as main_module
from reachy_mini_hermes.config import AppConfig
from reachy_mini_hermes.local_vision import LocalVisionClient, LocalVisionError
from reachy_mini_hermes.main import ReachyMiniHermes
from reachy_mini_hermes.realtime_client import RealtimeEvent
from reachy_mini_hermes.voice_realtime import camera_call_purpose

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


def vision_client(handler) -> LocalVisionClient:  # type: ignore[no-untyped-def]
    return LocalVisionClient("http://jetson.local:11434/v1/", "qwen2.5vl:3b", transport=httpx.MockTransport(handler))


def test_describe_sends_one_inline_image_and_returns_only_text() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://jetson.local:11434/v1/chat/completions"
        seen.append(json.loads(request.content))
        content = " A red\n mug. "
        return httpx.Response(200, json={"model": "qwen2.5vl:3b", "choices": [{"message": {"content": content}}]})

    with vision_client(handler) as client:
        result = client.describe(JPEG, "What is on the desk?")

    assert result["answer"] == "A red mug."
    assert result["model"] == "qwen2.5vl:3b"
    payload = seen[0]
    assert payload["model"] == "qwen2.5vl:3b"
    user = payload["messages"][1]["content"]  # type: ignore[index]
    assert user[0] == {"type": "text", "text": "What is on the desk?"}
    assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_describe_accepts_content_parts_and_rejects_empty_or_failed_answers() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "A plant"}]}}]}),
            httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
            httpx.Response(404, text="model 'qwen2.5vl:3b' not found, try pulling it first"),
        ]
    )
    with vision_client(lambda request: next(responses)) as client:
        assert client.describe(JPEG, "?")["answer"] == "A plant"
        with pytest.raises(LocalVisionError, match="empty answer"):
            client.describe(JPEG, "?")
        with pytest.raises(LocalVisionError, match="HTTP 404.*try pulling"):
            client.describe(JPEG, "?")
        with pytest.raises(LocalVisionError, match="1 MB"):
            client.describe(b"x" * 1_000_001, "?")


def test_unreachable_server_is_reported_plainly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with vision_client(handler) as client:
        with pytest.raises(LocalVisionError, match="not reachable"):
            client.health()


def test_health_reports_whether_the_model_is_pulled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gemma3:4b"}, {"id": "qwen2.5vl:3b"}]})

    with vision_client(handler) as client:
        health = client.health()

    assert health["model_listed"] is True and health["models"] == ["gemma3:4b", "qwen2.5vl:3b"]


class FakeVision:
    def __init__(self, answer: str = "A blue notebook", error: str = "") -> None:
        self.answer = answer
        self.error = error
        self.questions: list[str] = []

    def __enter__(self) -> FakeVision:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def describe(self, jpeg: bytes, question: str) -> dict[str, object]:
        self.questions.append(question)
        if self.error:
            raise LocalVisionError(self.error)
        return {"answer": self.answer, "model": "qwen2.5vl:3b", "latency_ms": 840}


def vision_config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {"local_vision_enabled": True, "camera_enabled": True}
    values.update(overrides)
    return AppConfig(**values)  # type: ignore[arg-type]


def test_runtime_answers_locally_and_records_status() -> None:
    runtime = make_realtime_runtime()
    vision = FakeVision()
    runtime._new_local_vision_client = lambda config: vision  # type: ignore[method-assign]
    runtime.camera_snapshot = lambda: JPEG  # type: ignore[method-assign]

    result = runtime.describe_camera_view("What is that?", config=vision_config())

    assert result["answer"] == "A blue notebook"
    assert vision.questions == ["What is that?"]
    status = runtime.status()
    assert status["local_vision_answers"] == 1 and status["local_vision_last_latency_ms"] == 840


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (vision_config(local_vision_enabled=False), "Local vision is turned off"),
        (vision_config(camera_enabled=False), "Camera access is disabled"),
    ],
)
def test_runtime_refuses_when_local_vision_or_camera_is_off(config: AppConfig, message: str) -> None:
    runtime = make_realtime_runtime()
    runtime.camera_snapshot = lambda: pytest.fail("no frame may be captured")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=message):
        runtime.describe_camera_view("?", config=config)


def test_runtime_respects_privacy_modes_before_capturing() -> None:
    runtime = make_realtime_runtime()
    runtime._power_mode = "sleep"
    runtime._privacy_requested.set()
    runtime._new_local_vision_client = lambda config: pytest.fail("no model call in privacy")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="privacy"):
        runtime.describe_camera_view("?", config=vision_config())


def test_runtime_reports_vision_server_errors() -> None:
    runtime = make_realtime_runtime()
    runtime._new_local_vision_client = lambda config: FakeVision(error="Local vision server is not reachable")  # type: ignore[method-assign]
    runtime.camera_snapshot = lambda: JPEG  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="not reachable"):
        runtime.describe_camera_view("?", config=vision_config())

    assert runtime.status()["local_vision_last_error"] == "Local vision server is not reachable"


def test_camera_call_purpose_reads_the_tool_arguments() -> None:
    payload = {"item": {"arguments": json.dumps({"purpose": "  read the   label on the box "})}}

    assert camera_call_purpose(payload) == "read the label on the box"
    assert camera_call_purpose({"item": {"arguments": "not json"}}) == ""


def test_realtime_camera_call_sends_a_local_description_instead_of_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_realtime_runtime()
    vision = FakeVision(answer="A cup of tea next to a laptop")
    runtime._new_local_vision_client = lambda config: vision  # type: ignore[method-assign]
    runtime._capture_camera_jpeg = lambda **kwargs: JPEG  # type: ignore[method-assign]
    sessions: list[object] = []

    class CameraSession:
        def __init__(self, config: AppConfig, **_kwargs: object) -> None:
            self.results: list[tuple[str, dict[str, object]]] = []
            self.frames: list[str] = []
            self.sent = False
            sessions.append(self)

        def start(self) -> None:
            pass

        def send_audio(self, samples: np.ndarray) -> None:
            pass

        def events(self) -> list[RealtimeEvent]:
            if self.sent:
                runtime.cancel_agent_work("stopped")
                return []
            self.sent = True
            item = {
                "type": "function_call",
                "name": "capture_reachy_camera",
                "status": "completed",
                "call_id": "call-1",
                "arguments": json.dumps({"purpose": "what is on the table"}),
            }
            return [RealtimeEvent("response.output_item.done", {"item": item})]

        def send_tool_result(self, call_id: str, result: dict[str, object], **_kwargs: object) -> None:
            self.results.append((call_id, result))

        def send_camera_frame(self, call_id: str, jpeg: bytes) -> None:
            self.frames.append(call_id)

        def send_camera_error(self, call_id: str, message: str) -> None:
            raise AssertionError(message)

        def close(self) -> None:
            pass

    monkeypatch.setattr("reachy_mini_hermes.runtime.RealtimeBridgeSession", CameraSession)

    def read_frame() -> np.ndarray:
        time.sleep(0.001)
        return np.zeros(160, dtype=np.float32)

    runtime._read_16k_frame = read_frame  # type: ignore[method-assign]
    config = AppConfig(
        conversation_mode="realtime",
        conversation_timeout_seconds=5.0,
        camera_enabled=True,
        local_vision_enabled=True,
    )
    runtime.config_loader = lambda: config  # type: ignore[method-assign]

    runtime._run_realtime_conversation(config)

    session = sessions[0]
    assert session.frames == []  # type: ignore[attr-defined]
    assert session.results == [  # type: ignore[attr-defined]
        (
            "call-1",
            {
                "ok": True,
                "image_attached": False,
                "seen_by": "local vision model on Reachy's computer",
                "description": "A cup of tea next to a laptop",
            },
        )
    ]
    assert vision.questions == ["what is on the table"]


def build_client(monkeypatch: pytest.MonkeyPatch, config: AppConfig) -> tuple[ReachyMiniHermes, TestClient, list]:
    saved: list[AppConfig] = []
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "save_config", lambda value: saved.append(value))
    app = ReachyMiniHermes(False)
    return app, TestClient(app.settings_app), saved


def test_describe_route_needs_a_runtime_and_maps_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client, _saved = build_client(monkeypatch, vision_config())

    assert client.post("/api/vision/describe", json={"question": "?"}).status_code == 409

    class Runtime:
        kids_controls_locked = False

        def describe_camera_view(self, question: str) -> dict[str, object]:
            if question == "fail":
                raise RuntimeError("Local vision is turned off in Reachy settings")
            return {"answer": "A lamp", "model": "m", "latency_ms": 5}

    app._runtime = Runtime()  # type: ignore[assignment]
    assert client.post("/api/vision/describe", json={"question": "What?"}).json()["answer"] == "A lamp"
    refused = client.post("/api/vision/describe", json={"question": "fail"})
    assert refused.status_code == 409 and "turned off" in refused.json()["detail"]
    assert client.post("/api/vision/describe", json={"question": "x" * 301}).status_code == 422


def test_vision_test_route_checks_the_form_values_before_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, saved = build_client(monkeypatch, AppConfig())
    used: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, url: str, model: str, *, timeout: float) -> None:
            used.append((url, model))

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def health(self) -> dict[str, object]:
            return {"reachable": True, "model": used[-1][1], "model_listed": True, "models": []}

    monkeypatch.setattr(main_module, "LocalVisionClient", FakeClient)

    ok = client.post(
        "/api/vision/test",
        json={"local_vision_url": "http://192.168.1.40:11434/v1", "local_vision_model": "gemma3:4b"},
    )
    assert ok.status_code == 200 and ok.json()["model_listed"] is True
    assert used == [("http://192.168.1.40:11434/v1", "gemma3:4b")]
    assert client.post("/api/vision/test", json={"local_vision_url": "ftp://x"}).status_code == 400
    assert saved == []


def test_redirecting_the_vision_server_needs_the_current_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, client, saved = build_client(monkeypatch, AppConfig(api_key="sk-secret"))

    redirect = client.post("/api/settings", json={"local_vision_url": "http://attacker.example/v1"})
    assert redirect.status_code == 403
    assert saved == []

    allowed = client.post(
        "/api/settings",
        json={"local_vision_url": "http://jetson.local:11434/v1", "current_api_key": "sk-secret"},
    )
    assert allowed.status_code == 200
    assert saved[-1].local_vision_url == "http://jetson.local:11434/v1"

    toggled = client.post("/api/settings", json={"local_vision_enabled": True, "local_vision_model": "gemma3:4b"})
    assert toggled.status_code == 200


@pytest.mark.parametrize(
    "updates",
    [{"local_vision_url": "not-a-url"}, {"local_vision_url": "file:///etc/passwd"}, {"local_vision_model": " "}],
)
def test_invalid_local_vision_settings_are_rejected(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AppConfig(**updates)  # type: ignore[arg-type]


def test_settings_and_camera_card_expose_local_vision() -> None:
    from pathlib import Path

    static = Path(main_module.__file__).parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "main.js").read_text(encoding="utf-8")

    for element in ("local_vision_enabled", "local_vision_url", "local_vision_model", "local_ai_accelerator"):
        assert f'name="{element}"' in html
        assert f'"{element}"' in script
    assert 'id="local-vision-ask"' in html and "/api/vision/describe" in script and "/api/vision/test" in script
    assert "answer.textContent = `${body.answer}" in script
