"""Silent, verified controls for the robot's Linux ALSA speaker, never its microphone.

SDK 1.9's /api/volume/set always plays a tone, and constructing its Linux mixer
resets capture controls. Neither is safe for quiet/privacy-preserving adjustments.
Discover the named robot playback card and index-zero pvolume control instead.
Do not fall back to a default/HDMI device or claim support on another platform.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext


class AlsaSpeaker:
    def __init__(self, *, run: Callable = subprocess.run) -> None:
        self._run = run
        self._deadline: float | None = None

    @contextmanager
    def transaction(self):
        self._deadline = time.monotonic() + 3
        try:
            yield
        finally:
            self._deadline = None

    def _command(self, args: list[str]) -> str:
        try:
            remaining = self._deadline - time.monotonic() if self._deadline is not None else 2
            if remaining <= 0:
                raise RuntimeError("Reachy speaker mixer transaction deadline exceeded")
            result = self._run(
                args,
                capture_output=True,
                text=True,
                check=True,
                timeout=min(2, remaining),
                shell=False,
                env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Reachy speaker mixer is unavailable") from exc

    def _target(self) -> tuple[str, str]:
        if platform.system() != "Linux":
            raise RuntimeError("Silent speaker control currently requires Linux ALSA")
        cards = re.findall(r"^card (\d+): [^\n\[]+\[([^\]]+)\]", self._command(["aplay", "-l"]), re.M)
        names = {"reachy mini audio", "respeaker", "respeaker 4 mic array (uac1)"}
        matches = {card for card, name in cards if name.casefold() in names}
        if len(matches) != 1:
            raise RuntimeError("One unambiguous Reachy speaker is required; no default device will be changed")
        card = matches.pop()
        text = self._command(["amixer", "-c", card, "scontents"])
        controls = []
        for block in re.split(r"(?=^Simple mixer control )", text, flags=re.M):
            header = re.match(r"Simple mixer control '([A-Za-z0-9 _-]+)',0\n", block)
            capability = re.search(r"^\s*Capabilities: (.+)$", block, re.M)
            if header and capability and "pvolume" in capability[1].split() and "cvolume" not in capability[1].split():
                controls.append(header[1] + ",0")
        if len(controls) != 1:
            raise RuntimeError("Reachy speaker playback control is unavailable or ambiguous")
        return card, controls[0]

    def read(self) -> int:
        card, control = self._target()
        text = self._command(["amixer", "-c", card, "sget", control])
        channels = re.findall(r"Playback[^\n]*?\[(\d+)%\]", text)
        if not channels or len(set(channels)) != 1:
            raise RuntimeError("Reachy speaker channels could not be read consistently")
        switches = re.findall(r"Playback[^\n]*?\[(on|off)\]", text)
        if switches and len(set(switches)) != 1:
            raise RuntimeError("Reachy speaker playback switches are inconsistent")
        return 0 if switches and switches[0] == "off" else int(channels[0])

    def write(self, volume: int) -> None:
        card, control = self._target()
        args = ["amixer", "-c", card, "sset", control, f"{volume}%"]
        if volume > 0:
            args.append("unmute")
        self._command(args)


class SpeakerVolume:
    def __init__(self, mixer: AlsaSpeaker | None = None) -> None:
        self._mixer = mixer if mixer is not None else AlsaSpeaker()
        self._lock = threading.RLock()
        self._restore: int | None = None

    def _read(self) -> int:
        value = self._mixer.read()
        if type(value) is not int or not 0 <= value <= 100:
            raise RuntimeError("Reachy speaker volume is unavailable")
        if value > 0:
            self._restore = value
        return value

    def _status(self, volume: int) -> dict[str, object]:
        return {"volume": volume, "muted": volume == 0, "can_unmute": self._restore is not None}

    def read(self) -> dict[str, object]:
        with self._lock, self._transaction():
            return self._status(self._read())

    def _transaction(self):
        limit = getattr(self._mixer, "transaction", None)
        return limit() if callable(limit) else nullcontext()

    def set(self, *, volume: int | None = None, muted: bool | None = None) -> dict[str, object]:
        if (volume is None) == (muted is None):
            raise ValueError("Supply exactly one of volume or muted")
        if volume is not None and (type(volume) is not int or not 0 <= volume <= 100):
            raise ValueError("Speaker volume must be an integer from 0 to 100")
        if muted is not None and type(muted) is not bool:
            raise ValueError("muted must be a boolean")
        with self._lock, self._transaction():
            current = self._read()
            if muted is False and current == 0 and self._restore is None:
                raise ValueError("Choose a volume to unmute; no previous level is known")
            target = volume if volume is not None else 0 if muted else current or self._restore
            assert target is not None
            if current != target:
                self._mixer.write(target)
            actual = self._read()
            # Hardware steps may round by 1–2%, but a requested mute must be exact.
            if (target == 0 and actual != 0) or abs(actual - target) > 2:
                raise RuntimeError("Speaker volume change was not confirmed; read the current volume before retrying")
            return self._status(actual)
