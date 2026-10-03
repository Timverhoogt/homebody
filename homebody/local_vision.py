"""Answer camera questions with a vision-language model on your own hardware.

Any OpenAI-compatible ``/v1/chat/completions`` server that accepts images works: Ollama,
llama.cpp ``llama-server``, vLLM or NVIDIA's Jetson containers. On a Jetson Orin Nano the model
runs on the GPU next to Reachy, so a camera frame never leaves the building. Only the text
answer is handed to the conversation.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

MAX_ANSWER_CHARS = 1200
MAX_JPEG_BYTES = 1_000_000

SYSTEM_PROMPT = (
    "You are the eyes of a small desk robot. Describe only what is visible in the single image. "
    "Be concrete and brief: name objects, colours, readable text, and where things are. "
    "Never guess who a person is, and do not describe faces or bodies beyond what the question needs."
)


class LocalVisionError(RuntimeError):
    """The local vision server is unreachable or returned something unusable."""


class LocalVisionClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LocalVisionClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def describe(self, jpeg: bytes, question: str) -> dict[str, object]:
        """Return ``{"answer", "model", "latency_ms"}`` for one JPEG frame."""
        if not jpeg or len(jpeg) > MAX_JPEG_BYTES:
            raise LocalVisionError("Camera frame must be between 1 byte and 1 MB")
        prompt = " ".join(question.split())[:500] or "What do you see?"
        image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        payload = {
            "model": self._model,
            "temperature": 0.2,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
        }
        started = time.monotonic()
        body = self._post_json("/chat/completions", payload)
        answer = _message_text(body)
        if not answer:
            raise LocalVisionError("The local vision model returned an empty answer")
        return {
            "answer": answer[:MAX_ANSWER_CHARS],
            "model": str(body.get("model") or self._model)[:200],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def health(self) -> dict[str, object]:
        """Check the server answers and whether it lists the configured model."""
        try:
            response = self._client.get(f"{self._base_url}/models")
            response.raise_for_status()
            listed = [str(item.get("id")) for item in response.json().get("data", []) if isinstance(item, dict)]
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LocalVisionError(f"Local vision server is not reachable: {_short(exc)}") from exc
        return {"reachable": True, "model": self._model, "model_listed": self._model in listed, "models": listed[:20]}

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        try:
            response = self._client.post(f"{self._base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise LocalVisionError(f"Local vision server is not reachable: {_short(exc)}") from exc
        if response.status_code >= 400:
            detail = _short(response.text)
            raise LocalVisionError(f"Local vision server answered HTTP {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LocalVisionError("Local vision server did not return JSON") from exc
        if not isinstance(body, dict):
            raise LocalVisionError("Local vision server returned an unexpected payload")
        return body


def _message_text(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return " ".join(str(content or "").split())


def _short(value: object) -> str:
    return " ".join(str(value).split())[:160]
