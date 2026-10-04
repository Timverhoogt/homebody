from __future__ import annotations

import asyncio
import io
import math
import threading
import time
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from aioesphomeapi import APIClient
from aioesphomeapi.model import CameraState, VoiceAssistantEventType
from PIL import Image

from homebody import home_assistant as home_assistant_module
from homebody.config import AppConfig
from homebody.home_assistant import (
    ENTITY_KEYS,
    DeviceIdentity,
    HermesHomeAssistantProvider,
    HomeAssistantStateProvider,
    default_device_identity,
    entity_specs,
    start_esphome_server,
)
from homebody.runtime import HermesVoiceRuntime


class FakeProvider(HomeAssistantStateProvider):
    def __init__(self) -> None:
        self.values: dict[str, object] = {
            "daemon_state": "running",
            "backend_ready": True,
            "speaker_volume": 42.0,
            "camera_disabled": True,
            "body_yaw": 0.0,
            "emotion": "Neutral",
            "error_message": "",
        }
        self.commands: list[tuple[str, object]] = []
        self.image: bytes | None = None

    def read(self, object_id: str) -> object | None:
        return self.values.get(object_id)

    def write(self, object_id: str, value: object) -> bool:
        self.commands.append((object_id, value))
        self.values[object_id] = value
        return True

    def camera_image(self) -> bytes | None:
        return self.image

    def media_command(self, url: str, *, announcement: bool) -> bool:
        self.commands.append(("announcement" if announcement else "media", url))
        return True

    def voice_event(self, event_type: int, data: dict[str, str]) -> None:
        self.commands.append(("voice_event", (event_type, data)))


def test_home_assistant_config_is_opt_in_and_voice_is_separate() -> None:
    config = AppConfig()

    assert config.home_assistant_enabled is False
    assert config.home_assistant_controls_enabled is False
    assert config.home_assistant_camera_enabled is False
    assert config.home_assistant_assist_enabled is False
    assert config.home_assistant_port == 6053


def test_home_assistant_live_bridge_refuses_non_rfc1918_bind(monkeypatch) -> None:
    monkeypatch.setattr(home_assistant_module, "_local_ipv4", lambda: "192.168.68.43")
    assert home_assistant_module._trusted_lan_ipv4() == "192.168.68.43"

    for address in ("0.0.0.0", "100.103.93.17", "203.0.113.4", ""):
        monkeypatch.setattr(home_assistant_module, "_local_ipv4", lambda address=address: address)
        with pytest.raises(RuntimeError, match="private bind address|determine a LAN"):
            home_assistant_module._trusted_lan_ipv4()


def test_default_identity_matches_existing_reachy_esphome_device(tmp_path: Path) -> None:
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("1643b6e79627abcdef\n", encoding="utf-8")

    identity = default_device_identity(machine_id_path=machine_id)

    assert identity.name == "Reachy Mini E79627"
    assert identity.node_name == "reachy-mini-e79627"
    assert identity.mac_address == "1643b6e79627"


def test_entity_contract_preserves_existing_keys_but_narrows_motion_limits() -> None:
    specs = {spec.object_id: spec for spec in entity_specs()}

    assert ENTITY_KEYS["body_yaw"] == 306
    assert ENTITY_KEYS["camera"] == 1001
    assert specs["body_yaw"].minimum == -120.0
    assert specs["body_yaw"].maximum == 120.0
    assert specs["head_pitch"].minimum == -25.0
    assert specs["head_pitch"].maximum == 25.0
    assert specs["look_at_x"].writable is False
    assert specs["imu_accel_x"].writable is False
    for object_id in (
        "daemon_state",
        "backend_ready",
        "speaker_volume",
        "awake",
        "camera_disabled",
        "head_x",
        "head_pitch",
        "head_yaw",
        "body_yaw",
        "doa_angle",
        "speech_detected",
        "control_loop_frequency",
        "sys_cpu_percent",
        "emotion",
        "camera",
        "reachy_mini_media_player",
    ):
        assert object_id in specs


def test_home_assistant_ui_exposes_nested_opt_ins_and_v44_assets() -> None:
    root = Path(__file__).resolve().parents[1]
    static = root / "homebody" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "main.js").read_text(encoding="utf-8")
    worker = (static / "service-worker.js").read_text(encoding="utf-8")

    for element_id in (
        "home_assistant_enabled",
        "home_assistant_controls_enabled",
        "home_assistant_camera_enabled",
        "home_assistant_assist_enabled",
        "home_assistant_port",
        "home-assistant-status",
    ):
        assert f'id="{element_id}"' in html
        assert element_id in script
    assert "dedicated Awake switch to run guarded wake/standby transitions" in html
    assert "toggleHomeAssistantOptions" in script
    assert "homebody-shell-v" in worker
    for asset in ("style.css", "camera.js", "main.js"):
        assert f"/static/{asset}?v=63" in html
        assert f'"/static/{asset}?v=63"' in worker


def test_runtime_provider_maps_native_daemon_telemetry_without_enabling_controls() -> None:
    class Runtime:
        control_ready = True
        robot = object()

        def status(self) -> dict[str, object]:
            return {"power_mode": "standby", "state": "waiting_for_wake_word", "last_error": ""}

        def robot_pose(self) -> dict[str, float]:
            return {"body_yaw": 0.0}

    provider = HermesHomeAssistantProvider(Runtime(), config_loader=lambda: AppConfig())
    payloads: dict[str, dict[str, object]] = {
        "/api/state/doa": {"angle": 0.5, "speech_detected": True},
        "/api/daemon/status": {"backend_status": {"control_loop_stats": {"mean_control_loop_frequency": 49.5}}},
        "/api/state/full": {"antennas_position": [-3.0, 3.0]},
    }
    provider._daemon_json = lambda path, **kwargs: payloads[path]  # type: ignore[method-assign]

    assert provider.read("doa_angle") == pytest.approx(math.degrees(0.5))
    assert provider.read("speech_detected") is True
    assert provider.read("control_loop_frequency") == 49.5
    assert provider.read("antenna_left") == pytest.approx(math.degrees(-3.0))
    assert provider.read("antenna_right") == pytest.approx(math.degrees(3.0))
    uptime = provider.read("sys_uptime")
    assert isinstance(uptime, (int, float)) and uptime > 0


def test_runtime_provider_rejects_remote_motion_until_locally_enabled_and_awake() -> None:
    class Runtime:
        control_ready = True
        mode = "standby"
        robot = object()
        actions: list[tuple[str, float]] = []

        def status(self) -> dict[str, object]:
            return {"power_mode": self.mode, "state": "idle", "last_error": ""}

        def robot_pose(self) -> dict[str, float]:
            return {"body_yaw": 5.0}

        def queue_precision_robot_action(self, axis: str, delta: float) -> None:
            self.actions.append((axis, delta))

    runtime = Runtime()
    saved: list[AppConfig] = []
    config = AppConfig()
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config, config_saver=saved.append)

    assert provider.write("body_yaw", 10.0) is False
    config = AppConfig(home_assistant_enabled=True, home_assistant_controls_enabled=True)
    assert provider.write("body_yaw", 10.0) is False
    assert runtime.actions == []

    runtime.mode = "awake"
    assert provider.write("body_yaw", 10.0) is True
    assert runtime.actions == [("body_yaw", 5.0)]
    assert provider.write("body_yaw", 40.0) is False
    assert runtime.actions == [("body_yaw", 5.0)]


def test_runtime_provider_exposes_gesture_switch_and_live_detection_state() -> None:
    class Runtime:
        control_ready = True
        robot = object()

        def status(self) -> dict[str, object]:
            return {
                "power_mode": "awake",
                "state": "idle",
                "last_error": "",
                "gesture_detected": "peace",
                "gesture_confidence": 0.91,
            }

    saved: list[AppConfig] = []
    provider = HermesHomeAssistantProvider(Runtime(), config_loader=AppConfig, config_saver=saved.append)

    assert provider.read("gesture_detection") is False
    assert provider.read("gesture_detected") == "peace"
    assert provider.read("gesture_confidence") == 91.0
    assert provider.write("gesture_detection", True) is False

    provider = HermesHomeAssistantProvider(
        Runtime(),
        config_loader=lambda: AppConfig(home_assistant_enabled=True, home_assistant_controls_enabled=True),
        config_saver=saved.append,
    )
    assert provider.write("gesture_detection", True) is True
    assert saved[-1].gesture_detection_enabled is True


def test_runtime_provider_awake_switch_uses_guarded_verified_power_transitions() -> None:
    class Runtime:
        control_ready = True
        robot = object()
        mode = "standby"
        calls: list[str] = []

        def status(self) -> dict[str, object]:
            return {
                "power_mode": self.mode,
                "state": "waiting_for_wake_word",
                "last_error": "",
                "motors_enabled": self.mode == "awake",
                "head_safely_folded": self.mode != "awake",
            }

        def set_power_mode(self, mode: str, *, duration_seconds: float = 0.0) -> dict[str, object]:
            del duration_seconds
            self.calls.append(mode)
            self.mode = mode
            return self.status()

    runtime = Runtime()
    disabled = HermesHomeAssistantProvider(runtime, config_loader=AppConfig)
    assert disabled.read("awake") is False
    assert disabled.write("awake", True) is False
    assert runtime.calls == []

    enabled = HermesHomeAssistantProvider(
        runtime,
        config_loader=lambda: AppConfig(home_assistant_enabled=True, home_assistant_controls_enabled=True),
    )
    assert enabled.write("awake", True) is True
    assert enabled.read("awake") is True
    assert runtime.calls == ["awake"]
    assert enabled.write("awake", False) is True
    assert enabled.read("awake") is False
    assert runtime.calls == ["awake", "standby"]


def test_runtime_provider_camera_is_local_opt_in_and_keeps_privacy_failures_unavailable() -> None:
    class Runtime:
        control_ready = True
        robot = object()

        def status(self) -> dict[str, object]:
            return {"power_mode": "awake", "state": "idle", "last_error": ""}

        def camera_snapshot(self) -> bytes:
            raise RuntimeError("Camera capture is blocked in the current privacy mode")

    config = AppConfig(home_assistant_enabled=True, home_assistant_camera_enabled=False)
    provider = HermesHomeAssistantProvider(Runtime(), config_loader=lambda: config)

    assert provider.camera_image() is None
    config = AppConfig(home_assistant_enabled=True, home_assistant_camera_enabled=True)
    assert provider.camera_image() is None
    assert "privacy mode" in str(provider.read("error_message"))


def test_runtime_provider_camera_normalizes_to_baseline_420_jpeg_when_opted_in() -> None:
    source = io.BytesIO()
    Image.new("RGB", (32, 24), (120, 80, 40)).save(source, format="JPEG", quality=95, subsampling=0)

    class Runtime:
        control_ready = True
        robot = object()
        image: object = source.getvalue()

        def status(self) -> dict[str, object]:
            return {"power_mode": "awake", "state": "idle", "last_error": ""}

        def camera_snapshot(self) -> object:
            return self.image

    runtime = Runtime()
    config = AppConfig(home_assistant_enabled=True, home_assistant_camera_enabled=True)
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config)

    normalized = provider.camera_image()
    assert normalized is not None
    assert normalized != runtime.image
    decoded = Image.open(io.BytesIO(normalized))
    decoded.load()
    assert decoded.format == "JPEG"
    assert decoded.mode == "RGB"
    assert decoded.size == (32, 24)
    assert [(layer[1], layer[2]) for layer in decoded.layer] == [(2, 2), (1, 1), (1, 1)]
    runtime.image = b"not-jpeg"
    assert provider.camera_image() is None
    assert "invalid JPEG" in str(provider.read("error_message"))
    runtime.image = b"\xff\xd8" + b"x" * 1_000_000 + b"\xff\xd9"
    assert provider.camera_image() is None
    assert "1 MB" in str(provider.read("error_message"))


def test_assist_event_state_waits_for_tts_playback_after_run_end() -> None:
    class Runtime:
        robot = object()

        def status(self) -> dict[str, object]:
            return {"power_mode": "awake", "state": "idle", "last_error": ""}

    config = AppConfig(home_assistant_enabled=True, home_assistant_assist_enabled=True)
    provider = HermesHomeAssistantProvider(Runtime(), config_loader=lambda: config)
    provider.begin_voice()
    provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START), {})
    assert provider.voice_snapshot()["streaming"] is True

    provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END), {})
    assert provider.voice_snapshot()["stage"] == "thinking"
    provider.voice_event(
        int(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END),
        {"url": "http://192.168.68.34:8123/api/tts_proxy/test.wav"},
    )
    provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END), {})
    snapshot = provider.voice_snapshot(consume_tts_url=True)
    assert snapshot["tts_url"] == "http://192.168.68.34:8123/api/tts_proxy/test.wav"
    assert snapshot["done"] is False
    provider.voice_playback_finished()
    assert provider.voice_snapshot()["done"] is True


def test_assist_media_url_must_resolve_to_connected_home_assistant(monkeypatch) -> None:
    class Runtime:
        robot = object()

        def status(self) -> dict[str, object]:
            return {"power_mode": "awake", "state": "idle", "last_error": ""}

    provider = HermesHomeAssistantProvider(Runtime(), config_loader=lambda: AppConfig())
    monkeypatch.setattr(
        "homebody.home_assistant.socket.getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("192.168.68.34" if host == "homeassistant.local" else "192.168.68.99", port))
        ],
    )
    provider.ha_connected("192.168.68.34")
    provider.ha_connected("192.168.68.27")
    provider.ha_disconnected("192.168.68.27")
    assert provider.validate_media_url("http://homeassistant.local:8123/api/tts.wav").endswith("tts.wav")

    try:
        provider.validate_media_url("http://evil.local/payload.wav")
    except ValueError as exc:
        assert "connected HA peer" in str(exc)
    else:
        raise AssertionError("unrelated media host was accepted")

    provider.ha_disconnected("192.168.68.34")
    try:
        provider.validate_media_url("http://homeassistant.local:8123/api/tts.wav")
    except RuntimeError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("media URL was accepted after the last HA peer disconnected")


def test_runtime_assist_turn_streams_pcm_plays_tts_and_finishes() -> None:
    class Media:
        pass

    class Robot:
        media = Media()

    config = AppConfig(
        home_assistant_enabled=True,
        home_assistant_assist_enabled=True,
        conversation_timeout_seconds=30.0,
    )
    runtime = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    runtime._power_mode = "awake"
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config)

    class Bridge:
        connected = True

        def __init__(self) -> None:
            self.provider = provider
            self.started: list[tuple[str, str]] = []
            self.audio: list[bytes] = []
            self.finished = 0
            self.stopped = 0

        def start_voice(self, *, wake_word_phrase: str, conversation_id: str) -> bool:
            self.started.append((wake_word_phrase, conversation_id))
            provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START), {})
            return True

        def send_voice_audio(self, data: bytes) -> bool:
            self.audio.append(data)
            provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END), {})
            provider.voice_event(
                int(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END),
                {"url": "http://192.168.68.34:8123/api/tts_proxy/test.wav"},
            )
            provider.voice_event(int(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END), {})
            return True

        def stop_voice(self) -> bool:
            self.stopped += 1
            return True

        def voice_announcement_finished(self) -> bool:
            self.finished += 1
            return True

        def status(self) -> dict[str, object]:
            return {"enabled": True, "ready": True, "connected": True, "error": ""}

    bridge = Bridge()
    runtime._home_assistant_bridge = bridge  # type: ignore[assignment]
    runtime._read_16k_frame = lambda: np.full(320, 0.25, dtype=np.float32)  # type: ignore[method-assign]
    played: list[str] = []
    runtime._play_home_assistant_media = lambda active_provider, url: played.append(url)  # type: ignore[method-assign]

    runtime._run_home_assistant_conversation(config, "Hey Hermes")

    assert bridge.started and bridge.started[0][0] == "Hey Hermes"
    assert len(bridge.audio) == 1
    assert len(bridge.audio[0]) == 640
    assert played == ["http://192.168.68.34:8123/api/tts_proxy/test.wav"]
    assert bridge.finished == 1
    assert runtime.status()["turns_completed"] == 1


def test_runtime_stops_active_assist_pipeline_on_cancellation() -> None:
    class Robot:
        media = object()

    config = AppConfig(home_assistant_enabled=True, home_assistant_assist_enabled=True)
    runtime = HermesVoiceRuntime(Robot(), threading.Event(), config_loader=lambda: config)
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config)
    provider.begin_voice()

    class Bridge:
        connected = True

        def __init__(self) -> None:
            self.provider = provider
            self.stopped = 0

        def stop_voice(self) -> bool:
            self.stopped += 1
            return True

    bridge = Bridge()
    runtime._home_assistant_bridge = bridge  # type: ignore[assignment]
    runtime._stop_home_assistant_voice_if_active()

    assert bridge.stopped == 1
    snapshot = provider.voice_snapshot()
    assert snapshot["active"] is False
    assert snapshot["done"] is True
    assert "cancelled" in str(snapshot["error"])


def test_real_aioesphome_client_sees_compatible_device_entities_states_and_camera() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider.image = b"\xff\xd8" + b"camera-round-trip" * 6_000 + b"\xff\xd9"
        running = await start_esphome_server(
            provider,
            host="127.0.0.1",
            port=0,
            advertise=False,
            assist_enabled=False,
            identity=DeviceIdentity("Reachy Mini E79627", "reachy-mini-e79627", "1643b6e79627"),
        )
        client = APIClient("127.0.0.1", running.port, None, client_info="homebody-test")
        states: list[object] = []
        try:
            await client.connect(login=True)
            info = await client.device_info()
            assert info.name == "Reachy Mini E79627"
            assert info.mac_address == "1643b6e79627"
            assert info.manufacturer == "Tim Verhoogt / Homebody"
            assert info.model == "Homebody"
            assert info.voice_assistant_feature_flags == 0

            entities, services = await client.list_entities_services()
            assert services == []
            by_object_id = {entity.object_id: entity for entity in entities}
            assert by_object_id["body_yaw"].key == 306
            assert by_object_id["camera"].key == 1001
            assert by_object_id["body_yaw"].min_value == -120.0
            assert by_object_id["body_yaw"].max_value == 120.0

            client.subscribe_states(states.append)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                getattr(state, "key", None) == 103 and getattr(state, "state", None) == 42.0 for state in states
            ):
                await asyncio.sleep(0.02)
            assert any(
                getattr(state, "key", None) == 101 and getattr(state, "state", None) is True for state in states
            )
            assert any(
                getattr(state, "key", None) == 103 and getattr(state, "state", None) == 42.0 for state in states
            )

            client.request_single_image()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(isinstance(state, CameraState) for state in states):
                await asyncio.sleep(0.02)
            camera = cast(CameraState, next(state for state in states if isinstance(state, CameraState)))
            assert camera.key == ENTITY_KEYS["camera"]
            assert camera.data == provider.image

            client.number_command(306, 15.0)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and ("body_yaw", 15.0) not in provider.commands:
                await asyncio.sleep(0.02)
            assert ("body_yaw", 15.0) in provider.commands
        finally:
            try:
                await client.disconnect(force=True)
            except Exception:
                pass
            await running.close()

    asyncio.run(scenario())


def test_slow_camera_capture_does_not_block_protocol_requests() -> None:
    async def scenario() -> None:
        class SlowCameraProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.image = b"\xff\xd8slow-camera\xff\xd9"
                self.started = threading.Event()
                self.release = threading.Event()

            def camera_image(self) -> bytes | None:
                self.started.set()
                if not self.release.wait(2.0):
                    raise RuntimeError("test camera release timed out")
                return self.image

        provider = SlowCameraProvider()
        running = await start_esphome_server(
            provider,
            host="127.0.0.1",
            port=0,
            advertise=False,
            assist_enabled=False,
            identity=DeviceIdentity("Reachy Mini E79627", "reachy-mini-e79627", "1643b6e79627"),
        )
        client = APIClient("127.0.0.1", running.port, None, client_info="homebody-nonblocking-test")
        states: list[object] = []
        try:
            await client.connect(login=True)
            client.subscribe_states(states.append)
            client.request_single_image()
            assert await asyncio.to_thread(provider.started.wait, 1.0)

            info = await asyncio.wait_for(client.device_info(), timeout=0.25)
            assert info.name == "Reachy Mini E79627"

            provider.release.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(isinstance(state, CameraState) for state in states):
                await asyncio.sleep(0.02)
            camera = cast(CameraState, next(state for state in states if isinstance(state, CameraState)))
            assert camera.data == provider.image
        finally:
            provider.release.set()
            try:
                await client.disconnect(force=True)
            except Exception:
                pass
            await running.close()

    asyncio.run(scenario())


def test_real_aioesphome_client_round_trips_assist_start_pcm_events_and_stop() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        running = await start_esphome_server(
            provider,
            host="127.0.0.1",
            port=0,
            advertise=False,
            assist_enabled=True,
            identity=DeviceIdentity("Reachy Mini E79627", "reachy-mini-e79627", "1643b6e79627"),
        )
        client = APIClient("127.0.0.1", running.port, None, client_info="homebody-assist-test")
        started = asyncio.Event()
        audio_received = asyncio.Event()
        stopped = asyncio.Event()
        start_payload: dict[str, object] = {}
        audio_payload: list[bytes] = []

        async def handle_start(
            conversation_id: str,
            flags: int,
            audio_settings: object,
            wake_word_phrase: str | None,
        ) -> int | None:
            start_payload.update(
                conversation_id=conversation_id,
                flags=flags,
                audio_settings=audio_settings,
                wake_word_phrase=wake_word_phrase,
            )
            started.set()
            return None

        async def handle_stop(abort: bool) -> None:
            start_payload["abort"] = abort
            stopped.set()

        async def handle_audio(data: bytes, end: bytes | None) -> None:
            del end
            audio_payload.append(data)
            audio_received.set()

        unsubscribe = None
        try:
            await client.connect(login=True)
            info = await client.device_info()
            assert info.voice_assistant_feature_flags != 0
            unsubscribe = client.subscribe_voice_assistant(
                handle_start=handle_start,
                handle_stop=handle_stop,
                handle_audio=handle_audio,
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not running.protocols:
                await asyncio.sleep(0.01)
            protocol = next(iter(running.protocols))
            assert protocol.start_voice(wake_word_phrase="Hey Hermes", conversation_id="conversation-1") is True
            await asyncio.wait_for(started.wait(), 2.0)
            assert start_payload["conversation_id"] == "conversation-1"
            assert start_payload["wake_word_phrase"] == "Hey Hermes"

            pcm = b"\x01\x02" * 320
            assert protocol.send_voice_audio(pcm) is True
            await asyncio.wait_for(audio_received.wait(), 2.0)
            assert audio_payload == [pcm]

            client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not provider.commands:
                await asyncio.sleep(0.01)
            assert provider.commands[-1][0] == "voice_event"

            assert protocol.stop_voice() is True
            await asyncio.wait_for(stopped.wait(), 2.0)
        finally:
            if unsubscribe is not None:
                unsubscribe()
            try:
                await client.disconnect(force=True)
            except Exception:
                pass
            await running.close()

    asyncio.run(scenario())


def _varuint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def test_esphome_server_drops_peer_declaring_an_oversized_packet() -> None:
    async def scenario() -> None:
        running = await start_esphome_server(FakeProvider(), host="127.0.0.1", port=0, advertise=False)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", running.port)
            # Preamble, a 64 MiB declared length, message type 1, and a few payload bytes.
            writer.write(b"\x00" + _varuint(64 * 1024 * 1024) + _varuint(1) + b"x" * 16)
            await writer.drain()
            closed = await asyncio.wait_for(reader.read(), timeout=2.0)
            assert closed == b""
            writer.close()
        finally:
            await running.close()

    asyncio.run(scenario())


def test_esphome_state_publisher_survives_a_failing_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def flaky_publish(self) -> None:  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("config.json is not valid JSON")

    monkeypatch.setattr(home_assistant_module.ESPHomeProtocol, "publish_states", flaky_publish)

    async def scenario() -> None:
        running = await start_esphome_server(FakeProvider(), host="127.0.0.1", port=0, advertise=False)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", running.port)
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(calls) < 2:
                await asyncio.sleep(0.05)
            assert len(calls) >= 2
            assert not running._publisher.done()
            writer.close()
        finally:
            await running.close()

    asyncio.run(scenario())


def test_esphome_server_close_tolerates_a_crashed_publisher() -> None:
    async def scenario() -> None:
        running = await start_esphome_server(FakeProvider(), host="127.0.0.1", port=0, advertise=False)
        running._publisher.cancel()

        async def crashed() -> None:
            raise RuntimeError("publisher crashed")

        failed = asyncio.create_task(crashed())
        await asyncio.sleep(0)
        running._publisher = failed
        await running.close()
        assert not running.server.is_serving()

    asyncio.run(scenario())
