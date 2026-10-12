"""Robot side of Everyday/Workspace and GPT-Live: config, Live client, Live loop and timing marks."""

import base64
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from homebody.config import AppConfig, load_config, merge_config, save_config
from homebody.hermes_client import AgentBrokerContext
from homebody.live_client import LiveBridgeSession, live_url
from homebody.realtime_client import RealtimeBridgeSession, RealtimeEvent
from homebody.voice_live import LiveVoiceMixin
from homebody.voice_timing import VoiceTimings


def context(profile="conversation"):
    return AgentBrokerContext(profile, profile == "agent", False, "awake", True, False, True, 7, 7, False)


def test_config_migration_preserves_engine_and_flags_routing_change(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"conversation_mode": "pipeline", "realtime_model": "gpt-4o-realtime-preview",
                                "api_key": "secret", "unknown_future_key": 1}))
    loaded = load_config(path)
    assert loaded.conversation_mode == "pipeline" and loaded.realtime_model == "gpt-4o-realtime-preview"
    assert loaded.assistant_mode == "everyday" and loaded.assistant_mode_notice == "migrated"
    save_config(merge_config(loaded, {"assistant_mode_notice": "", "conversation_mode": "live",
                                      "live_voice": "cedar", "realtime_semantic_eagerness": "high"}), path)
    saved = json.loads(path.read_text())
    assert (saved["conversation_mode"], saved["live_voice"], saved["assistant_mode_notice"]) == ("live", "cedar", "")
    assert load_config(path).assistant_mode_notice == ""  # acknowledged configs stay quiet
    public = load_config(path).redacted_dict()
    assert public["api_key"] == "********" and "secret" not in json.dumps(public)
    assert not any("location" in key or "latlon" in key for key in public)
    for bad in ({"assistant_mode": "full_hermes"}, {"live_voice": "alloy"}, {"live_session_timeout_seconds": 5},
                {"realtime_semantic_eagerness": "eager"}, {"conversation_mode": "telepathy"}):
        with pytest.raises(ValueError):
            AppConfig(**bad)


def test_realtime_session_mode_follows_agent_profile_only():
    config = AppConfig(api_key="k")
    assert RealtimeBridgeSession(config, agent_context=context()).assistant_mode == "everyday"
    assert RealtimeBridgeSession(config, agent_context=context("agent")).assistant_mode == "workspace"
    assert RealtimeBridgeSession(config).assistant_mode == "everyday"


def test_live_client_pcm_framing_and_gating():
    session = LiveBridgeSession(AppConfig(api_key="k", bridge_url="https://h.example/prefix"))
    assert live_url(session.config.bridge_url) == "wss://h.example/prefix/v1/live"
    pcm = LiveBridgeSession.pcm16(np.array([0.0, 1.0, -1.0, 2.0], dtype=np.float32))
    assert len(pcm) == 8 and np.frombuffer(pcm, "<i2").tolist() == [0, 32767, -32767, 32767]
    sent = []
    session._socket = SimpleNamespace(send=sent.append)
    session.send_audio(np.zeros(10, dtype=np.float32))
    assert sent == []  # nothing streams before session.started
    session.started.set()
    session.send_audio(np.zeros(10, dtype=np.float32))
    assert json.loads(sent[0])["type"] == "session.input_audio.append"
    samples = np.array([1000, -2000, 3000], dtype="<i2").tobytes()
    first = session.audio_samples(RealtimeEvent("x", {"delta": base64.b64encode(samples[:3]).decode()}))
    second = session.audio_samples(RealtimeEvent("x", {"delta": base64.b64encode(samples[3:]).decode()}))
    assert np.round(np.concatenate([first, second]) * 32768).astype(int).tolist() == [1000, -2000, 3000]


class FakeLiveSession:
    def __init__(self, script):
        self.script = list(script)
        self.sent = 0
        self.order = []

    def start(self):
        self.order.append("start")

    def wait_started(self):
        return []

    def send_audio(self, _samples):
        self.sent += 1

    def events(self):
        return self.script.pop(0) if self.script else []

    audio_samples = staticmethod(lambda event: np.ones(int(event.payload["n"]), dtype=np.float32))

    def close(self):
        self.order.append("close")


class LiveHarness(LiveVoiceMixin):
    def __init__(self, session, *, stop_after=None):
        self.session = session
        self._voice_timings = VoiceTimings()
        self._privacy_requested = threading.Event()
        self.stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status = SimpleNamespace(turns_completed=0)
        self._motion = None
        self._output_sample_rate = 24000
        self.pushed = []
        self.statuses = []
        self.workspace = []
        self.loops = 0
        self.stop_after = stop_after
        self.robot = SimpleNamespace(media=SimpleNamespace(push_audio_sample=lambda a: self.pushed.append(a.size)))
        self.finished = []

    def _new_live_session(self, *args, **kwargs):
        return self.session

    def agent_broker_context(self, **_):
        return context()

    def _effective_power_mode(self):
        return "awake"

    def _workspace_lease(self):
        return 1

    def _workspace_event(self, _generation, role, text):
        self.workspace.append((role, text))

    def _play_asset(self, _name):
        pass

    def _discard_audio(self, _seconds):
        pass

    def _set_status(self, state, detail="", **_):
        self.statuses.append(state)

    def _read_16k_frame(self):
        self.loops += 1
        return np.zeros(160, dtype=np.float32)

    def _turn_stop_requested(self):
        return self.stop_after is not None and self.loops >= self.stop_after

    def _clear_streamed_audio(self):
        self.session.order.append("clear_audio")

    def _finish_agent_request(self, *args, **kwargs):
        self.finished.append(args)


def audio(n):
    return RealtimeEvent("session.output_audio.delta", {"n": n})


def test_live_loop_plays_until_queue_drains_and_records_turn(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr("homebody.voice_live.time.monotonic", lambda: clock["t"])
    session = FakeLiveSession([
        [RealtimeEvent("session.input_transcript.delta", {"delta": "what time"}),
         RealtimeEvent("bridge.delegation", {"state": "started"}),
         RealtimeEvent("bridge.delegation", {"state": "answered"}),
         RealtimeEvent("session.output_transcript.delta", {"delta": "It is noon."}), audio(2400)],
        [], [], [], [RealtimeEvent("session.closed", {})],
    ])
    runtime = LiveHarness(session)
    original = runtime._read_16k_frame

    def advancing():
        clock["t"] += 0.06  # 2400 samples at 24 kHz = 0.1 s of queued speech
        return original()

    runtime._read_16k_frame = advancing
    runtime._voice_timings._clock = lambda: clock["t"]
    runtime._run_live_conversation(AppConfig(api_key="k", barge_in_enabled=True))
    assert runtime.pushed == [2400]
    assert ("assistant", "It is noon.") in runtime.workspace and ("user", "what time") in runtime.workspace
    assert runtime._status.turns_completed == 1
    assert session.order == ["start", "clear_audio", "close"]
    first = runtime._voice_timings.snapshot()[0]
    assert first["engine"] == "live" and first["mode"] == "everyday" and first["warm"] is False
    assert {"session_ready", "tool_requested", "tool_result", "first_provider_audio",
            "player_enqueued", "playback_estimated"} <= set(first["marks"])
    assert "text" not in json.dumps(runtime._voice_timings.snapshot())


def test_live_stop_flushes_before_close_and_bounds_queue_and_gates_mic(monkeypatch):
    monkeypatch.setattr("homebody.voice_live.time.monotonic", lambda: 50.0)
    session = FakeLiveSession([[audio(24000 * 20), audio(24000 * 20), audio(24000)]] + [[]] * 5)
    runtime = LiveHarness(session, stop_after=4)
    runtime._run_live_conversation(AppConfig(api_key="k", barge_in_enabled=False))
    assert runtime.pushed == [24000 * 20, 24000 * 20]  # beyond 30 s queued audio is dropped
    assert session.sent == 1  # mic is not streamed while Reachy is audible without barge-in
    assert session.order[-2:] == ["clear_audio", "close"]


@pytest.mark.parametrize(("config", "expected"), [
    ({"conversation_mode": "live"}, "live"),
    ({"conversation_mode": "realtime"}, "realtime"),
    ({"conversation_mode": "pipeline"}, "pipeline"),
    ({"conversation_mode": "live", "kids_mode_enabled": True, "kids_age_band": "7-9", "kids_activity": "buddy"},
     "pipeline"),  # Kids keeps its dedicated route ahead of every engine
])
def test_engine_routing_never_silently_falls_back(config, expected):
    from homebody.runtime import HermesVoiceRuntime

    calls = []
    fake = SimpleNamespace(
        _run_live_conversation=lambda c: calls.append("live"),
        _run_realtime_conversation=lambda c: calls.append("realtime"),
        _run_conversation=lambda c: calls.append("pipeline"),
    )
    HermesVoiceRuntime._run_selected_voice_conversation(fake, AppConfig(**config), "hey")
    assert calls == [expected]


def test_offline_timing_report_groups_cold_and_warm():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("report", Path("tools/voice_timing_report.py"))
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    summary = report.summarize([
        {"engine": "realtime", "mode": "everyday", "warm": False, "marks": {"first_provider_audio": 900}},
        {"engine": "realtime", "mode": "everyday", "warm": True, "marks": {"first_provider_audio": 300}},
        {"engine": "realtime", "mode": "everyday", "warm": True, "marks": {"first_provider_audio": 500}},
    ])
    assert summary["realtime/everyday/cold"]["first_provider_audio"] == {"n": 1, "p50_ms": 900, "p90_ms": 900}
    assert summary["realtime/everyday/warm"]["first_provider_audio"]["n"] == 2


def test_timing_ring_is_bounded_and_text_free():
    clock = {"t": 0.0}
    timings = VoiceTimings(limit=3, clock=lambda: clock["t"])
    for index in range(5):
        timings.begin("realtime", "everyday", warm=index > 0)
        clock["t"] += 0.25
        timings.mark("first_provider_audio")
        timings.mark("first_provider_audio")  # first mark wins
        timings.mark("transcript: secret")  # unknown marks are ignored
        timings.end()
    snapshot = timings.snapshot()
    assert len(snapshot) == 3 and snapshot[-1]["marks"]["first_provider_audio"] == 250
    assert all(set(turn) == {"engine", "mode", "warm", "marks"} for turn in snapshot)
