"""Token-scoped cancellation must preserve live gameplay and fence late observations."""
import asyncio
import json

import pytest
from test_ispy_target_variety import CHAIR, SESSION, Request
from test_ispy_visual_bridge import VisualHarness, until


@pytest.mark.parametrize("stage", ["pending", "completed", "held"])
def test_abandoned_view_consumes_budget_and_rejects_replay_or_late_result(monkeypatch, stage):
    h = VisualHarness(monkeypatch)

    async def scenario():
        token = await h.begin()
        pending = None
        release = asyncio.Event()
        if stage == "completed":
            await h.observe([CHAIR])
        elif stage == "held":
            h.vision.gate = release
            h.vision.requests.clear()
            pending = asyncio.create_task(h.observe([CHAIR]))
            await until(lambda: bool(h.vision.requests))
        request = Request({"session_id": SESSION, "view_token": token})
        receipt = json.loads((await h.bridge.kids_ispy_cancel(request)).text)
        assert receipt == {"cancelled": True, "view_token": token}
        assert h.state["ispy_role"] == "player_picker"
        assert h.state["ispy_player_phase"] == "awaiting_clue"
        assert h.state["ispy_player_scans"] == 1
        assert h.state["ispy_player_candidates"] == []
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_cancel(request)
        assert h.state["ispy_player_scans"] == 1
        if pending:
            release.set()
            with pytest.raises(h.module.web.HTTPConflict):
                await pending
        async def clear(*_):
            return False
        monkeypatch.setattr(h.bridge, "_moderation_flagged", clear)
        next_turn = await h.chat("It is round")
        assert next_turn["ispy_next_action"] == "prepare_player_view"
        assert next_turn["ispy_view_token"] != token
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_cancel(request)
        await h.observe([])
        no_more = await h.chat("It is red")
        assert no_more["ispy_next_action"] == ""
        assert h.state["ispy_player_scans"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, "", "A" * 32, "a" * 31, 123])
def test_bad_scoped_cancel_cannot_become_whole_session_cancel(monkeypatch, value):
    h = VisualHarness(monkeypatch)

    async def scenario():
        token = await h.begin()
        with pytest.raises(h.module.web.HTTPBadRequest):
            await h.bridge.kids_ispy_cancel(Request({"session_id": SESSION, "view_token": value}))
        assert h.state["ispy_view_token"] == token
        assert h.state["ispy_player_scans"] == 0

    asyncio.run(scenario())
