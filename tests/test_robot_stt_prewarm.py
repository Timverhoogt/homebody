"""Synthetic subprocess prewarming: no native model, devices or external services."""
import json
import threading
import time

import pytest
from test_asr_worker import make_client, never, speech, wait_dead
from test_asr_worker import short_dir as short_dir_fixture  # noqa: F401

from homebody.robot_stt import RobotSttCancelled, RobotSttUnavailable


@pytest.fixture(name="short_dir")
def _short_dir(short_dir_fixture):  # noqa: F811
    return short_dir_fixture


def test_cold_warm_languages_and_activities_share_one_worker(short_dir):
    spawns = []
    client = make_client(short_dir, "ok", spawns=spawns)
    try:
        assert client.status()["inference_warm"] is False
        ready = client.prewarm(cancelled=never)
        assert ready["model_ready"] is True and ready["inference_warm"] is True
        pid = client.worker_pid
        for generation, language in enumerate(("en", "nl", "auto", "nl")):
            assert client.transcribe(speech(), session=f"activity-{generation}", generation=generation,
                                     language=language, cancelled=never).text
            client.cancel()  # completed activity: retain idle model, invalidate old operations
            assert client.worker_pid == pid
        assert client.prewarm(cancelled=never)["warmup_ms"] == ready["warmup_ms"]
        assert spawns == ["ok"]
    finally:
        client.close()
    assert wait_dead(pid)


@pytest.mark.parametrize("mode", ["hang_load", "hang_decode", "native_hang_load", "native_hang_decode"])
def test_stop_during_prewarm_never_resurrects(short_dir, mode, monkeypatch):
    client = make_client(short_dir, mode)
    decoding = threading.Event()
    if mode.endswith("decode"):
        client.status()  # finish loading: cancellation below must exercise submitted warmup
        original = client._read_reply
        def reading(*args):
            decoding.set()
            return original(*args)
        monkeypatch.setattr(client, "_read_reply", reading)
    errors = []
    def run():
        try:
            client.prewarm(cancelled=never)
        except (RobotSttCancelled, RobotSttUnavailable):
            errors.append(True)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 3
    while client.worker_pid is None and time.monotonic() < deadline:
        time.sleep(.01)
    pid = client.worker_pid
    if mode.endswith("decode"):
        assert decoding.wait(3)
    client.cancel(release=True)
    thread.join(3)
    assert not thread.is_alive() and errors and client.worker_pid is None
    assert wait_dead(pid)
    with pytest.raises(RobotSttCancelled):
        client.prewarm(cancelled=lambda: True)
    assert client.worker_pid is None
    client.close()


def test_concurrent_first_turn_waits_for_single_prewarm(short_dir):
    spawns = []
    client = make_client(short_dir, "ok", spawns=spawns)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(client.prewarm(cancelled=never)))
    thread.start()
    assert client._warming.wait(2)
    try:
        receipt = client.transcribe(speech(), session="first", generation=1, language="nl", cancelled=never)
        thread.join(3)
        assert receipt.text and outcomes[0]["inference_warm"] and spawns == ["ok"]
    finally:
        client.close()
        thread.join(3)


def test_changed_manifest_rejects_warm_reuse(short_dir):
    client = make_client(short_dir, "ok")
    try:
        client.prewarm(cancelled=never)
        manifest = json.loads(client._manifest_path.read_text())
        manifest["gpu"] = -1
        client._manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(RobotSttUnavailable, match="provisioning_changed"):
            client.prewarm(cancelled=never)
        assert client.worker_pid is None
    finally:
        client.close()


def test_warm_socket_trust_rechecked_before_decode(short_dir):
    client = make_client(short_dir, "ok")
    try:
        client.prewarm(cancelled=never)
        client._socket_path.chmod(0o666)
        with pytest.raises(RobotSttUnavailable, match="untrusted_endpoint"):
            client.prewarm(cancelled=never)
        assert client.worker_pid is None
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["hang_decode", "native_hang_decode"])
def test_hung_warmup_is_killed_and_reaped(short_dir, mode):
    client = make_client(short_dir, mode, decode_timeout=.3)
    try:
        client.status()
        pid = client.worker_pid
        with pytest.raises(RobotSttUnavailable, match="decode_timeout"):
            client.prewarm(cancelled=never)
        assert wait_dead(pid)
    finally:
        client.close()


def test_post_privacy_warm_reply_is_discarded(short_dir, monkeypatch):
    client = make_client(short_dir, "ok")
    original = client._read_reply
    def late(*args):
        raw = original(*args)
        client.cancel(release=True)
        return raw
    monkeypatch.setattr(client, "_read_reply", late)
    try:
        with pytest.raises(RobotSttCancelled):
            client.prewarm(cancelled=never)
        assert client.worker_pid is None
    finally:
        client.close()


@pytest.mark.parametrize("blocked", ["provider", "privacy", "meeting", "sleep", "stop", "kids_end"])
def test_runtime_only_admitted_current_robot_turn_can_prewarm(short_dir, blocked):
    from test_announcements import ready_runtime

    from homebody.config import AppConfig
    runtime = ready_runtime()
    config = AppConfig(stt_provider="robot")
    runtime.config_loader = lambda: config
    runtime._accept_wake_turn()
    spawns = []
    client = make_client(short_dir, "ok", spawns=spawns)
    runtime._robot_stt = client
    if blocked == "provider":
        config = AppConfig(stt_provider="configured")
    elif blocked == "privacy":
        runtime._privacy_requested.set()
    elif blocked in {"meeting", "sleep"}:
        runtime._power_mode = blocked
        runtime._meeting_until = time.monotonic() + 60
    elif blocked == "stop":
        runtime._request_conversation_stop()
    else:
        config = AppConfig(stt_provider="robot", kids_mode_enabled=True, kids_session_id="ended",
                           kids_age_band="7-9", kids_activity="story")
    try:
        runtime._prewarm_robot_stt(config)
        assert spawns == [] and getattr(runtime, "_robot_stt_warm_thread", None) is None
        assert runtime.robot.media.played == []
    finally:
        client.close()


def test_runtime_config_drift_cancels_load_without_capture_or_fallback(short_dir):
    from test_announcements import ready_runtime

    from homebody.config import AppConfig
    runtime = ready_runtime()
    config = AppConfig(stt_provider="robot")
    runtime.config_loader = lambda: config
    runtime._accept_wake_turn()
    client = make_client(short_dir, "native_hang_load")
    runtime._robot_stt = client
    try:
        runtime._prewarm_robot_stt(config)
        deadline = time.monotonic() + 3
        while client.worker_pid is None and time.monotonic() < deadline:
            time.sleep(.01)
        pid = client.worker_pid
        config = AppConfig(stt_provider="configured")
        runtime._robot_stt_warm_thread.join(3)
        assert not runtime._robot_stt_warm_thread.is_alive()
        assert wait_dead(pid) and client.worker_pid is None
        assert runtime.robot.media.played == []
    finally:
        client.close()


def test_runtime_privacy_releases_already_idle_model(short_dir):
    from test_announcements import ready_runtime
    runtime = ready_runtime()
    client = make_client(short_dir, "ok")
    runtime._robot_stt = client
    try:
        client.prewarm(cancelled=never)
        pid = client.worker_pid
        runtime._privacy_requested.set()
        runtime._request_conversation_stop()
        assert wait_dead(pid) and client.worker_pid is None
    finally:
        client.close()
