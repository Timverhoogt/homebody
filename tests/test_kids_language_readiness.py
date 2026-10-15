"""The source readiness report separates source, supplied config and manual acceptance; no I/O beyond reads."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_tool():
    path = ROOT / "tools" / "kids_language_readiness.py"
    spec = importlib.util.spec_from_file_location("kids_language_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_layers_are_complete_and_propagated_for_all_five_languages() -> None:
    result = load_tool().report()
    assert result["unsupported_example_rejected"] is True
    for language in ("es", "de", "fr", "it", "pt"):
        entry = result["languages"][language]
        assert entry["source"] == {"catalog_complete": True, "propagated_to_all_layers": True, "missing_layers": []}
        # Without supplied bridge health nothing about the voice is claimed; no clips are shipped.
        assert entry["config"]["voice_proof"] == "not_supplied"
        assert entry["config"]["waiting_clip_bundle"] == "missing"
        assert set(entry["acceptance"].values()) == {"pending_manual"}
        assert entry["enable_for_children"] is False


@pytest.mark.parametrize("language", ["es", "de", "fr", "it", "pt"])
def test_a_valid_voice_proof_never_satisfies_acceptance_gates(language) -> None:
    proof = {"kids_voice_sha256": "d" * 64, "kids_tts_model": "eleven_flash_v2_5"}
    result = load_tool().report({"kids_voice_proofs": {language: proof, "en": proof}})
    for code, entry in result["languages"].items():
        assert entry["config"]["voice_proof"] == ("present" if code == language else "missing")
        assert set(entry["acceptance"].values()) == {"pending_manual"}
        assert entry["enable_for_children"] is False


def test_malformed_proofs_are_reported_missing() -> None:
    bad = {"kids_voice_sha256": "D" * 64, "kids_tts_model": "eleven_flash_v2_5"}
    result = load_tool().report({"kids_voice_proofs": {"fr": bad, "it": {}, "pt": "x"}})
    assert {result["languages"][code]["config"]["voice_proof"] for code in ("fr", "it", "pt")} == {"missing"}
