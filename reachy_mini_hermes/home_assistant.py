"""Optional ESPHome-native API bridge for Home Assistant.

The wire framing and entity key contract are compatible with the Apache-2.0
``djhui5710/reachy_mini_home_assistant`` app so an existing Home Assistant
ESPHome device can reconnect without duplicate entities. Robot commands are
handled by :class:`HomeAssistantStateProvider`; this module never talks to
motors directly.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import io
import ipaddress
import logging
import math
import os
import shutil
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from aioesphomeapi import api_pb2
from aioesphomeapi.api_options_pb2 import id as api_message_id
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    VoiceAssistantEventType,
    VoiceAssistantFeature,
)
from google.protobuf.message import Message
from PIL import Image, UnidentifiedImageError
from zeroconf import ServiceInfo, Zeroconf

from .config import AppConfig, load_config, merge_config, save_config

_LOGGER = logging.getLogger(__name__)
_HOME_ASSISTANT_CAMERA_MAX_PIXELS = 1920 * 1080
_PRIVATE_LAN_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

try:
    _AIOESPHOMEAPI_VERSION = importlib.metadata.version("aioesphomeapi")
except importlib.metadata.PackageNotFoundError:
    _AIOESPHOMEAPI_VERSION = "unknown"

try:
    _APP_VERSION = importlib.metadata.version("reachy_mini_hermes")
except importlib.metadata.PackageNotFoundError:
    _APP_VERSION = "0.3.0"


def _normalize_camera_jpeg_for_home_assistant(jpeg: bytes) -> bytes:
    """Return a metadata-free baseline 4:2:0 JPEG for broad HA client compatibility."""
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            if image.format != "JPEG":
                raise RuntimeError("Reachy camera returned an invalid JPEG")
            width, height = image.size
            if width < 1 or height < 1 or width * height > _HOME_ASSISTANT_CAMERA_MAX_PIXELS:
                raise RuntimeError("Reachy camera JPEG dimensions exceed the Home Assistant limit")
            image.load()
            output = io.BytesIO()
            image.convert("RGB").save(
                output,
                format="JPEG",
                quality=85,
                subsampling="4:2:0",
                progressive=False,
                optimize=False,
            )
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as exc:
        raise RuntimeError("Reachy camera returned an invalid JPEG") from exc
    normalized = output.getvalue()
    if len(normalized) > 1_000_000:
        raise RuntimeError("Home Assistant camera JPEG exceeds the 1 MB limit")
    return normalized


ENTITY_KEYS: dict[str, int] = {
    "daemon_state": 100,
    "backend_ready": 101,
    "mute": 102,
    "speaker_volume": 103,
    "idle": 104,
    "sendspin_enabled": 105,
    "face_tracking": 106,
    "gesture_detection": 107,
    "face_confidence_threshold": 108,
    "camera_disabled": 109,
    "doa_tracking": 110,
    "continuous_conversation": 111,
    "awake": 112,
    "doa_angle": 200,
    "speech_detected": 201,
    "control_loop_frequency": 202,
    "imu_accel_x": 203,
    "imu_accel_y": 204,
    "imu_accel_z": 205,
    "imu_gyro_x": 206,
    "imu_gyro_y": 207,
    "imu_gyro_z": 208,
    "imu_temperature": 209,
    "gesture_detected": 210,
    "gesture_confidence": 211,
    "face_detected": 212,
    "sys_cpu_percent": 220,
    "sys_cpu_temperature": 221,
    "sys_memory_percent": 222,
    "sys_memory_used": 223,
    "sys_disk_percent": 224,
    "sys_disk_free": 225,
    "sys_uptime": 226,
    "sys_process_cpu": 227,
    "sys_process_memory": 228,
    "sdk_version": 229,
    "robot_name": 230,
    "wireless_version": 231,
    "simulation_mode": 232,
    "wlan_ip": 233,
    "error_message": 234,
    "head_x": 300,
    "head_y": 301,
    "head_z": 302,
    "head_roll": 303,
    "head_pitch": 304,
    "head_yaw": 305,
    "body_yaw": 306,
    "antenna_left": 307,
    "antenna_right": 308,
    "emotion": 400,
    "look_at_x": 500,
    "look_at_y": 501,
    "look_at_z": 502,
    "reachy_mini_media_player": 1000,
    "camera": 1001,
}


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    name: str
    node_name: str
    mac_address: str


def default_device_identity(*, machine_id_path: Path = Path("/etc/machine-id")) -> DeviceIdentity:
    """Derive the same stable identity as the existing Reachy HA app."""
    try:
        raw = machine_id_path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        raw = ""
    hexadecimal = "".join(char for char in raw if char in "0123456789abcdef")
    mac = (hexadecimal[:12] or "000000000000").ljust(12, "0")
    suffix = mac[-6:].upper()
    return DeviceIdentity(
        name=f"Reachy Mini {suffix}",
        node_name=f"reachy-mini-{suffix.lower()}",
        mac_address=mac,
    )


@dataclass(frozen=True, slots=True)
class EntitySpec:
    kind: str
    object_id: str
    name: str
    icon: str = ""
    unit: str = ""
    accuracy: int = 1
    device_class: str = ""
    state_class: int = 0
    entity_category: int = 0
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    mode: int = 0
    options: tuple[str, ...] = ()
    writable: bool = False

    @property
    def key(self) -> int:
        return ENTITY_KEYS[self.object_id]


def entity_specs() -> tuple[EntitySpec, ...]:
    """Return the stable HA entity contract.

    Unsupported hardware/vision values are still listed and publish
    ``missing_state``. This preserves entity registry continuity without
    fabricating measurements.
    """
    specs: list[EntitySpec] = [
        EntitySpec("text", "daemon_state", "Daemon State", "mdi:server"),
        EntitySpec("binary", "backend_ready", "Backend Ready", "mdi:check-network", device_class="connectivity"),
        EntitySpec("switch", "mute", "Mute", "mdi:microphone-off", writable=True),
        EntitySpec("switch", "awake", "Awake", "mdi:power", writable=True),
        EntitySpec(
            "number",
            "speaker_volume",
            "Speaker Volume",
            "mdi:volume-high",
            "%",
            0,
            minimum=0,
            maximum=100,
            step=1,
            mode=2,
            writable=True,
        ),
        EntitySpec("switch", "idle", "Idle Movement", "mdi:robot-happy", writable=True),
        EntitySpec("switch", "sendspin_enabled", "Sendspin", "mdi:cast-audio", writable=True),
        EntitySpec("switch", "face_tracking", "Face Tracking", "mdi:face-recognition", writable=True),
        EntitySpec("switch", "gesture_detection", "Gesture Detection", "mdi:hand-wave", writable=True),
        EntitySpec(
            "number",
            "face_confidence_threshold",
            "Face Confidence Threshold",
            "mdi:gauge",
            minimum=0,
            maximum=1,
            step=0.05,
            mode=2,
            writable=True,
        ),
        EntitySpec("switch", "camera_disabled", "Disable Camera", "mdi:camera-off", writable=True),
        EntitySpec("switch", "doa_tracking", "DOA Tracking", "mdi:surround-sound", writable=True),
        EntitySpec("switch", "continuous_conversation", "Continuous Conversation", "mdi:account-voice", writable=True),
        EntitySpec("sensor", "doa_angle", "DOA Angle", "mdi:angle-acute", "°", 1, state_class=1),
        EntitySpec("binary", "speech_detected", "Speech Detected", "mdi:account-voice"),
        EntitySpec(
            "sensor",
            "control_loop_frequency",
            "Control Loop Frequency",
            "mdi:speedometer",
            "Hz",
            1,
            state_class=1,
            entity_category=2,
        ),
    ]
    for prefix, label, unit, icon in (
        ("imu_accel", "IMU Accel", "m/s²", "mdi:axis-arrow"),
        ("imu_gyro", "IMU Gyro", "rad/s", "mdi:rotate-3d-variant"),
    ):
        for axis in "xyz":
            specs.append(
                EntitySpec("sensor", f"{prefix}_{axis}", f"{label} {axis.upper()}", icon, unit, 3, state_class=1)
            )
    specs.extend(
        [
            EntitySpec(
                "sensor",
                "imu_temperature",
                "IMU Temperature",
                "mdi:thermometer",
                "°C",
                1,
                device_class="temperature",
                state_class=1,
            ),
            EntitySpec("text", "gesture_detected", "Gesture Detected", "mdi:hand-wave"),
            EntitySpec("sensor", "gesture_confidence", "Gesture Confidence", "mdi:gauge", "%", 1, state_class=1),
            EntitySpec("binary", "face_detected", "Face Detected", "mdi:face-recognition"),
        ]
    )
    diagnostic = (
        ("sys_cpu_percent", "System CPU Usage", "%", 1, "mdi:cpu-64-bit"),
        ("sys_cpu_temperature", "CPU Temperature", "°C", 1, "mdi:thermometer"),
        ("sys_memory_percent", "System Memory Usage", "%", 1, "mdi:memory"),
        ("sys_memory_used", "System Memory Used", "GB", 2, "mdi:memory"),
        ("sys_disk_percent", "System Disk Usage", "%", 1, "mdi:harddisk"),
        ("sys_disk_free", "System Disk Free", "GB", 1, "mdi:harddisk"),
        ("sys_uptime", "System Uptime", "h", 1, "mdi:clock-outline"),
        ("sys_process_cpu", "App CPU Usage", "%", 1, "mdi:application-cog"),
        ("sys_process_memory", "App Memory Usage", "MB", 1, "mdi:application-cog"),
    )
    specs.extend(
        EntitySpec("sensor", oid, name, icon, unit, accuracy, state_class=1, entity_category=2)
        for oid, name, unit, accuracy, icon in diagnostic
    )
    specs.extend(
        [
            EntitySpec("text", "sdk_version", "SDK Version", "mdi:information", entity_category=2),
            EntitySpec("text", "robot_name", "Robot Name", "mdi:robot", entity_category=2),
            EntitySpec(
                "binary",
                "wireless_version",
                "Wireless Version",
                "mdi:wifi",
                device_class="connectivity",
                entity_category=2,
            ),
            EntitySpec("binary", "simulation_mode", "Simulation Mode", "mdi:virtual-reality", entity_category=2),
            EntitySpec("text", "wlan_ip", "WLAN IP", "mdi:ip-network", entity_category=2),
            EntitySpec("text", "error_message", "Error Message", "mdi:alert-circle", entity_category=2),
        ]
    )
    pose = (
        ("head_x", "Head X Position", "mm", -15, 15),
        ("head_y", "Head Y Position", "mm", -15, 15),
        ("head_z", "Head Z Position", "mm", -10, 30),
        ("head_roll", "Head Roll", "°", -25, 25),
        ("head_pitch", "Head Pitch", "°", -25, 25),
        ("head_yaw", "Head Yaw", "°", -35, 35),
        ("body_yaw", "Body Yaw", "°", -120, 120),
        ("antenna_left", "Antenna(L)", "°", -45, 45),
        ("antenna_right", "Antenna(R)", "°", -45, 45),
    )
    specs.extend(
        EntitySpec(
            "number",
            oid,
            name,
            "mdi:rotate-3d-variant",
            unit,
            1,
            minimum=minimum,
            maximum=maximum,
            step=1,
            mode=2,
            writable=True,
        )
        for oid, name, unit, minimum, maximum in pose
    )
    specs.append(
        EntitySpec(
            "select",
            "emotion",
            "Emotion",
            "mdi:emoticon-happy-outline",
            options=("Neutral", "Happy", "Sad", "Surprised", "Thinking", "Confused"),
            writable=True,
        )
    )
    for axis in "xyz":
        specs.append(
            EntitySpec(
                "number",
                f"look_at_{axis}",
                f"Look At {axis.upper()}",
                "mdi:crosshairs-gps",
                "m",
                1,
                minimum=-2,
                maximum=2,
                step=0.1,
                mode=1,
                writable=False,
            )
        )
    specs.extend(
        [
            EntitySpec("media_player", "reachy_mini_media_player", "Reachy Mini Media Player"),
            EntitySpec("camera", "camera", "Camera", "mdi:camera"),
        ]
    )
    return tuple(specs)


class HomeAssistantStateProvider:
    """Thread-safe adapter implemented by the Hermes runtime."""

    def read(self, object_id: str) -> object | None:
        return None

    def write(self, object_id: str, value: object) -> bool:
        return False

    def camera_image(self) -> bytes | None:
        return None

    def media_command(self, url: str, *, announcement: bool) -> bool:
        return False

    def media_transport(self, command: str, value: float | None = None) -> bool:
        return False

    def ha_connected(self, peer_host: str) -> None:
        return None

    def ha_disconnected(self, peer_host: str) -> None:
        del peer_host
        return None

    def voice_event(self, event_type: int, data: dict[str, str]) -> None:
        return None

    def voice_announcement(
        self, media_id: str, *, preannounce_media_id: str = "", start_conversation: bool = False
    ) -> bool:
        urls = [url for url in (preannounce_media_id, media_id) if url]
        return all(self.media_command(url, announcement=True) for url in urls)


@dataclass(slots=True)
class _ConnectionState:
    subscribed: bool = False


class ESPHomeProtocol(asyncio.Protocol):
    """Small plaintext ESPHome native API server for one HA connection."""

    def __init__(
        self,
        provider: HomeAssistantStateProvider,
        *,
        identity: DeviceIdentity,
        assist_enabled: bool,
        on_close: Any = None,
    ) -> None:
        self.provider = provider
        self.identity = identity
        self.assist_enabled = assist_enabled
        self.transport: asyncio.Transport | None = None
        self._buffer = bytearray()
        self._connection = _ConnectionState()
        self._on_close = on_close
        self._peer_host = ""
        self._provider_lock = asyncio.Lock()
        self._provider_tasks: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        peer = transport.get_extra_info("peername")
        self._peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
        if self._peer_host:
            self.provider.ha_connected(self._peer_host)
        _LOGGER.info("Home Assistant ESPHome client connected from %s", peer)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        for task in tuple(self._provider_tasks):
            task.cancel()
        self._provider_tasks.clear()
        if self._peer_host:
            self.provider.ha_disconnected(self._peer_host)
            self._peer_host = ""
        if self._on_close:
            self._on_close(self)
        _LOGGER.info("Home Assistant ESPHome client disconnected%s", f": {exc}" if exc else "")

    def data_received(self, data: bytes) -> None:
        self._buffer.extend(data)
        while self._buffer:
            if self._buffer[0] != 0:
                _LOGGER.warning("Dropping malformed ESPHome connection: invalid preamble")
                if self.transport:
                    self.transport.close()
                return
            try:
                packet = _read_packet(self._buffer)
            except ValueError as exc:
                _LOGGER.warning("Dropping malformed ESPHome connection: %s", exc)
                self._buffer.clear()
                if self.transport:
                    self.transport.close()
                return
            if packet is None:
                return
            payload, msg_type = packet
            try:
                proto_type = MESSAGE_TYPE_TO_PROTO[msg_type]
                msg = proto_type.FromString(payload)
                if self._message_needs_provider(msg):
                    task = asyncio.create_task(self._handle_provider_message(msg, msg_type))
                    self._provider_tasks.add(task)
                    task.add_done_callback(self._provider_tasks.discard)
                else:
                    self.send_messages(self.handle_message(msg))
            except Exception:
                _LOGGER.exception("Failed to process ESPHome message type %s", msg_type)
                if self.transport:
                    self.transport.close()
                return

    @staticmethod
    def _message_needs_provider(msg: Message) -> bool:
        return isinstance(
            msg,
            (
                api_pb2.SubscribeStatesRequest,
                api_pb2.SubscribeHomeAssistantStatesRequest,
                api_pb2.NumberCommandRequest,
                api_pb2.SwitchCommandRequest,
                api_pb2.SelectCommandRequest,
                api_pb2.CameraImageRequest,
                api_pb2.MediaPlayerCommandRequest,
                api_pb2.VoiceAssistantAnnounceRequest,
            ),
        )

    async def _handle_provider_message(self, msg: Message, msg_type: int) -> None:
        try:
            async with self._provider_lock:
                messages = await asyncio.to_thread(lambda: list(self.handle_message(msg)))
            self.send_messages(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Failed to process ESPHome provider message type %s", msg_type)
            if self.transport:
                self.transport.close()

    def send_messages(self, messages: Iterable[Message]) -> None:
        if self.transport is None:
            return
        packets: list[bytes] = []
        for msg in messages:
            msg_type = msg.DESCRIPTOR.GetOptions().Extensions[api_message_id]
            payload = msg.SerializeToString()
            packets.append(b"\0" + _varuint_to_bytes(len(payload)) + _varuint_to_bytes(msg_type) + payload)
        if packets:
            self.transport.writelines(packets)

    def handle_message(self, msg: Message) -> Iterable[Message]:
        if isinstance(msg, api_pb2.HelloRequest):
            yield api_pb2.HelloResponse(
                api_version_major=1,
                api_version_minor=10,
                server_info=f"Reachy Mini Hermes ({_AIOESPHOMEAPI_VERSION})",
                name=self.identity.name,
            )
            return
        if isinstance(msg, api_pb2.AuthenticationRequest):
            yield api_pb2.AuthenticationResponse(invalid_password=False)
            return
        if isinstance(msg, api_pb2.PingRequest):
            yield api_pb2.PingResponse()
            return
        if isinstance(msg, api_pb2.DisconnectRequest):
            yield api_pb2.DisconnectResponse()
            return
        if isinstance(msg, api_pb2.DeviceInfoRequest):
            features = 0
            if self.assist_enabled:
                features = int(
                    VoiceAssistantFeature.VOICE_ASSISTANT
                    | VoiceAssistantFeature.API_AUDIO
                    | VoiceAssistantFeature.ANNOUNCE
                    | VoiceAssistantFeature.START_CONVERSATION
                    | VoiceAssistantFeature.TIMERS
                )
            yield api_pb2.DeviceInfoResponse(
                uses_password=False,
                name=self.identity.name,
                friendly_name=self.identity.name,
                project_name="Timverhoogt.reachy-mini-hermes",
                project_version=_APP_VERSION,
                esphome_version=_AIOESPHOMEAPI_VERSION,
                mac_address=self.identity.mac_address,
                manufacturer="Tim Verhoogt / Hermes Agent",
                model="Reachy Mini Hermes",
                voice_assistant_feature_flags=features,
            )
            return
        if isinstance(msg, api_pb2.ListEntitiesRequest):
            for spec in entity_specs():
                yield _list_entity_message(spec)
            yield api_pb2.ListEntitiesDoneResponse()
            return
        if isinstance(msg, (api_pb2.SubscribeStatesRequest, api_pb2.SubscribeHomeAssistantStatesRequest)):
            self._connection.subscribed = True
            yield from self.state_messages()
            return
        if isinstance(msg, api_pb2.NumberCommandRequest):
            spec = _spec_by_key(msg.key, "number")
            if spec and spec.writable:
                value = max(spec.minimum, min(spec.maximum, float(msg.state)))
                self.provider.write(spec.object_id, value)
                yield _state_message(spec, self.provider.read(spec.object_id))
            return
        if isinstance(msg, api_pb2.SwitchCommandRequest):
            spec = _spec_by_key(msg.key, "switch")
            if spec and spec.writable:
                self.provider.write(spec.object_id, bool(msg.state))
                yield _state_message(spec, self.provider.read(spec.object_id))
            return
        if isinstance(msg, api_pb2.SelectCommandRequest):
            spec = _spec_by_key(msg.key, "select")
            if spec and spec.writable and msg.state in spec.options:
                self.provider.write(spec.object_id, msg.state)
                yield _state_message(spec, self.provider.read(spec.object_id))
            return
        if isinstance(msg, api_pb2.CameraImageRequest):
            data = self.provider.camera_image() or b""
            if not data:
                yield api_pb2.CameraImageResponse(key=ENTITY_KEYS["camera"], data=b"", done=True)
                return
            chunk_size = 32 * 1024
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset : offset + chunk_size]
                yield api_pb2.CameraImageResponse(
                    key=ENTITY_KEYS["camera"],
                    data=chunk,
                    done=offset + len(chunk) >= len(data),
                )
            return
        if isinstance(msg, api_pb2.MediaPlayerCommandRequest) and msg.key == ENTITY_KEYS["reachy_mini_media_player"]:
            if msg.has_media_url:
                self.provider.media_command(
                    msg.media_url, announcement=bool(msg.has_announcement and msg.announcement)
                )
            elif msg.has_command:
                self.provider.media_transport(str(int(msg.command)))
            elif msg.has_volume:
                self.provider.media_transport("volume", float(msg.volume))
            yield _state_message(
                _spec_by_id("reachy_mini_media_player"), self.provider.read("reachy_mini_media_player")
            )
            return
        if self.assist_enabled and isinstance(msg, api_pb2.VoiceAssistantConfigurationRequest):
            yield api_pb2.VoiceAssistantConfigurationResponse(
                available_wake_words=[
                    api_pb2.VoiceAssistantWakeWord(id="hey_hermes", wake_word="Hey Hermes", trained_languages=["en"])
                ],
                active_wake_words=["hey_hermes"],
                max_active_wake_words=1,
            )
            return
        if self.assist_enabled and isinstance(msg, api_pb2.VoiceAssistantEventResponse):
            self.provider.voice_event(int(msg.event_type), {item.name: item.value for item in msg.data})
            return
        if self.assist_enabled and isinstance(msg, api_pb2.VoiceAssistantAnnounceRequest):
            self.provider.voice_announcement(
                msg.media_id,
                preannounce_media_id=msg.preannounce_media_id,
                start_conversation=msg.start_conversation,
            )
            return

    def state_messages(self) -> Iterable[Message]:
        for spec in entity_specs():
            if spec.kind not in {"camera"}:
                yield _state_message(spec, self.provider.read(spec.object_id))

    async def publish_states(self) -> None:
        if not self._connection.subscribed:
            return
        async with self._provider_lock:
            messages = await asyncio.to_thread(lambda: list(self.state_messages()))
        self.send_messages(messages)

    def start_voice(self, *, wake_word_phrase: str = "Hey Hermes", conversation_id: str = "") -> bool:
        if not self.assist_enabled or self.transport is None:
            return False
        request = api_pb2.VoiceAssistantRequest(start=True, wake_word_phrase=wake_word_phrase)
        if conversation_id:
            request.conversation_id = conversation_id
        self.send_messages([request])
        return True

    def send_voice_audio(self, data: bytes) -> bool:
        if not self.assist_enabled or self.transport is None:
            return False
        self.send_messages([api_pb2.VoiceAssistantAudio(data=data)])
        return True

    def stop_voice(self) -> bool:
        if not self.assist_enabled or self.transport is None:
            return False
        self.send_messages([api_pb2.VoiceAssistantRequest(start=False)])
        return True

    def voice_announcement_finished(self) -> bool:
        if not self.assist_enabled or self.transport is None:
            return False
        self.send_messages([api_pb2.VoiceAssistantAnnounceFinished()])
        return True


@dataclass(slots=True)
class RunningESPHomeServer:
    server: asyncio.AbstractServer
    protocols: set[ESPHomeProtocol]
    port: int
    _publisher: asyncio.Task[None]

    async def close(self) -> None:
        self._publisher.cancel()
        try:
            await self._publisher
        except asyncio.CancelledError:
            pass
        except Exception:
            # A crashed publisher must not stop the sockets and server from closing.
            _LOGGER.exception("Home Assistant state publisher had failed before shutdown")
        for protocol in tuple(self.protocols):
            if protocol.transport:
                protocol.transport.close()
        self.server.close()
        await self.server.wait_closed()


async def start_esphome_server(
    provider: HomeAssistantStateProvider,
    *,
    host: str = "0.0.0.0",
    port: int = 6053,
    advertise: bool = True,
    assist_enabled: bool = False,
    identity: DeviceIdentity | None = None,
) -> RunningESPHomeServer:
    """Start the compatible native API server on the current event loop."""
    del advertise  # mDNS is owned by the threaded bridge; tests use direct TCP.
    identity = identity or default_device_identity()
    protocols: set[ESPHomeProtocol] = set()

    def remove(protocol: ESPHomeProtocol) -> None:
        protocols.discard(protocol)

    def factory() -> ESPHomeProtocol:
        protocol = ESPHomeProtocol(provider, identity=identity, assist_enabled=assist_enabled, on_close=remove)
        protocols.add(protocol)
        return protocol

    loop = asyncio.get_running_loop()
    server = await loop.create_server(factory, host=host, port=port, reuse_address=True)
    sockets = server.sockets or []
    if not sockets:
        server.close()
        raise RuntimeError("ESPHome server did not bind a socket")
    bound_port = int(sockets[0].getsockname()[1])

    async def publish() -> None:
        while True:
            await asyncio.sleep(1.0)
            # One failing provider read (for example a hand-edited config) must not end
            # state publishing for every connected Home Assistant client.
            results = await asyncio.gather(
                *(protocol.publish_states() for protocol in tuple(protocols)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    _LOGGER.warning("Home Assistant state publish failed: %s", result)

    publisher = asyncio.create_task(publish(), name="reachy-hermes-ha-state-publisher")
    return RunningESPHomeServer(server=server, protocols=protocols, port=bound_port, _publisher=publisher)


class HermesHomeAssistantProvider(HomeAssistantStateProvider):
    """Map HA entities onto Hermes state and guarded runtime operations."""

    _CONFIG_SWITCHES: ClassVar[dict[str, str]] = {
        "idle": "motion_enabled",
        "face_tracking": "face_tracking_enabled",
        "gesture_detection": "gesture_detection_enabled",
        "doa_tracking": "doa_enabled",
        "continuous_conversation": "continuous_conversation",
    }
    _POSE_AXES: ClassVar[dict[str, str]] = {
        "head_x": "x",
        "head_y": "y",
        "head_z": "z",
        "head_roll": "roll",
        "head_pitch": "pitch",
        "head_yaw": "yaw",
        "body_yaw": "body_yaw",
    }

    def __init__(
        self,
        runtime: Any,
        *,
        config_loader: Any = load_config,
        config_saver: Any = save_config,
    ) -> None:
        self.runtime = runtime
        self.config_loader = config_loader
        self.config_saver = config_saver
        self._cache_lock = threading.RLock()
        self._pose_cache: dict[str, float] = {}
        self._pose_cached_at = 0.0
        self._last_error = ""
        self._started_at = time.monotonic()
        self._last_cpu_total: tuple[float, float] | None = None
        self._last_process_cpu: tuple[float, float] | None = None
        self._http_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._emotion = "Neutral"
        self._ha_peers: set[str] = set()
        self._voice_condition = threading.Condition(threading.RLock())
        self._voice_active = False
        self._voice_streaming = False
        self._voice_run_ended = False
        self._voice_playback_complete = False
        self._voice_done = False
        self._voice_stage = "idle"
        self._voice_tts_url = ""
        self._voice_continue = False
        self._voice_error = ""

    def ha_connected(self, peer_host: str) -> None:
        with self._voice_condition:
            self._ha_peers.add(peer_host)
            self._voice_condition.notify_all()

    def ha_disconnected(self, peer_host: str) -> None:
        with self._voice_condition:
            self._ha_peers.discard(peer_host)
            if self._ha_peers:
                self._voice_condition.notify_all()
                return
            self._voice_active = False
            self._voice_streaming = False
            self._voice_done = True
            self._voice_error = "Home Assistant disconnected"
            self._voice_condition.notify_all()

    def begin_voice(self) -> None:
        with self._voice_condition:
            self._voice_active = True
            self._voice_streaming = False
            self._voice_run_ended = False
            self._voice_playback_complete = False
            self._voice_done = False
            self._voice_stage = "starting"
            self._voice_tts_url = ""
            self._voice_continue = False
            self._voice_error = ""

    def voice_snapshot(self, *, consume_tts_url: bool = False) -> dict[str, object]:
        with self._voice_condition:
            payload: dict[str, object] = {
                "active": self._voice_active,
                "streaming": self._voice_streaming,
                "done": self._voice_done,
                "stage": self._voice_stage,
                "tts_url": self._voice_tts_url,
                "continue_conversation": self._voice_continue,
                "error": self._voice_error,
            }
            if consume_tts_url:
                self._voice_tts_url = ""
            return payload

    def wait_voice_update(self, timeout: float = 0.1) -> dict[str, object]:
        with self._voice_condition:
            self._voice_condition.wait(timeout)
        return self.voice_snapshot(consume_tts_url=True)

    def voice_playback_finished(self) -> None:
        with self._voice_condition:
            self._voice_playback_complete = True
            if self._voice_run_ended:
                self._voice_done = True
                self._voice_active = False
                self._voice_stage = "idle"
            self._voice_condition.notify_all()

    def cancel_voice(self, reason: str = "Home Assistant Assist was cancelled") -> None:
        with self._voice_condition:
            self._voice_active = False
            self._voice_streaming = False
            self._voice_done = True
            self._voice_stage = "idle"
            self._voice_error = reason
            self._voice_condition.notify_all()

    def voice_event(self, event_type: int, data: dict[str, str]) -> None:
        try:
            event = VoiceAssistantEventType(event_type)
        except ValueError:
            return
        with self._voice_condition:
            if event == VoiceAssistantEventType.VOICE_ASSISTANT_ERROR:
                self._voice_error = data.get("message") or data.get("code") or "Home Assistant Assist failed"
                self._voice_active = False
                self._voice_streaming = False
                self._voice_done = True
            elif event == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START:
                self._voice_active = True
                self._voice_streaming = True
                self._voice_stage = "listening"
            elif event in {
                VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                VoiceAssistantEventType.VOICE_ASSISTANT_STT_END,
            }:
                self._voice_streaming = False
                self._voice_stage = "thinking"
            elif event == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END:
                self._voice_continue = data.get("continue_conversation") == "1"
            elif event == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START:
                self._voice_stage = "speaking"
            elif event == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END:
                self._voice_tts_url = data.get("url", "")
                self._voice_playback_complete = False
                self._voice_stage = "speaking"
            elif event == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END:
                self._voice_streaming = False
                self._voice_run_ended = True
                if self._voice_playback_complete or (not self._voice_tts_url and self._voice_stage != "speaking"):
                    self._voice_active = False
                    self._voice_done = True
                    self._voice_stage = "idle"
            self._voice_condition.notify_all()

    def validate_media_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Home Assistant media URL must be an unauthenticated http(s) URL")
        with self._voice_condition:
            peers = set(self._ha_peers)
        if not peers:
            raise RuntimeError("Home Assistant is not connected")
        try:
            resolved = {
                item[4][0]
                for item in socket.getaddrinfo(
                    parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
                )
            }
        except OSError as exc:
            raise ValueError(f"Home Assistant media host could not be resolved: {exc}") from exc
        if peers.isdisjoint(resolved):
            raise ValueError("Home Assistant media URL does not resolve to a connected HA peer")
        return url

    def _config(self) -> AppConfig:
        return self.config_loader()

    def _update_config(self, changes: dict[str, object]) -> AppConfig:
        current = self._config()
        updated = merge_config(current, changes)
        self.config_saver(updated)
        return updated

    def _runtime_status(self) -> dict[str, object]:
        try:
            value = self.runtime.status()
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            self._last_error = str(exc)
            return {}

    def _daemon_json(self, path: str, *, ttl: float = 0.4) -> dict[str, object]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._http_cache.get(path)
            if cached is not None and now - cached[0] < ttl:
                return dict(cached[1])
        try:
            import httpx

            response = httpx.get(f"http://127.0.0.1:8000{path}", timeout=1.0)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("daemon response is not an object")
            clean = {str(key): value for key, value in payload.items()}
            with self._cache_lock:
                self._http_cache[path] = (now, clean)
            return dict(clean)
        except Exception as exc:
            self._last_error = str(exc)
            return {}

    def _pose(self) -> dict[str, float]:
        now = time.monotonic()
        with self._cache_lock:
            if self._pose_cache and now - self._pose_cached_at < 0.4:
                return dict(self._pose_cache)
        try:
            pose = self.runtime.robot_pose()
            clean = {str(key): float(value) for key, value in pose.items() if isinstance(value, (int, float))}
            if not all(math.isfinite(value) for value in clean.values()):
                raise ValueError("pose contains non-finite values")
            with self._cache_lock:
                self._pose_cache = clean
                self._pose_cached_at = now
            return dict(clean)
        except Exception as exc:
            self._last_error = str(exc)
            return {}

    @staticmethod
    def _read_number(path: Path) -> float | None:
        try:
            return float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _system_value(self, object_id: str) -> float | None:
        if object_id == "sys_uptime":
            try:
                uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            except (OSError, ValueError, IndexError):
                return None
            return uptime / 3600.0
        if object_id in {"sys_disk_percent", "sys_disk_free"}:
            usage = shutil.disk_usage("/")
            if object_id == "sys_disk_free":
                return usage.free / (1024**3)
            return 100.0 * usage.used / usage.total if usage.total else None
        if object_id == "sys_cpu_temperature":
            raw = self._read_number(Path("/sys/class/thermal/thermal_zone0/temp"))
            return None if raw is None else raw / 1000.0
        if object_id in {"sys_memory_percent", "sys_memory_used"}:
            values: dict[str, float] = {}
            try:
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                    key, raw = line.split(":", 1)
                    values[key] = float(raw.strip().split()[0]) * 1024.0
            except (OSError, ValueError, IndexError):
                return None
            total = values.get("MemTotal", 0.0)
            available = values.get("MemAvailable", 0.0)
            used = max(0.0, total - available)
            if object_id == "sys_memory_used":
                return used / (1024**3)
            return 100.0 * used / total if total else None
        if object_id == "sys_process_memory":
            try:
                for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
            except (OSError, ValueError, IndexError):
                return None
        if object_id == "sys_process_cpu":
            try:
                fields = Path("/proc/self/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
                process_ticks = float(fields[11]) + float(fields[12])
                clock_ticks = float(os.sysconf("SC_CLK_TCK"))
                now = time.monotonic()
            except (OSError, ValueError, IndexError):
                return None
            with self._cache_lock:
                previous = self._last_process_cpu
                self._last_process_cpu = (now, process_ticks)
            if previous is None or now <= previous[0]:
                return None
            return 100.0 * ((process_ticks - previous[1]) / clock_ticks) / (now - previous[0])
        if object_id == "sys_cpu_percent":
            try:
                raw = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
                ticks = [float(item) for item in raw]
            except (OSError, ValueError, IndexError):
                return None
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0.0)
            total = sum(ticks)
            with self._cache_lock:
                previous = self._last_cpu_total
                self._last_cpu_total = (total, idle)
            if previous is None or total <= previous[0]:
                return None
            return 100.0 * (1.0 - (idle - previous[1]) / (total - previous[0]))
        return None

    def _volume(self) -> float | None:
        try:
            import httpx

            response = httpx.get("http://127.0.0.1:8000/api/volume/current", timeout=1.0)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("volume", "current", "value"):
                    if key in payload:
                        return float(payload[key])
            return float(payload)
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def read(self, object_id: str) -> object | None:
        config = self._config()
        status = self._runtime_status()
        if object_id == "daemon_state":
            return (
                "running"
                if bool(getattr(self.runtime, "control_ready", False))
                else str(status.get("state") or "starting")
            )
        if object_id == "backend_ready":
            return bool(getattr(self.runtime, "control_ready", False))
        if object_id == "mute":
            return str(status.get("power_mode") or "") in {"meeting", "sleep"}
        if object_id == "awake":
            return str(status.get("power_mode") or "") == "awake"
        if object_id == "speaker_volume":
            return self._volume()
        if object_id in self._CONFIG_SWITCHES:
            return bool(getattr(config, self._CONFIG_SWITCHES[object_id]))
        if object_id == "sendspin_enabled":
            return None
        if object_id == "face_confidence_threshold":
            return None
        if object_id == "camera_disabled":
            return not config.home_assistant_camera_enabled
        if object_id in {"doa_angle", "speech_detected"}:
            doa = self._daemon_json("/api/state/doa")
            if object_id == "speech_detected":
                return bool(doa.get("speech_detected")) if "speech_detected" in doa else None
            angle = doa.get("angle")
            return math.degrees(float(angle)) if isinstance(angle, (int, float)) else None
        if object_id == "control_loop_frequency":
            daemon = self._daemon_json("/api/daemon/status", ttl=1.0)
            backend = daemon.get("backend_status")
            stats = backend.get("control_loop_stats") if isinstance(backend, dict) else None
            frequency = stats.get("mean_control_loop_frequency") if isinstance(stats, dict) else None
            return float(frequency) if isinstance(frequency, (int, float)) else None
        if object_id == "gesture_detected":
            return str(status.get("gesture_detected") or "none")
        if object_id == "gesture_confidence":
            return float(status.get("gesture_confidence") or 0.0) * 100.0
        if object_id.startswith("imu_") or object_id == "face_detected":
            return None
        if object_id.startswith("sys_"):
            return self._system_value(object_id)
        if object_id == "sdk_version":
            try:
                return importlib.metadata.version("reachy-mini")
            except importlib.metadata.PackageNotFoundError:
                return None
        if object_id == "robot_name":
            return default_device_identity().name
        if object_id == "wireless_version":
            return bool(getattr(self.runtime.robot, "wireless_version", True))
        if object_id == "simulation_mode":
            return bool(getattr(self.runtime.robot, "simulated", False))
        if object_id == "wlan_ip":
            return _local_ipv4()
        if object_id == "error_message":
            return str(status.get("last_error") or self._last_error)
        if object_id in self._POSE_AXES:
            return self._pose().get(self._POSE_AXES[object_id])
        if object_id in {"antenna_left", "antenna_right"}:
            full_state = self._daemon_json("/api/state/full")
            positions = full_state.get("antennas_position")
            index = 0 if object_id == "antenna_left" else 1
            if isinstance(positions, list) and len(positions) > index and isinstance(positions[index], (int, float)):
                return math.degrees(float(positions[index]))
            return None
        if object_id == "emotion":
            return self._emotion
        if object_id.startswith("look_at_"):
            return None
        if object_id == "reachy_mini_media_player":
            return {"state": int(MediaPlayerState.IDLE), "volume": (self._volume() or 100.0) / 100.0, "muted": False}
        return None

    def write(self, object_id: str, value: object) -> bool:
        try:
            config = self._config()
            if object_id == "mute":
                if bool(value):
                    self.runtime.set_power_mode("meeting", duration_seconds=3600.0)
                    return True
                return False
            if object_id == "awake":
                if not config.home_assistant_controls_enabled:
                    return False
                target = "awake" if bool(value) else "standby"
                result = self.runtime.set_power_mode(target, duration_seconds=0.0)
                if target == "awake":
                    return (
                        result.get("power_mode") == "awake"
                        and result.get("motors_enabled") is True
                        and result.get("head_safely_folded") is False
                    )
                return (
                    result.get("power_mode") == "standby"
                    and result.get("motors_enabled") is False
                    and result.get("head_safely_folded") is True
                )
            if object_id == "speaker_volume":
                import httpx

                volume = max(0.0, min(100.0, float(value)))
                response = httpx.post("http://127.0.0.1:8000/api/volume/set", params={"volume": volume}, timeout=2.0)
                response.raise_for_status()
                return True
            if object_id == "gesture_detection" and not config.home_assistant_controls_enabled:
                return False
            if object_id in self._CONFIG_SWITCHES:
                self._update_config({self._CONFIG_SWITCHES[object_id]: bool(value)})
                if object_id == "face_tracking":
                    self.runtime._set_face_tracking(bool(value), weight=config.face_tracking_weight)
                return True
            if object_id == "camera_disabled":
                if bool(value):
                    self._update_config({"home_assistant_camera_enabled": False})
                    return True
                return bool(config.home_assistant_camera_enabled)
            if object_id in {"sendspin_enabled", "face_confidence_threshold"}:
                return False
            if object_id in self._POSE_AXES:
                if not config.home_assistant_controls_enabled:
                    return False
                status = self._runtime_status()
                if status.get("power_mode") != "awake":
                    return False
                axis = self._POSE_AXES[object_id]
                current = self._pose().get(axis)
                if current is None:
                    return False
                delta = float(value) - current
                if not math.isfinite(delta) or abs(delta) < 0.01 or abs(delta) > 10.0:
                    return False
                self.runtime.queue_precision_robot_action(axis, delta)
                with self._cache_lock:
                    self._pose_cached_at = 0.0
                return True
            if object_id in {"antenna_left", "antenna_right"} or object_id.startswith("look_at_"):
                return False
            if object_id == "emotion":
                if not config.home_assistant_controls_enabled or self._runtime_status().get("power_mode") != "awake":
                    return False
                mapping = {
                    "Happy": "happy",
                    "Sad": "sad",
                    "Surprised": "surprised",
                    "Thinking": "thinking",
                    "Confused": "confused",
                }
                if str(value) == "Neutral":
                    return True
                emotion = mapping.get(str(value))
                if not emotion:
                    return False
                self.runtime.queue_manual_robot_action("emotion", emotion)
                self._emotion = str(value)
                return True
            return False
        except Exception as exc:
            self._last_error = str(exc)
            _LOGGER.warning("Rejected Home Assistant command %s: %s", object_id, exc)
            return False

    def camera_image(self) -> bytes | None:
        try:
            if not self._config().home_assistant_camera_enabled:
                return None
            image = self.runtime.camera_snapshot()
            if not isinstance(image, (bytes, bytearray, memoryview)):
                raise RuntimeError("Reachy camera returned an unsupported payload")
            jpeg = bytes(image)
            if len(jpeg) > 1_000_000:
                raise RuntimeError("Home Assistant camera JPEG exceeds the 1 MB limit")
            if len(jpeg) < 4 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise RuntimeError("Reachy camera returned an invalid JPEG")
            return _normalize_camera_jpeg_for_home_assistant(jpeg)
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def media_command(self, url: str, *, announcement: bool) -> bool:
        try:
            handler = getattr(self.runtime, "queue_home_assistant_media", None)
            if not callable(handler):
                return False
            handler(url, announcement=announcement)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def voice_announcement(
        self,
        media_id: str,
        *,
        preannounce_media_id: str = "",
        start_conversation: bool = False,
    ) -> bool:
        del start_conversation
        try:
            urls = [url for url in (preannounce_media_id, media_id) if url]
            if not urls:
                return False
            handler = getattr(self.runtime, "queue_home_assistant_media", None)
            if not callable(handler):
                return False
            handler(urls, announcement=True)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False


class HomeAssistantBridge:
    """Own the ESPHome TCP server and mDNS advertisement in a daemon thread."""

    def __init__(
        self, provider: HomeAssistantStateProvider, *, config: AppConfig, identity: DeviceIdentity | None = None
    ) -> None:
        self.provider = provider
        self.config = config
        self.identity = identity or default_device_identity()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running: RunningESPHomeServer | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error = ""
        self._zeroconf: Zeroconf | None = None
        self._service: ServiceInfo | None = None
        self._bind_address = ""

    @property
    def connected(self) -> bool:
        running = self._running
        return bool(running and running.protocols)

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "ready": self._ready.is_set() and not self._error,
            "connected": self.connected,
            "assist_enabled": self.config.home_assistant_assist_enabled,
            "camera_enabled": self.config.home_assistant_camera_enabled,
            "controls_enabled": self.config.home_assistant_controls_enabled,
            "device_name": self.identity.name,
            "port": self.config.home_assistant_port,
            "bind_address": self._bind_address,
            "error": self._error,
        }

    def start(self, *, timeout: float = 10.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="reachy-hermes-home-assistant", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Home Assistant bridge did not start in time")
        if self._error:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._bind_address = _trusted_lan_ipv4()
            self._running = loop.run_until_complete(
                start_esphome_server(
                    self.provider,
                    host=self._bind_address,
                    port=self.config.home_assistant_port,
                    advertise=False,
                    assist_enabled=self.config.home_assistant_assist_enabled,
                    identity=self.identity,
                )
            )
            self._register_mdns()
            self._ready.set()
            loop.run_forever()
        except Exception as exc:
            self._error = f"Home Assistant bridge failed: {exc}"
            _LOGGER.exception(self._error)
            self._ready.set()
        finally:
            self._unregister_mdns()
            if self._running is not None:
                loop.run_until_complete(self._running.close())
                self._running = None
            loop.close()
            self._closed.set()

    def _register_mdns(self) -> None:
        address = self._bind_address
        if not address:
            _LOGGER.warning("Home Assistant mDNS advertisement skipped: no LAN IPv4 address")
            return
        service_type = "_esphomelib._tcp.local."
        self._service = ServiceInfo(
            service_type,
            f"{self.identity.node_name}.{service_type}",
            addresses=[socket.inet_aton(address)],
            port=self.config.home_assistant_port,
            properties={
                "friendly_name": self.identity.name,
                "mac": self.identity.mac_address,
                "platform": "Linux",
                "board": "reachy-mini",
                "network": "wifi",
                "version": _AIOESPHOMEAPI_VERSION,
            },
            server=f"{self.identity.node_name}.local.",
        )
        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._service)

    def _unregister_mdns(self) -> None:
        if self._zeroconf is not None:
            try:
                if self._service is not None:
                    self._zeroconf.unregister_service(self._service)
            finally:
                self._zeroconf.close()
        self._zeroconf = None
        self._service = None

    def close(self, *, timeout: float = 10.0) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError("Home Assistant bridge did not stop in time")

    def start_voice(self, *, wake_word_phrase: str = "Hey Hermes", conversation_id: str = "") -> bool:
        running, loop = self._running, self._loop
        if running is None or loop is None or not self.connected:
            return False
        result: list[bool] = []
        complete = threading.Event()

        def send() -> None:
            result.append(
                any(
                    protocol.start_voice(wake_word_phrase=wake_word_phrase, conversation_id=conversation_id)
                    for protocol in tuple(running.protocols)
                )
            )
            complete.set()

        loop.call_soon_threadsafe(send)
        return complete.wait(2.0) and bool(result and result[0])

    def send_voice_audio(self, data: bytes) -> bool:
        running, loop = self._running, self._loop
        if running is None or loop is None or not self.connected:
            return False
        loop.call_soon_threadsafe(lambda: [protocol.send_voice_audio(data) for protocol in tuple(running.protocols)])
        return True

    def stop_voice(self) -> bool:
        running, loop = self._running, self._loop
        if running is None or loop is None or not self.connected:
            return False
        loop.call_soon_threadsafe(lambda: [protocol.stop_voice() for protocol in tuple(running.protocols)])
        return True

    def voice_announcement_finished(self) -> bool:
        running, loop = self._running, self._loop
        if running is None or loop is None or not self.connected:
            return False
        loop.call_soon_threadsafe(
            lambda: [protocol.voice_announcement_finished() for protocol in tuple(running.protocols)]
        )
        return True


def _trusted_lan_ipv4() -> str:
    address = _local_ipv4()
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise RuntimeError("Home Assistant bridge could not determine a LAN IPv4 address") from exc
    if not isinstance(parsed, ipaddress.IPv4Address) or not any(
        parsed in network for network in _PRIVATE_LAN_NETWORKS
    ):
        raise RuntimeError(f"Home Assistant bridge refuses non-private bind address {address or '<none>'}")
    return address


def _local_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return str(sock.getsockname()[0])
    except OSError:
        return ""


def _varuint_to_bytes(value: int) -> bytes:
    """Encode one ESPHome unsigned varint without relying on private client APIs."""
    if value < 0:
        raise ValueError("ESPHome varuint cannot be negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


_MAX_INBOUND_PACKET_BYTES = 1024 * 1024


def _read_varuint(data: bytearray, offset: int) -> tuple[int, int] | None:
    result = 0
    bitpos = 0
    while offset < len(data):
        value = data[offset]
        offset += 1
        result |= (value & 0x7F) << bitpos
        if not value & 0x80:
            return result, offset
        bitpos += 7
        if bitpos >= 64:
            raise ValueError("invalid ESPHome varuint")
    return None


def _read_packet(buffer: bytearray) -> tuple[bytes, int] | None:
    length_result = _read_varuint(buffer, 1)
    if length_result is None:
        return None
    length, offset = length_result
    if length > _MAX_INBOUND_PACKET_BYTES:
        # The plaintext API accepts any LAN peer, so never buffer an unbounded declared length.
        raise ValueError(f"ESPHome packet of {length} bytes exceeds the inbound limit")
    type_result = _read_varuint(buffer, offset)
    if type_result is None:
        return None
    msg_type, offset = type_result
    end = offset + length
    if len(buffer) < end:
        return None
    payload = bytes(buffer[offset:end])
    del buffer[:end]
    return payload, msg_type


def _spec_by_key(key: int, kind: str) -> EntitySpec | None:
    return next((spec for spec in entity_specs() if spec.key == key and spec.kind == kind), None)


def _spec_by_id(object_id: str) -> EntitySpec:
    return next(spec for spec in entity_specs() if spec.object_id == object_id)


def _list_entity_message(spec: EntitySpec) -> Message:
    common = dict(
        object_id=spec.object_id, key=spec.key, name=spec.name, icon=spec.icon, entity_category=spec.entity_category
    )
    if spec.kind == "sensor":
        return api_pb2.ListEntitiesSensorResponse(
            **common,
            unit_of_measurement=spec.unit,
            accuracy_decimals=spec.accuracy,
            device_class=spec.device_class,
            state_class=spec.state_class,
        )
    if spec.kind == "binary":
        return api_pb2.ListEntitiesBinarySensorResponse(**common, device_class=spec.device_class)
    if spec.kind == "text":
        return api_pb2.ListEntitiesTextSensorResponse(**common)
    if spec.kind == "switch":
        return api_pb2.ListEntitiesSwitchResponse(**common, device_class=spec.device_class)
    if spec.kind == "number":
        return api_pb2.ListEntitiesNumberResponse(
            **common,
            min_value=spec.minimum,
            max_value=spec.maximum,
            step=spec.step,
            unit_of_measurement=spec.unit,
            mode=spec.mode,
        )
    if spec.kind == "select":
        return api_pb2.ListEntitiesSelectResponse(**common, options=spec.options)
    if spec.kind == "camera":
        return api_pb2.ListEntitiesCameraResponse(**common)
    if spec.kind == "media_player":
        features = int(
            MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
        )
        return api_pb2.ListEntitiesMediaPlayerResponse(**common, supports_pause=True, feature_flags=features)
    raise ValueError(f"Unsupported ESPHome entity kind: {spec.kind}")


def _state_message(spec: EntitySpec, value: object | None) -> Message:
    missing = value is None
    if spec.kind == "sensor":
        return api_pb2.SensorStateResponse(key=spec.key, state=0.0 if missing else float(value), missing_state=missing)
    if spec.kind == "binary":
        return api_pb2.BinarySensorStateResponse(
            key=spec.key, state=False if missing else bool(value), missing_state=missing
        )
    if spec.kind == "text":
        return api_pb2.TextSensorStateResponse(
            key=spec.key, state="" if missing else str(value), missing_state=missing
        )
    if spec.kind == "switch":
        return api_pb2.SwitchStateResponse(key=spec.key, state=False if missing else bool(value))
    if spec.kind == "number":
        return api_pb2.NumberStateResponse(
            key=spec.key, state=spec.minimum if missing else float(value), missing_state=missing
        )
    if spec.kind == "select":
        return api_pb2.SelectStateResponse(
            key=spec.key, state=spec.options[0] if missing else str(value), missing_state=missing
        )
    if spec.kind == "media_player":
        if isinstance(value, dict):
            state = int(value.get("state", MediaPlayerState.IDLE))
            volume = float(value.get("volume", 1.0))
            muted = bool(value.get("muted", False))
        else:
            state, volume, muted = int(MediaPlayerState.IDLE), 1.0, False
        return api_pb2.MediaPlayerStateResponse(key=spec.key, state=state, volume=volume, muted=muted)
    raise ValueError(f"Entity kind has no state message: {spec.kind}")
