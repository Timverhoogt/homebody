"""Source-only Kids language readiness report; never reads secrets, contacts a provider or the robot.

Three things are reported separately per language and must not be confused:

* ``source``: catalog completeness and supported-language propagation, derived from the source tree;
* ``config``: whether a per-language voice proof and a waiting-clip bundle were *supplied* (a bridge
  health ``kids_voice_proofs`` map passed in by the caller, and the app-owned asset directory);
* ``acceptance``: manual gates (native-speaker review, provider audition/screening, recognition and
  caregiver-supervised physical acceptance). These are never derived from source or config: a voice
  digest proves which voice id is configured, not that it was auditioned, reviewed or accepted.

Usage: ``python tools/kids_language_readiness.py`` (prints JSON; no arguments, no network).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEW_LANGUAGES = ("es", "de", "fr", "it", "pt")
MANUAL_GATES = (
    "owner_voice_and_region_selection", "native_speaker_copy_review", "provider_voice_audition",
    "recognition_accuracy_on_robot", "caregiver_supervised_physical_acceptance",
)


def _propagation() -> dict[str, frozenset[str]]:
    """Supported language sets as each layer declares them in source."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from companion import kids_fixed_copy
    from homebody import asr_worker, kids_acknowledgements, kids_game_master, kids_mode
    from homebody.ispy import COLOURS, SESSION_HINT_LANGUAGES

    bridge = (ROOT / "companion" / "hermes_reachy_bridge.py").read_text(encoding="utf-8")
    bridge_set = re.search(r"_KIDS_LANGUAGES = frozenset\(\{([^}]*)\}\)", bridge)
    stt = (ROOT / "companion" / "local_stt_command.py").read_text(encoding="utf-8")
    stt_hint = re.search(r"homebody-stt-\(([a-z|]+)\)-", stt)
    main = (ROOT / "homebody" / "main.py").read_text(encoding="utf-8")
    route = re.search(r'language: str = Field\(default="en", pattern=r"\^\(([a-z|]+)\)\$"\)', main)
    html = (ROOT / "homebody" / "static" / "index.html").read_text(encoding="utf-8")

    def options(select_id: str) -> frozenset[str]:
        match = re.search(rf'<select id="{select_id}">(.*?)</select>', html)
        return frozenset(re.findall(r'<option value="([a-z]+)"', match.group(1) if match else ""))

    return {
        "bridge_catalog": frozenset(kids_fixed_copy.LANGUAGES),
        "bridge_session": frozenset(re.findall(r'"([a-z]{2})"', bridge_set.group(1) if bridge_set else "")),
        "robot_profile": frozenset(kids_mode.LANGUAGES),
        "robot_session_voice": frozenset(kids_mode.SESSION_VOICE_LANGUAGES) | {"en", "nl"},
        "game_master_copy": frozenset(kids_game_master._COPY),
        "ispy_colours": frozenset.intersection(*(frozenset(names) for names in COLOURS.values())),
        "ispy_session_hints": frozenset(SESSION_HINT_LANGUAGES) | {"en", "nl"},
        "waiting_phrases": frozenset(kids_acknowledgements.ALLOWED_PHRASES),
        "robot_stt_worker": frozenset(asr_worker._LANGUAGES) - {"auto"},
        "host_local_stt_hint": frozenset((stt_hint.group(1) if stt_hint else "").split("|")) - {""},
        "start_route": frozenset((route.group(1) if route else "").split("|")) - {""},
        "kids_picker": options("kids-language"),
        "onboarding_picker": options("setup-language"),
    }


def _catalog_complete(language: str) -> bool:
    from companion import kids_fixed_copy as catalog

    try:
        for activity in catalog.ACTIVITIES:
            catalog.render_segment({"kind": "greeting"}, activity=activity, language=language)
            for key, slots in catalog.HOST_SLOTS.items():
                args = dict(zip(slots, ("story", "riddles"), strict=False))
                catalog.render_segment({"kind": "host", "key": key, **args}, activity=activity, language=language)
        catalog.render_segment({"kind": "notice", "key": "five_minutes"}, activity="story", language=language)
    except (KeyError, ValueError):
        return False
    return True


def _proof_supplied(proofs: object, language: str) -> bool:
    proof = proofs.get(language) if isinstance(proofs, dict) else None
    digest = proof.get("kids_voice_sha256") if isinstance(proof, dict) else None
    return (
        isinstance(proof, dict) and proof.get("kids_tts_model") == "eleven_flash_v2_5"
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def report(health: dict[str, object] | None = None) -> dict[str, object]:
    """Readiness per new language. ``health`` is an optional bridge health payload supplied by the caller."""
    layers = _propagation()
    asset_root = ROOT / "homebody" / "assets" / "kids_acknowledgements"
    proofs = health.get("kids_voice_proofs") if isinstance(health, dict) else None
    languages: dict[str, object] = {}
    for language in NEW_LANGUAGES:
        missing_layers = sorted(name for name, codes in layers.items() if language not in codes)
        languages[language] = {
            "source": {
                "catalog_complete": _catalog_complete(language),
                "propagated_to_all_layers": not missing_layers,
                "missing_layers": missing_layers,
            },
            "config": {
                "voice_env_var": f"ELEVENLABS_KIDS_{language.upper()}_VOICE_ID",
                "voice_proof": "not_supplied" if proofs is None
                else ("present" if _proof_supplied(proofs, language) else "missing"),
                "waiting_clip_bundle": "present" if (asset_root / language / "manifest.json").is_file() else "missing",
            },
            # Never inferred: only an owner-recorded manual result could change these.
            "acceptance": {gate: "pending_manual" for gate in MANUAL_GATES},
            "enable_for_children": False,
        }
    return {"languages": languages, "unsupported_example_rejected": "sv" not in layers["bridge_catalog"]}


if __name__ == "__main__":
    print(json.dumps(report(), indent=2, sort_keys=True))
