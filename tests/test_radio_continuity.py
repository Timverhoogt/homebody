"""Real worker, simulated clock/decoder/send delays; no network or physical media."""

from types import SimpleNamespace

import numpy as np
import pytest

from homebody import radio


class Simulation:
    def __init__(self, monkeypatch, *, send_cost=0.01, read_cost=lambda n: 0.0, sizes=(640,), seconds=5):
        self.now = 0.0
        self.send_cost, self.read_cost = send_cost, read_cost
        self.sizes, self.seconds = sizes, seconds
        self.records = []
        self.reads = 0
        self.clears = 0
        self.clear_boundaries = []
        self.wait_hook = lambda: None
        self.permitted = True
        sim = self
        monkeypatch.setattr(radio, "time", SimpleNamespace(monotonic=lambda: sim.now))

        class Event:
            stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, seconds):
                assert seconds >= 0
                sim.now += seconds
                sim.wait_hook()
                return self.stopped

        class Decoder:
            def __init__(self, rate):
                assert rate == 16000

            def read(self):
                sim.reads += 1
                sim.now += sim.read_cost(sim.reads)
                size = sim.sizes[(sim.reads - 1) % len(sim.sizes)]
                return np.full(size, sim.reads / 1000, dtype=np.float32)

            def close(self):
                pass

        class Output:
            def push_audio_sample(self, samples):
                sim.now += sim.send_cost
                sim.records.append((sim.now, len(samples) / 16000, float(samples[0])))

            def clear(self):
                sim.clears += 1
                sim.clear_boundaries.append(len(sim.records))

        self.player = radio.RadioPlayer(
            Output(), 16000, allowed=lambda: sim.permitted and sim.now < sim.seconds, decoder_factory=Decoder
        )
        self.player.cancelled = Event()
        self.player.resume()

    def run(self):
        self.player._run()
        assert not self.player.error
        assert self.player.state == "stopped"

    def gaps_and_max_reserve(self):
        end = None
        gaps = []
        maximum = 0.0
        for index, (now, duration, _) in enumerate(self.records):
            if index in self.clear_boundaries:
                end = None
            if end is not None:
                gaps.append(max(0, now - end))
            end = max(now, end or now) + duration
            maximum = max(maximum, end - now)
        return sum(gaps), maximum


@pytest.mark.parametrize("send_cost", [0.0, 0.003, 0.01])
def test_send_and_decode_costs_do_not_accumulate_into_gaps(monkeypatch, send_cost):
    sim = Simulation(monkeypatch, send_cost=send_cost, read_cost=lambda n: (0.001, 0.003)[n % 2])
    sim.run()
    gaps, reserve = sim.gaps_and_max_reserve()
    assert len(sim.records) > 100
    assert gaps == pytest.approx(0, abs=1e-7)
    assert 0.1 <= reserve <= 0.2  # Software estimate, not a measured SDK queue depth.
    assert sim.records[0][0] <= 0.25


def test_small_source_jitter_and_variable_chunks_keep_continuity(monkeypatch):
    sim = Simulation(monkeypatch, read_cost=lambda n: (0, 0.015, 0.035, 0.005)[n % 4], sizes=(640, 320, 480))
    sim.run()
    gaps, reserve = sim.gaps_and_max_reserve()
    assert gaps == pytest.approx(0, abs=1e-7)
    assert reserve <= 0.2
    # PCM is preserved in order; never drop packets to hide an underrun.
    ids = [v for _, _, v in sim.records]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_long_source_stall_rebases_instead_of_unbounded_catchup(monkeypatch, caplog):
    sim = Simulation(monkeypatch, read_cost=lambda n: 0.8 if n == 25 else 0)
    sim.run()
    gaps, reserve = sim.gaps_and_max_reserve()
    assert gaps > 0.5  # A stall exceeding the bounded reserve cannot be made inaudible.
    assert reserve <= 0.2
    assert len(sim.records) > 50


@pytest.mark.parametrize("boundary", ["stop", "permission"])
def test_stop_or_revocation_during_prefill_cannot_enqueue(monkeypatch, boundary):
    sim = Simulation(monkeypatch)

    def revoke():
        if boundary == "stop":
            sim.player.stop()
        else:
            sim.permitted = False

    sim.wait_hook = revoke
    sim.run()
    assert sim.records == []
    assert sim.clears == 0


@pytest.mark.parametrize("sizes", [(640,), (16000,)])
def test_pause_resume_during_pacing_retires_pre_pause_chunk(monkeypatch, sizes):
    sim = Simulation(monkeypatch, sizes=sizes)
    retired = []

    def interrupt():
        if len(sim.records) >= 5 and not retired:
            retired.append((sim.reads / 2000, len(sim.records)))  # Worker applies 0.5 gain.
            sim.player.pause()
            assert sim.clears == 1
            sim.player.resume()

    sim.wait_hook = interrupt
    sim.run()
    assert retired
    # The read chunk waiting when pause/resume occurred must not leak afterward.
    value, boundary = retired[0]
    assert all(abs(v - value) > 1e-7 for _, _, v in sim.records[boundary:])
    assert len(sim.records) > 30  # A fresh generation still makes forward progress.
    assert sim.gaps_and_max_reserve()[1] <= 0.2


def test_pause_resume_while_decoder_read_is_held_retires_its_entire_result(monkeypatch):
    sim = None

    def read_cost(n):
        if n == 8:
            sim.player.pause()
            sim.player.resume()
        return 0.002

    sim = Simulation(monkeypatch, read_cost=read_cost)
    sim.run()
    assert sim.reads > 10
    assert all(abs(v - 8 / 2000) > 1e-7 for _, _, v in sim.records)
