"""Silent speaker-only mixer controls: no SDK client, test tone, default device or microphone writes."""

import subprocess
import threading
from types import SimpleNamespace

import pytest

from homebody.runtime import HermesVoiceRuntime


def test_runtime_exposes_confirmed_volume():
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    assert callable(getattr(runtime, "speaker_volume", None))
    assert callable(getattr(runtime, "set_speaker_volume", None))


class Mixer:
    def __init__(self, value=62):
        self.value = value
        self.writes = []
        self.fail = False
        self.ignore = False

    def read(self):
        return self.value

    def write(self, value):
        self.writes.append(value)
        if self.fail:
            raise RuntimeError("Mixer unavailable")
        if not self.ignore:
            self.value = value


def service(value=62):
    from homebody.speaker_volume import SpeakerVolume

    mixer = Mixer(value)
    return SpeakerVolume(mixer), mixer


def test_mute_restores_measured_volume_without_defaulting_to_maximum():
    speaker, mixer = service()
    assert speaker.read() == {"volume": 62, "muted": False, "can_unmute": True}
    assert speaker.set(muted=True)["volume"] == 0
    assert speaker.set(muted=True)["volume"] == 0
    assert speaker.set(muted=False)["volume"] == 62
    assert mixer.writes == [0, 62]
    restarted, zero = service(0)
    assert restarted.read()["can_unmute"] is False
    with pytest.raises(ValueError, match="Choose a volume"):
        restarted.set(muted=False)
    assert zero.writes == []
    assert restarted.set(volume=25)["volume"] == 25


@pytest.mark.parametrize("value", [-1, 101, True, False, "50", 50.5, float("nan"), float("inf"), None])
def test_invalid_volume_never_touches_hardware(value):
    speaker, mixer = service()
    with pytest.raises(ValueError):
        speaker.set(volume=value)
    assert mixer.writes == []


@pytest.mark.parametrize("args", [{}, {"volume": 20, "muted": True}, {"muted": "yes"}, {"muted": 1}])
def test_mutations_are_exact_and_strict(args):
    speaker, mixer = service()
    with pytest.raises(ValueError):
        speaker.set(**args)
    assert mixer.writes == []


def test_post_write_readback_rejects_false_success_and_reports_quantization():
    speaker, mixer = service()
    mixer.ignore = True
    with pytest.raises(RuntimeError, match="not confirmed"):
        speaker.set(volume=20)
    mixer.ignore = False

    def quantized(value):
        mixer.value = value - 1

    mixer.write = quantized
    assert speaker.set(volume=30)["volume"] == 29


@pytest.mark.parametrize("value", [-1, 200, True, None])
def test_invalid_hardware_reads_fail_closed(value):
    speaker, _ = service(value)
    with pytest.raises(RuntimeError):
        speaker.read()


def test_alsa_driver_targets_named_playback_only_with_bounded_no_shell_commands():
    from homebody.speaker_volume import AlsaSpeaker

    calls = []
    level = [62]

    def run(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs["timeout"] <= 2 and kwargs.get("shell", False) is False
        if args == ["aplay", "-l"]:
            text = "card 0: Audio [Reachy Mini Audio], device 0: USB Audio [USB Audio]\ncard 1: HDA [NVIDIA HDA]"
        elif args[-1] == "scontents":
            text = (
                "Simple mixer control 'PCM',0\n  Capabilities: pvolume pswitch\n"
                "Simple mixer control 'PCM',1\n  Capabilities: pvolume\n"
                "Simple mixer control 'Headset',0\n  Capabilities: cvolume cswitch"
            )
        else:
            if "sset" in args:
                level[0] = int(args[5].rstrip("%"))
            text = f"  Front Left: Playback 37 [{level[0]}%] [on]\n  Front Right: Playback 37 [{level[0]}%] [on]"
        return subprocess.CompletedProcess(args, 0, text, "")

    driver = AlsaSpeaker(run=run)
    assert driver.read() == 62
    driver.write(30)
    assert driver.read() == 30
    assert ["amixer", "-c", "0", "sset", "PCM,0", "30%", "unmute"] in calls
    assert all("Headset" not in str(args) and "8000" not in str(args) for args in calls)


@pytest.mark.parametrize(
    "cards", ["card 1: HDA [NVIDIA HDA]", "card 0: X [Reachy Mini Audio]\ncard 1: Y [Reachy Mini Audio]"]
)
def test_missing_or_ambiguous_robot_audio_never_falls_back_to_default(cards):
    from homebody.speaker_volume import AlsaSpeaker

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, cards, "")

    with pytest.raises(RuntimeError, match="Reachy speaker"):
        AlsaSpeaker(run=run).write(20)
    assert calls == [["aplay", "-l"]]


@pytest.mark.parametrize("agent", [False, True])
def test_runtime_serializes_and_refuses_kids_or_stopping(agent):
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    runtime._control_ready.set()
    runtime._speaker, mixer = service()
    runtime._kids_active = True
    with pytest.raises(RuntimeError, match="child session"):
        runtime.set_speaker_volume(volume=20, agent=agent)
    runtime._kids_active = False
    runtime._kids_locked = True
    with pytest.raises(RuntimeError, match="child session"):
        runtime.set_speaker_volume(volume=20, agent=agent)
    runtime._kids_locked = False
    runtime.stop_event.set()
    with pytest.raises(RuntimeError, match="stopping"):
        runtime.set_speaker_volume(volume=20, agent=agent)
    assert mixer.writes == []


def test_owner_silent_controls_work_in_sleep_but_agent_privacy_never_bypassed():
    runtime = HermesVoiceRuntime(SimpleNamespace(), threading.Event())
    runtime._control_ready.set()
    runtime._speaker, mixer = service()
    runtime._effective_power_mode = lambda: "sleep"
    with pytest.raises(RuntimeError, match="private|Sleep"):
        runtime.set_speaker_volume(volume=20, agent=True)
    assert runtime.set_speaker_volume(volume=20)["volume"] == 20
    runtime._effective_power_mode = lambda: "standby"
    runtime._privacy_requested.set()
    with pytest.raises(RuntimeError, match="private"):
        runtime.set_speaker_volume(volume=30, agent=True)
    assert mixer.writes == [20]


def test_playback_hardware_switch_off_is_effectively_muted():
    from homebody.speaker_volume import AlsaSpeaker

    def run(args, **kwargs):
        if args == ["aplay", "-l"]:
            text = "card 0: Audio [Reachy Mini Audio]"
        elif args[-1] == "scontents":
            text = "Simple mixer control 'PCM',0\n  Capabilities: pvolume pswitch"
        else:
            text = "  Front Left: Playback 37 [62%] [off]\n  Front Right: Playback 37 [62%] [off]"
        return subprocess.CompletedProcess(args, 0, text, "")

    assert AlsaSpeaker(run=run).read() == 0


def test_mixer_transaction_has_one_total_deadline(monkeypatch):
    from homebody import speaker_volume as module

    clock = [0.0]
    import time

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def run(args, **kwargs):
        clock[0] += 1.1
        text = (
            "card 0: Audio [Reachy Mini Audio]"
            if args == ["aplay", "-l"]
            else (
                "Simple mixer control 'PCM',0\n  Capabilities: pvolume"
                if args[-1] == "scontents"
                else "  Front Left: Playback 37 [62%] [on]\n  Front Right: Playback 37 [62%] [on]"
            )
        )
        return subprocess.CompletedProcess(args, 0, text, "")

    speaker = module.SpeakerVolume(module.AlsaSpeaker(run=run))
    with pytest.raises(RuntimeError, match="deadline"):
        speaker.set(volume=20)
