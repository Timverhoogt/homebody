"""Real robot entry points for the household camera policy and direct controls.

Realtime: the actual ``_run_realtime_conversation`` loop with the companion one-frame look.
Pipeline: the actual ``_run_conversation`` loop, the real bridge client and host Everyday route.
Only provider events, STT/TTS, SDK capture, the vision answer and motor hardware are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from test_companion import make_runtime
from test_companion_realtime_consent import camera, transcript
from test_everyday_camera_consent import pipeline_runtime
from test_everyday_local_transport import run_host

from homebody.realtime_client import RealtimeEvent

APPROVED = {"camera_enabled": True, "camera_natural_requests_enabled": True, "household_setup_version": 1}


def power(mode, identifier="power-1"):
    return RealtimeEvent(
        "response.output_item.done",
        {
            "item": {
                "type": "function_call",
                "status": "completed",
                "name": "set_reachy_power_mode",
                "call_id": identifier,
                "arguments": json.dumps({"mode": mode}),
            }
        },
    )


def realtime(batches, **settings):
    runtime, client, captures = make_runtime(conversation_mode="realtime", **settings)
    runtime._motion = None
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._read_16k_frame = lambda: None
    modes, results, errors = [], [], []
    runtime.set_power_mode = lambda mode, **kw: modes.append(mode) or {"power_mode": mode}
    trailing = [0]
    power_completed = threading.Event()
    original_launch = runtime.launch_everyday_tool
    launched = []

    def launch(*args, **kwargs):
        deliver = kwargs["deliver"]
        def completed(result):
            try:
                deliver(result)
            finally:
                power_completed.set()
        kwargs["deliver"] = completed
        launched.append(True)
        return original_launch(*args, **kwargs)

    runtime.launch_everyday_tool = launch

    def events():
        if batches:
            return batches.pop(0)
        trailing[0] += 1
        if trailing[0] > 4:
            # The fake stream must not manufacture Stop while its tool worker is
            # still admitted. Wait on actual completion, not event-loop spins.
            if launched:
                assert power_completed.wait(2), "Realtime tool worker did not complete"
            runtime._request_conversation_stop()
        return []

    session = SimpleNamespace(
        start=lambda: None,
        close=lambda: None,
        events=events,
        send_camera_error=lambda *a: errors.append(a),
        send_tool_result=lambda call_id, result, **kw: results.append((call_id, result)),
    )
    runtime._new_realtime_session = lambda *a, **k: session

    def describe(_session, identifier, question, _config, *, authority, is_current):
        runtime.look_at_this(question, origin="voice", authority=authority)

    runtime._describe_for_realtime = describe
    with runtime._voice_activity_lock:
        runtime._run_realtime_conversation(runtime.config_loader())
    return SimpleNamespace(captures=captures, modes=modes, results=results, errors=errors, client=client)


@pytest.mark.parametrize(
    "settings,looks",
    [(APPROVED, 1), ({"camera_enabled": True}, 0), ({**APPROVED, "camera_enabled": False}, 0)],
)
def test_realtime_natural_request_needs_household_approval_and_camera_on(settings, looks):
    run = realtime([[transcript("Does this shirt look good on me?"), camera()]], **settings)
    assert len(run.captures) == looks and len(run.client.looks) == looks
    assert len(run.errors) == 1 - looks


@pytest.mark.parametrize("words,modes", [("Go to sleep", ["sleep"]), ("What colour is it?", [])])
def test_realtime_power_after_a_look_follows_only_the_persons_direct_words(words, modes):
    run = realtime(
        [
            [transcript("Look at this letter"), camera()],
            [transcript(words, "user-2"), power("sleep")],
        ],
        camera_enabled=True,
    )
    assert len(run.captures) == 1
    assert run.modes == modes
    if not modes:
        assert run.results and run.results[-1][1]["ok"] is False and "tool-free" in run.results[-1][1]["error"]


def test_pipeline_sleep_and_direct_volume_after_a_look_are_not_image_follow_ups():
    async def main(bridge, server, module):
        words = ["Look at this letter", "Set the volume to 30", "Go to sleep"]
        runtime, client, captures, config, spoken, converse = pipeline_runtime(server, words, camera_enabled=True)
        modes, volumes = [], []
        runtime.set_power_mode = lambda mode, **kw: modes.append(mode) or {"power_mode": mode}
        runtime._speaker = SimpleNamespace(
            read=lambda: {"volume": 24, "verified": True},
            set=lambda **kw: volumes.append(kw) or {**kw, "verified": True},
        )
        await asyncio.wait_for(asyncio.to_thread(converse), 10)
        assert words == [] and len(captures) == 1 and len(client.looks) == 1
        assert client.follow_ups == [] and client.turns == []  # neither went to the image follow-up or a model
        assert [call.get("volume") for call in volumes] == [30]
        assert modes == ["sleep"]
        assert spoken == [client.answer, "Okay, the volume is changed."]  # nothing spoken after Sleep

    run_host(main)


def test_pipeline_quoted_sleep_after_a_look_stays_a_visual_follow_up():
    async def main(bridge, server, module):
        words = ["Look at this letter", 'What does "go to sleep" mean here?']
        runtime, client, captures, config, spoken, converse = pipeline_runtime(server, words, camera_enabled=True)
        modes = []
        runtime.set_power_mode = lambda mode, **kw: modes.append(mode) or {"power_mode": mode}
        await asyncio.wait_for(asyncio.to_thread(converse), 10)
        assert modes == [] and len(client.follow_ups) == 1

    run_host(main)
