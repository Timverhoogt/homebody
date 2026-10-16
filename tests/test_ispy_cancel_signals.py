"""Turn cancellation remains expected even before the Kids generation changes."""
import threading

import pytest
from test_ispy_expected_cancellation import Rig


@pytest.mark.parametrize("signal", ["turn_stop", "privacy", "sleep", "app_stop"])
def test_current_generation_cancellation_is_not_round_failure(monkeypatch, tmp_path, signal):
    entered, release = threading.Event(), threading.Event()

    def held_frame():
        entered.set()
        release.wait(3)
        return b"jpeg"

    rig = Rig(monkeypatch, tmp_path, frame=held_frame)
    thread, raised = rig.run_in_thread()
    assert entered.wait(3)
    runtime = rig.runtime
    if signal == "turn_stop":
        runtime._conversation_stop_requested.set()
    elif signal == "privacy":
        runtime._privacy_requested.set()
    elif signal == "sleep":
        runtime._power_mode = "sleep"
    else:
        runtime.stop_event.set()
    runtime._set_status("waiting_for_wake_word", "Control transition owns this status")
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert raised == [] and rig.errors == []
    assert rig.stops == []
    assert runtime._status.detail == "Control transition owns this status"
    assert rig.events == ["tts", "camera"]
