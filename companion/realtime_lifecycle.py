"""Serialize Realtime continuations independently of bridge routes and providers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class RealtimeResponseLifecycle:
    """Serialize explicit Realtime responses and invalidate them on barge-in."""

    _CREATING = "__creating__"

    def __init__(
        self,
        send_json: Callable[[dict[str, object]], Awaitable[None]],
        *,
        settle_seconds: float = 0.05,
    ) -> None:
        self._send_json = send_json
        self._settle_seconds = settle_seconds
        self._active_response_id = ""
        self._generation = 0
        self._speech_active = False
        self._pending_generation: int | None = None
        self._create_task: asyncio.Task[None] | None = None

    @property
    def generation(self) -> int:
        return self._generation

    async def observe(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "input_audio_buffer.speech_started":
            self._generation += 1
            self._speech_active = True
            self._pending_generation = None
            self._cancel_scheduled_create()
            return
        if kind == "input_audio_buffer.speech_stopped":
            self._speech_active = False
            return
        if kind == "response.created":
            response = event.get("response")
            response_id = str(response.get("id") or "") if isinstance(response, dict) else ""
            self._active_response_id = response_id or self._CREATING
            self._cancel_scheduled_create()
            return
        if kind not in {"response.done", "response.cancelled", "response.failed"}:
            return
        self._active_response_id = ""
        self._schedule_create_if_needed()

    async def request_create(self, generation: int | None = None) -> bool:
        """Queue one continuation unless its originating turn was interrupted."""
        requested_generation = self._generation if generation is None else generation
        if requested_generation != self._generation or self._speech_active:
            return False
        self._pending_generation = requested_generation
        if not self._active_response_id:
            self._schedule_create_if_needed()
        return True

    async def close(self) -> None:
        task = self._create_task
        self._create_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _cancel_scheduled_create(self) -> None:
        task = self._create_task
        self._create_task = None
        if task is not None:
            task.cancel()

    def _schedule_create_if_needed(self) -> None:
        if (
            self._pending_generation is None
            or self._active_response_id
            or self._speech_active
            or (self._create_task is not None and not self._create_task.done())
        ):
            return
        self._create_task = asyncio.create_task(self._create_after_settle())

    async def _create_after_settle(self) -> None:
        try:
            await asyncio.sleep(self._settle_seconds)
            generation = self._pending_generation
            if generation is None or generation != self._generation or self._active_response_id or self._speech_active:
                return
            self._pending_generation = None
            self._active_response_id = self._CREATING
            try:
                await self._send_json({"type": "response.create"})
            except Exception:
                self._active_response_id = ""
                raise
        finally:
            if self._create_task is asyncio.current_task():
                self._create_task = None
