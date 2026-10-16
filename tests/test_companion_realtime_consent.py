"""Native Realtime camera dispatch uses only fresh completed user intent, never a tool's purpose."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_companion import make_runtime

from homebody.realtime_client import RealtimeEvent


def camera(identifier="camera-1"):
    return RealtimeEvent(
        "response.output_item.done",
        {
            "item": {
                "type": "function_call",
                "status": "completed",
                "name": "capture_reachy_camera",
                "call_id": identifier,
                "arguments": json.dumps({"purpose": "Look at this letter"}),
            }
        },
    )


def transcript(text, item="user-1"):
    return RealtimeEvent(
        "conversation.item.input_audio_transcription.completed", {"transcript": text, "item_id": item}
    )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("ordinary", 0),
        ("negated", 0),
        ("quoted", 0),
        ("single", 1),
        ("duplicate", 1),
        ("refresh", 2),
        ("tool_before_transcript", 1),
        ("stale_transcript", 0),
    ],
)
def test_native_camera_needs_one_fresh_explicit_request_per_capture(case, expected):
    runtime, client, captures = make_runtime(conversation_mode="realtime")
    runtime._motion = None
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._read_16k_frame = lambda: None
    runtime._new_realtime_session = lambda *a, **k: session
    errors, descriptions = [], []
    scenarios = {
        "ordinary": [[transcript("What is this project about?"), camera()]],
        "negated": [[transcript("Do not look at this letter"), camera()]],
        "quoted": [[transcript('My colleague said "look at this" yesterday'), camera()]],
        "single": [[transcript("Look at this letter"), camera()]],
        "duplicate": [[transcript("Look at this letter"), camera(), camera("camera-2")]],
        "refresh": [
            [transcript("Look at this letter"), camera()],
            [transcript("Look again", "user-2"), camera("camera-2")],
        ],
        "tool_before_transcript": [[camera()], [transcript("Look at this letter")]],
        "stale_transcript": [
            [RealtimeEvent("input_audio_buffer.committed", {"item_id": "user-new"}), camera()],
            [transcript("Look at this letter", "user-old")],
        ],
    }
    batches = scenarios[case]
    trailing = [0]

    def events():
        if batches:
            return batches.pop(0)
        trailing[0] += 1
        if trailing[0] > 4:
            runtime._request_conversation_stop()
        return []

    session = SimpleNamespace(
        start=lambda: None, close=lambda: None, events=events, send_camera_error=lambda *a: errors.append(a)
    )

    def describe(_session, identifier, question, _config):
        # Real one-frame orchestration. Thread scheduling/Stop fences have separate held-worker tests.
        descriptions.append((identifier, runtime.look_at_this(question, origin="voice")))

    runtime._describe_for_realtime = describe
    with runtime._voice_activity_lock:
        runtime._run_realtime_conversation(runtime.config_loader())
    assert len(captures) == expected
    assert len(client.looks) == expected
    assert len(descriptions) == expected
    if case in {"ordinary", "negated", "quoted", "duplicate"}:
        assert len(errors) == 1
    assert runtime._look_context is None  # Stop tears down actual private context
