from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "homebody" / "static"


def test_robot_tab_contains_explicit_local_live_camera_controls() -> None:
    html = (STATIC / "index.html").read_text()

    assert 'id="reachy-camera-video"' in html
    assert 'class="camera-vision-overlay"' in html
    assert 'id="camera-vision-timecode"' in html
    assert "HERMES VISION" in html
    assert "LOCAL OPTIC" in html
    assert "PRIVATE LINK" in html
    assert 'id="camera-live-start"' in html
    assert 'id="camera-live-stop"' in html
    assert 'id="camera-live-fullscreen"' in html
    assert 'id="camera_feed_enabled"' in html
    assert "never sent to Hermes or OpenAI" in html
    assert html.index('/static/gstwebrtc-api.js') < html.index('/static/camera.js')
    assert html.index('/static/camera.js') < html.index('/static/main.js')


def test_camera_viewer_uses_private_webrtc_without_public_stun_or_audio() -> None:
    camera = (STATIC / "camera.js").read_text()

    assert 'window.location.protocol === "https:" ? "wss" : "ws"' in camera
    assert 'signalingServerUrl: `${signalingScheme}://${window.location.hostname}:8443`' in camera
    assert "webrtcConfig: { iceServers: [] }" in camera
    assert "stun:" not in camera
    assert "track.enabled = false" in camera
    assert 'state.powerMode !== "awake"' in camera
    assert 'window.addEventListener("pagehide"' in camera
    assert 'document.addEventListener("visibilitychange"' in camera
    assert 'state.api._channel?.close' in camera
    assert 'name === "reachymini"' in camera
    assert 'producer?.id !== state.producerId' in camera
    assert "No frames are sent to Hermes or OpenAI" in camera
    assert "startVisionOverlay()" in camera
    assert "stopVisionOverlay()" in camera
    assert 'byId("camera-vision-timecode")' in camera


def test_camera_vision_overlay_is_visual_only_and_non_interactive() -> None:
    style = (STATIC / "style.css").read_text()

    assert ".camera-vision-overlay" in style
    assert "pointer-events: none" in style
    assert ".camera-viewer.live .camera-vision-overlay" in style
    assert ".vision-reticle" in style
    assert ".vision-scanline" in style


def test_live_camera_is_opt_in_and_stops_for_privacy_transitions() -> None:
    main = (STATIC / "main.js").read_text()
    config = (ROOT / "homebody" / "config.py").read_text()
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
    bundled_api = (STATIC / "gstwebrtc-api.js").read_text()[:1000]

    assert "camera_feed_enabled: bool = False" in config
    assert "camera_controls_enabled: bool = False" in config
    assert "enabled: !kidsActive && !kidsLocked && Boolean(payload.config?.camera_feed_enabled)" in main
    assert 'mode !== "awake" && window.ReachyCamera?.isActive()' in main
    assert "Camera stopped because Hermes status is unavailable" in main
    assert "Camera stopped before stopping the voice app" in main
    assert "Camera stopped when leaving the Robot tab" in main
    assert "MPL-2.0" in bundled_api
    assert "BSD 3-Clause" in bundled_api
    assert "Mozilla Public License 2.0" in notices
    assert "Redistribution and use in source and binary forms" in notices
    assert 'THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"' in notices
