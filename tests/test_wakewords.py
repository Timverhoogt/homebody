from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "homebody"


def test_bundled_wake_phrases_use_verified_gigaspeech_bpe_tokens() -> None:
    lines = (PACKAGE / "assets" / "keywords.txt").read_text(encoding="utf-8").splitlines()

    assert lines == [
        "▁HE Y ▁HER ME S :1.5 #0.25 @HEY_HERMES",
        "▁OKAY ▁NA B U :1.5 #0.25 @OKAY_NABU",
        "▁HE Y ▁RE A CH Y :1.5 #0.25 @HEY_REACHY",
    ]


def test_runtime_and_public_status_advertise_all_wake_phrases() -> None:
    runtime = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
    backend = (PACKAGE / "main.py").read_text(encoding="utf-8")
    html = (PACKAGE / "static" / "index.html").read_text(encoding="utf-8")

    for phrase in ("Hey Hermes", "Okay Nabu", "Hey Reachy"):
        assert phrase in runtime
        assert phrase in backend
        assert phrase in html
    assert '"wake_phrases": ["Hey Hermes", "Okay Nabu", "Hey Reachy"]' in backend
