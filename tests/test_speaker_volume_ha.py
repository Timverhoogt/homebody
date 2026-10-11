"""Home Assistant uses the same silent verified service; zero is not a missing/default level."""

from types import SimpleNamespace

from homebody.config import AppConfig
from homebody.home_assistant import HermesHomeAssistantProvider


def test_ha_zero_volume_is_not_reported_as_maximum():
    runtime = SimpleNamespace(status=lambda: {}, speaker_volume=lambda: {"volume": 0, "muted": True})
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: AppConfig())
    provider._volume = lambda: 0
    state = provider.read("reachy_mini_media_player")
    assert state["volume"] == 0 and state["muted"] is True


def test_ha_volume_routes_through_guarded_silent_runtime_and_requires_controls():
    writes = []
    runtime = SimpleNamespace(
        control_ready=True,
        status=lambda: {},
        kids_controls_locked=False,
        set_speaker_volume=lambda **args: writes.append(args) or {"volume": args["volume"]},
    )
    config = [AppConfig(home_assistant_enabled=True, home_assistant_controls_enabled=False)]
    provider = HermesHomeAssistantProvider(runtime, config_loader=lambda: config[0])
    assert provider.write("speaker_volume", 30) is False
    assert writes == []
    config[0] = AppConfig(home_assistant_enabled=True, home_assistant_controls_enabled=True)
    assert provider.write("speaker_volume", 30) is True
    assert writes == [{"volume": 30}]
    assert provider.write("speaker_volume", float("nan")) is False
    assert provider.write("speaker_volume", True) is False


def test_telemetry_only_bridge_can_read_without_full_runtime(monkeypatch):
    from homebody.speaker_volume import SpeakerVolume

    monkeypatch.setattr(SpeakerVolume, "read", lambda self: {"volume": 62, "muted": False})
    provider = HermesHomeAssistantProvider(SimpleNamespace(status=lambda: {}), config_loader=lambda: AppConfig())
    assert provider._volume() == 62


def test_runtime_refusal_is_not_bypassed_by_telemetry_fallback(monkeypatch):
    from homebody.speaker_volume import SpeakerVolume

    def refused():
        raise RuntimeError("child session")

    def unexpected(self):
        raise AssertionError("must not bypass runtime refusal")

    monkeypatch.setattr(SpeakerVolume, "read", unexpected)
    provider = HermesHomeAssistantProvider(SimpleNamespace(status=lambda: {}, speaker_volume=refused))
    assert provider._volume() is None

