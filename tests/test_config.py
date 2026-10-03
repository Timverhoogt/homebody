from __future__ import annotations

import os
from pathlib import Path

import pytest

from homebody.config import AppConfig, load_config, merge_config, save_config


def test_config_round_trip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = AppConfig(bridge_url="http://hermes.local:8643", api_key="super-secret")
    save_config(original, path)

    loaded = load_config(path)
    assert loaded.bridge_url == "http://hermes.local:8643"
    assert loaded.api_key == "super-secret"
    assert loaded.instance_id == original.instance_id
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_redacted_config_never_exposes_secret() -> None:
    config = AppConfig(api_key="super-secret")
    payload = config.redacted_dict()
    assert payload["api_key"] == "********"
    assert payload["api_key_configured"] is True
    assert "super-secret" not in repr(payload)


def test_masked_key_keeps_existing_secret() -> None:
    current = AppConfig(api_key="keep-me")
    updated = merge_config(current, {"api_key": "********", "language": "nl"})
    assert updated.api_key == "keep-me"
    assert updated.language == "nl"


def test_embodiment_features_are_explicit_and_privacy_bounded_by_default() -> None:
    config = AppConfig()

    assert config.face_tracking_enabled is False
    assert config.gesture_detection_enabled is False
    assert config.camera_feed_enabled is False
    assert config.camera_controls_enabled is False
    assert config.camera_controls_handedness == "right"
    assert config.face_tracking_weight == 0.65
    assert config.doa_enabled is False
    assert config.robot_tools_enabled is True
    assert config.initiative_policy_enabled is False
    assert config.initiative_mode == "quiet"
    assert config.initiative_quiet_hours_enabled is True
    assert config.initiative_quiet_hours_start == "22:00"
    assert config.initiative_quiet_hours_end == "07:00"
    assert config.initiative_hourly_budget == 2
    assert config.initiative_daily_budget == 6
    assert config.contextual_offers_enabled is False
    assert config.contextual_offer_response_window_seconds == 10.0
    assert config.shared_physical_context_enabled is False
    assert config.presentation_window_seconds == 20.0


@pytest.mark.parametrize("mode", ["off", "chatty", "maintenance"])
def test_initiative_mode_is_bounded(mode: str) -> None:
    with pytest.raises(ValueError, match="initiative mode"):
        AppConfig(initiative_mode=mode)


@pytest.mark.parametrize("value", ["7:00", "24:00", "12:60", "noon"])
def test_initiative_quiet_hours_require_valid_24_hour_time(value: str) -> None:
    with pytest.raises(ValueError, match="quiet hours"):
        AppConfig(initiative_quiet_hours_start=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initiative_hourly_budget", 0),
        ("initiative_hourly_budget", 11),
        ("initiative_daily_budget", 0),
        ("initiative_daily_budget", 31),
        ("initiative_topic_cooldown_seconds", 59),
        ("initiative_duplicate_window_seconds", 29),
        ("initiative_dismissal_backoff_seconds", 59),
        ("contextual_offer_response_window_seconds", 4),
        ("contextual_offer_response_window_seconds", 31),
        ("presentation_window_seconds", 4),
        ("presentation_window_seconds", 31),
    ],
)
def test_initiative_limits_are_bounded(field: str, value: int) -> None:
    config = AppConfig()
    setattr(config, field, value)
    with pytest.raises(ValueError, match="Initiative|Contextual|Presentation"):
        config.validate()


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_face_tracking_weight_must_be_bounded(weight: float) -> None:
    with pytest.raises(ValueError, match="face_tracking_weight"):
        AppConfig(face_tracking_weight=weight)


@pytest.mark.parametrize("url", ["", "localhost:8643", "file:///tmp/socket"])
def test_bridge_url_must_be_http(url: str) -> None:
    with pytest.raises(ValueError):
        AppConfig(bridge_url=url)


def test_capability_profile_is_bounded_to_conversation_or_agent() -> None:
    assert AppConfig().capability_profile == "conversation"
    assert AppConfig(capability_profile=" Agent ").capability_profile == "agent"
    with pytest.raises(ValueError, match="capability profile"):
        AppConfig(capability_profile="maintenance")


def test_config_transaction_serialises_read_modify_write(tmp_path, monkeypatch) -> None:
    import threading
    import time

    from homebody.config import config_transaction, load_config, merge_config, save_config

    monkeypatch.setenv("HOMEBODY_CONFIG", str(tmp_path / "config.json"))
    save_config(AppConfig())
    first_loaded = threading.Event()

    def slow_settings_save() -> None:
        with config_transaction():
            current = load_config()
            first_loaded.set()
            time.sleep(0.2)
            save_config(merge_config(current, {"language": "nl"}))

    def home_assistant_toggle() -> None:
        first_loaded.wait(timeout=2.0)
        with config_transaction():
            save_config(merge_config(load_config(), {"motion_enabled": False}))

    writers = [threading.Thread(target=slow_settings_save), threading.Thread(target=home_assistant_toggle)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=5.0)

    final = load_config()
    assert final.language == "nl"
    assert final.motion_enabled is False


def test_default_config_path_prefers_homebody_and_keeps_pre_rename_settings(tmp_path, monkeypatch) -> None:
    from homebody.config import default_config_path

    monkeypatch.delenv("HOMEBODY_CONFIG", raising=False)
    monkeypatch.delenv("REACHY_MINI_HERMES_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    data = tmp_path / ".local" / "share"
    assert default_config_path() == data / "homebody" / "config.json"

    legacy = data / "reachy_mini_hermes" / "config.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    assert default_config_path() == legacy

    current = data / "homebody" / "config.json"
    current.parent.mkdir(parents=True)
    current.write_text("{}", encoding="utf-8")
    assert default_config_path() == current


def test_default_config_path_honours_new_and_pre_rename_overrides(tmp_path, monkeypatch) -> None:
    from homebody.config import default_config_path

    monkeypatch.delenv("HOMEBODY_CONFIG", raising=False)
    monkeypatch.setenv("REACHY_MINI_HERMES_CONFIG", str(tmp_path / "legacy.json"))
    assert default_config_path() == tmp_path / "legacy.json"
    monkeypatch.setenv("HOMEBODY_CONFIG", str(tmp_path / "homebody.json"))
    assert default_config_path() == tmp_path / "homebody.json"
