from types import SimpleNamespace

import pytest
from test_owner_auth import ORIGIN, pair
from test_owner_routes import application as owner_application

from homebody.config import AppConfig

application = owner_application


def setup_live(application, monkeypatch, capture=lambda: b"\xff\xd8test\xff\xd9"):
    import homebody.owner_media as media

    robot, client, code = application
    calls = []
    robot._runtime = SimpleNamespace(
        control_ready=True,
        kids_controls_locked=False,
        status=lambda: {"power_mode": "awake", "kids_mode": {}},
        robot=SimpleNamespace(media=SimpleNamespace(get_frame_jpeg=lambda: calls.append("capture") or capture())),
    )
    monkeypatch.setattr(media, "load_config", lambda: AppConfig(camera_feed_enabled=True))
    pair(client, code)
    session = client.get("/api/owner/session").json()
    headers = {"Origin": ORIGIN, "X-Homebody-CSRF": session["csrf"]}
    return robot, client, headers, calls


def test_owner_live_frame_needs_owner_and_csrf(application, monkeypatch):
    _, client, headers, calls = setup_live(application, monkeypatch)
    assert client.post("/api/camera/frame").status_code == 403
    response = client.post("/api/camera/frame", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "image/jpeg"
    assert calls == ["capture"]
    client.cookies.clear()
    assert client.post("/api/camera/frame", headers=headers).status_code == 401
    assert client.get("/api/camera/connectivity").status_code == 401


@pytest.mark.parametrize(
    "mode,locked,kids,enabled",
    [
        ("sleep", False, False, True),
        ("meeting", False, False, True),
        ("awake", True, False, True),
        ("awake", False, True, True),
        ("awake", False, False, False),
    ],
)
def test_owner_live_frame_privacy_gates(application, monkeypatch, mode, locked, kids, enabled):
    import homebody.owner_media as media

    robot, client, headers, calls = setup_live(application, monkeypatch)
    robot._runtime.kids_controls_locked = locked
    robot._runtime.status = lambda: {"power_mode": mode, "kids_mode": {"active": kids}}
    monkeypatch.setattr(media, "load_config", lambda: AppConfig(camera_feed_enabled=enabled))
    assert client.post("/api/camera/frame", headers=headers).status_code in {403, 423}
    assert not calls


def test_owner_live_frame_rechecks_policy_and_revocation(application, monkeypatch):
    robot, client, headers, _ = setup_live(application, monkeypatch)
    robot._runtime.robot.media.get_frame_jpeg = lambda: (
        setattr(robot._runtime, "kids_controls_locked", True) or b"jpeg"
    )
    assert client.post("/api/camera/frame", headers=headers).status_code == 403
    robot._runtime.kids_controls_locked = False
    device = client.get("/api/owner/session").json()["device_id"]
    robot._runtime.robot.media.get_frame_jpeg = lambda: robot.settings_app.state.owner_store.revoke(device) or b"jpeg"
    assert client.post("/api/camera/frame", headers=headers).status_code == 401


def test_owner_connectivity_checks_welcome_without_media(application, monkeypatch):
    import homebody.owner_media as media

    _, client, _, calls = setup_live(application, monkeypatch)

    class Remote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            return '{"type":"welcome"}'

    monkeypatch.setattr(media, "connect", lambda *args, **kwargs: Remote())
    body = client.get("/api/camera/connectivity").json()
    assert body["signaling_ready"] is True
    assert body["media_verified"] is False
    assert body["camera_started"] is False
    assert not calls


@pytest.mark.parametrize("frame", [None, b"x" * 1_000_001, "not bytes"])
def test_owner_live_frame_rejects_unavailable_or_oversize(application, monkeypatch, frame):
    _, client, headers, _ = setup_live(application, monkeypatch, lambda: frame)
    assert client.post("/api/camera/frame", headers=headers).status_code == 503
