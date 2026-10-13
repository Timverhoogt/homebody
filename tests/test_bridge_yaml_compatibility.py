"""Provider metadata must survive managed Hermes without legacy PyYAML."""

import builtins
import sys
from types import SimpleNamespace

import pytest
import yaml
from test_bridge import load_bridge_module


def configured(monkeypatch, tmp_path, text):
    home = tmp_path / "profiles" / "reachy"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(text)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return load_bridge_module()


def test_managed_reader_works_when_legacy_yaml_import_is_unavailable(monkeypatch, tmp_path):
    bridge = configured(monkeypatch, tmp_path, "stt:\n  provider: local\ntts:\n  provider: edge\n")
    seen = []

    def read(text):
        seen.append(text)
        return yaml.safe_load(text)

    monkeypatch.setitem(sys.modules, "hermes_yaml", SimpleNamespace(safe_load=read, YAMLError=yaml.YAMLError))
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No legacy yaml in managed runtime")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    result = bridge._configured_voice_providers("reachy")
    assert result == {"stt_provider": "local", "tts_provider": "edge"}
    assert len(seen) == 1
    assert bridge._stt_locality(result["stt_provider"], True) == "local"


@pytest.mark.parametrize("provider,locality", [("local_command", "local"), ("groq", "cloud"), ("custom", "unknown")])
def test_standalone_reader_and_truthful_provider_mapping(monkeypatch, tmp_path, provider, locality):
    bridge = configured(monkeypatch, tmp_path, f"stt:\n  provider: {provider}\n")
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    result = bridge._configured_voice_providers("reachy")
    assert result == {"stt_provider": provider}
    assert bridge._stt_locality(provider, True) == locality
    assert bridge._stt_locality(provider, False) == "unknown"


@pytest.mark.parametrize(
    "text", ["stt: [", "- non-mapping", "stt: false", "!!python/object/apply:os.system ['false']"]
)
def test_invalid_metadata_fails_closed(monkeypatch, tmp_path, text):
    bridge = configured(monkeypatch, tmp_path, text)
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    assert bridge._configured_voice_providers("reachy") == {}


def test_no_reader_is_unknown(monkeypatch, tmp_path):
    bridge = configured(monkeypatch, tmp_path, "stt:\n  provider: local\n")
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert bridge._configured_voice_providers("reachy") == {}


def test_missing_profile_config_is_unknown(monkeypatch, tmp_path):
    bridge = configured(monkeypatch, tmp_path, "stt:\n  provider: local\n")
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    (tmp_path / "profiles" / "reachy" / "config.yaml").unlink()
    assert bridge._configured_voice_providers("reachy") == {}
