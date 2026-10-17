"""Owner Look now during a continuous Kids Chat: the real Owner boundary and route, real runtime gate
and the real pipeline conversation loop (real endpoint recorder over synthetic silent frames).

Mechanism under test (retained-log finding): the continuous conversation owns the voice slot for
its whole length, so 0.5.33's nonblocking acquire in ``show_tell_look_now`` returned 409 and no
Show and Tell request reached the bridge. Loopback fakes only; NOT physical acceptance.
"""

import threading
import time

import numpy as np
import pytest
from test_owner_show_and_tell import kids_state, persisted  # noqa: F401 - fixture
from test_show_and_tell_runtime import RESULT, Bridge

from homebody import show_and_tell
from homebody.kids_mode import KidsProfile

LOOK_NOW = "/api/kids/show-and-tell/look-now"


class HookedBridge(Bridge):
    def health(self):
        self.hooks.get("health", lambda: None)()
        return super().health()


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


@pytest.fixture
def chat(persisted, monkeypatch):  # noqa: F811 - pytest fixture injection
    _runtime_for, runtime, client, headers, events, _path = persisted
    hooks: dict[object, object] = {}
    monkeypatch.setattr(show_and_tell, "MIN_LOOK_INTERVAL", 0.0)
    monkeypatch.setattr(runtime, "_new_bridge_client", lambda config: HookedBridge(events, hooks, config))
    runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime.start_kids_mode(KidsProfile(duration_minutes=15), greet=False)
    original_record = runtime._record_utterance

    def record(config, **kwargs):
        events.append("record")
        return original_record(config, **kwargs)

    def silent_frame():
        time.sleep(0.003)
        return np.zeros(48, dtype=np.float32)

    runtime._record_utterance = record
    runtime._read_16k_frame = silent_frame
    threads: list[threading.Thread] = []

    def converse() -> threading.Thread:
        """What the wake loop does: accept the turn, own the slot for the whole continuous chat."""
        config = runtime._kids_voice_config(runtime.config_loader())
        runtime._accept_wake_turn()

        def body():
            with runtime._voice_activity_lock:
                runtime._run_conversation(config)

        thread = threading.Thread(target=body, name="kids-conversation", daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    yield runtime, client, headers, events, hooks, converse
    runtime.stop_event.set()  # ends any still-open synthetic listen
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive(), "conversation did not end"


def permits(events):
    return [item for item in events if isinstance(item, tuple) and item[0] == "permit"]


def test_baseline_mechanism_slot_held_by_conversation_returned_409_and_reached_no_bridge(chat, monkeypatch):
    runtime, client, headers, events, _hooks, converse = chat
    # 0.5.33 had no handoff: model it by a conversation that registers no Look now consumer.
    monkeypatch.setattr(runtime, "_show_tell_register_consumer", lambda _session: None)
    converse()
    wait_for(lambda: "record" in events)
    response = client.post(LOOK_NOW, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "Wait until Reachy finishes the current turn"
    assert not permits(events) and "capture" not in events


def test_look_now_during_listening_is_run_by_the_same_conversation_owner(chat):
    runtime, client, headers, events, hooks, converse = chat
    owners: list[str] = []
    hooks["permit"] = lambda: owners.append(threading.current_thread().name)
    converse()
    wait_for(lambda: "record" in events)
    response = client.post(LOOK_NOW, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True and response.json()["queued"] is True
    wait_for(lambda: ("speak", RESULT) in events and events.count("record") >= 2)

    assert owners == ["kids-conversation"], "the voice owner itself ran the look"
    assert events.count("capture") == 1 and len(permits(events)) == 1
    first_record = events.index("record")
    order = [events.index(item) for item in (permits(events)[0], ("speak", "LOOKING"), "capture", "look",
                                             ("speak", RESULT))]
    assert first_record < order[0] and order == sorted(order)
    # No listening overlapped the look; the ordinary continuous listen resumed only afterwards.
    assert "record" not in events[first_record + 1:order[-1]]
    assert not [item for item in events if isinstance(item, tuple) and item[0] == "chat"]
    assert kids_state(client)["queued"] is False and kids_state(client)["looking"] is False
    text = client.get("/api/status").text
    assert RESULT not in text and runtime._kids_session_id not in text


def test_repeated_look_now_requests_stay_one_look(chat):
    _runtime, client, headers, events, hooks, converse = chat
    in_look, release = threading.Event(), threading.Event()

    def hold_permit():
        in_look.set()
        assert release.wait(5)

    hooks["permit"] = hold_permit
    converse()
    wait_for(lambda: "record" in events)
    assert client.post(LOOK_NOW, headers=headers).json()["queued"] is True
    assert in_look.wait(5)
    repeats = [client.post(LOOK_NOW, headers=headers) for _ in range(3)]
    assert all(r.status_code == 200 and r.json()["queued"] is False and r.json()["looking"] is True
               for r in repeats), [r.text for r in repeats]
    release.set()
    wait_for(lambda: ("speak", RESULT) in events and events.count("record") >= 2)
    time.sleep(0.1)
    assert events.count("capture") == 1 and len(permits(events)) == 1


@pytest.mark.parametrize("cancel", ["end", "stop", "privacy", "opt_out"])
def test_cancellation_drops_the_queued_look_without_late_look_or_relisten(chat, cancel):
    runtime, client, headers, events, hooks, converse = chat
    responses = []

    def at_health():  # inside the conversation: slot held, consumer registered, not yet listening
        responses.append(client.post(LOOK_NOW, headers=headers))
        assert kids_state(client)["queued"] is True
        if cancel == "end":
            runtime.stop_kids_mode(fold=False)
        elif cancel == "stop":
            runtime._request_conversation_stop()
        elif cancel == "privacy":
            runtime._privacy_requested.set()
            runtime._request_conversation_stop()
        else:
            assert client.post("/api/kids/show-and-tell/setting", json={"enabled": False},
                               headers=headers).status_code == 200

    hooks["health"] = at_health
    thread = converse()
    if cancel == "opt_out":
        wait_for(lambda: "record" in events)  # the ordinary Kids Chat continues, without the look
        runtime.stop_event.set()
    thread.join(5)
    assert not thread.is_alive()
    runtime._privacy_requested.clear()

    assert responses[0].status_code == 200 and responses[0].json()["queued"] is True
    assert not permits(events) and "capture" not in events and "look" not in events
    if cancel != "opt_out":
        assert "record" not in events, "a cancelled request never re-opens the microphone"
    assert runtime._show_tell_pending is None


def test_other_slot_owners_still_refuse_truthfully(chat):
    runtime, client, headers, events, _hooks, _converse = chat
    with runtime._voice_activity_lock:  # e.g. an announcement or a typed I Spy turn, not this chat
        response = client.post(LOOK_NOW, headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "Wait until Reachy finishes the current turn"
    assert not permits(events)
