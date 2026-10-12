"""Summarize owner voice timings (``GET /api/voice/timings``) saved as JSON; no network access.

Usage: python tools/voice_timing_report.py timings.json

Reports p50/p90 per engine, assistant mode and cold/warm for each interval from ``wake``
(turn start). ``player_enqueued`` is an SDK enqueue and ``playback_estimated`` is the local
queue's estimate; neither proves the speaker was heard. Collecting the input file needs a
supervised session; this script never calls the robot, the bridge or a model provider.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

INTERVALS = (
    "session_ready",
    "local_speech_end",
    "provider_speech_end",
    "tool_requested",
    "tool_result",
    "first_provider_audio",
    "player_enqueued",
    "playback_estimated",
    "useful_result_verified",
    "turn_end",
)


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def summarize(turns: list[dict[str, object]]) -> dict[str, dict[str, dict[str, int]]]:
    groups: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for turn in turns:
        marks = turn.get("marks") if isinstance(turn.get("marks"), dict) else {}
        key = f"{turn.get('engine')}/{turn.get('mode')}/{'warm' if turn.get('warm') else 'cold'}"
        for name in INTERVALS:
            value = marks.get(name)
            if isinstance(value, int) and value >= 0:
                groups[key][name].append(value)
    return {
        key: {name: {"n": len(v), "p50_ms": percentile(v, 0.5), "p90_ms": percentile(v, 0.9)}
              for name, v in marks.items()}
        for key, marks in groups.items()
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    turns = payload.get("turns", payload) if isinstance(payload, dict) else payload
    print(json.dumps(summarize(turns if isinstance(turns, list) else []), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
