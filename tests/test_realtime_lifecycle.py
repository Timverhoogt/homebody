"""The response scheduler is a standalone, stdlib-only state machine."""
from __future__ import annotations

import asyncio
import importlib

import pytest


def lifecycle_class():
    return importlib.import_module("companion.realtime_lifecycle").RealtimeResponseLifecycle


def test_bridge_retains_the_public_lifecycle_alias():
    from companion import hermes_reachy_bridge

    assert hermes_reachy_bridge.RealtimeResponseLifecycle is lifecycle_class()


@pytest.mark.parametrize("terminal", ["response.done", "response.cancelled", "response.failed"])
def test_active_response_blocks_duplicate_continuations_until_terminal(terminal):
    async def scenario():
        sent = []
        emitted = asyncio.Event()

        async def send(event):
            sent.append(event)
            emitted.set()

        lifecycle = lifecycle_class()(send, settle_seconds=0)
        try:
            await lifecycle.observe({"type": "response.created", "response": {"id": "first"}})
            assert await lifecycle.request_create()
            assert await lifecycle.request_create()
            # A scheduler turn must not send while a response is active.
            await asyncio.sleep(0)
            assert sent == []
            await lifecycle.observe({"type": terminal})
            await asyncio.wait_for(emitted.wait(), timeout=1)
            assert sent == [{"type": "response.create"}]
            # The in-flight create sentinel must suppress further sends.
            emitted.clear()
            assert await lifecycle.request_create()
            await asyncio.sleep(0)
            assert not emitted.is_set()
        finally:
            await lifecycle.close()

    asyncio.run(scenario())


def test_barge_in_invalidates_already_queued_continuation():
    async def scenario():
        sent = []

        async def send(event):
            sent.append(event)

        lifecycle = lifecycle_class()(send, settle_seconds=0)
        try:
            old_generation = lifecycle.generation
            assert await lifecycle.request_create(old_generation)
            await lifecycle.observe({"type": "input_audio_buffer.speech_started"})
            assert lifecycle.generation == old_generation + 1
            assert not await lifecycle.request_create(old_generation)
            assert not await lifecycle.request_create()
            await lifecycle.observe({"type": "input_audio_buffer.speech_stopped"})
            await asyncio.sleep(0)
            assert sent == []
        finally:
            await lifecycle.close()

    asyncio.run(scenario())


def test_close_cancels_and_joins_queued_send():
    async def scenario():
        sent = []

        async def send(event):
            sent.append(event)

        lifecycle = lifecycle_class()(send, settle_seconds=0)
        assert await lifecycle.request_create()
        await lifecycle.close()
        await lifecycle.close()
        await asyncio.sleep(0)
        assert sent == []

    asyncio.run(scenario())
