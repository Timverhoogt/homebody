"""Regression boundary: single-target -> candidate-pool contract over the real native adapter.

Synthetic responses/images only; real builder/create/parser, fake transport/moderation. No auth.
"""
from __future__ import annotations

import pytest
from test_ispy_target_variety import CHAIR, CUSHION, LAMP, SESSION, candidate
from test_kids_turn_deadlines_lifecycle import subscription_harness


def test_legacy_single_target_is_not_mistaken_for_a_candidates_pool(monkeypatch):
    harness, calls, _ = subscription_harness(monkeypatch, {"target": CHAIR})
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert len(calls) == 1
    assert calls[0]["text"]["format"]["name"] == "kids_ispy_candidates"
    assert calls[0]["text"]["format"]["schema"]["required"] == ["candidates"]
    assert SESSION not in harness.bridge._kids_sessions
    assert harness.moderated == []


@pytest.mark.parametrize("offset,expected", [(0, "bookshelf"), (1, "lamp"), (2, "cushion")])
def test_valid_native_multi_pool_is_randomized_and_not_always_bookshelf(monkeypatch, offset, expected):
    shelf = candidate("bookshelf", colour="brown", frame_index=1)
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [shelf, LAMP, CUSHION]})
    harness.rng.offset = offset
    assert harness.run()["target"]["object_name"] == expected
    [wire] = calls
    assert wire["reasoning"] == {"effort": "none"}
    pool = wire["text"]["format"]["schema"]["properties"]["candidates"]
    assert pool["minItems"] == 0 and pool["maxItems"] == 4  # an empty pool is a model refusal
    assert wire["extra_body"]["input"][0]["content"][0]["type"] == "input_text"
    assert "Choose up to 4 distinct visible objects" in wire["extra_body"]["input"][0]["content"][0]["text"]
    assert len(wire["extra_body"]["input"][0]["content"]) == 6  # prompt + five synthetic frames
    assert harness.bridge._kids_sessions[SESSION]["ispy_target"]["object_name"] == expected


def test_real_adapter_rounds_avoid_recent_targets_and_dedupe_bookshelf_aliases(monkeypatch):
    shelf = candidate("bookshelf", colour="brown", frame_index=1)
    alias = candidate("the bookshelves", colour="brown", frame_index=4)
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [shelf, alias, LAMP, CUSHION]})
    picks = [harness.run()["target"]["object_name"] for _ in range(3)]
    assert picks == ["bookshelf", "lamp", "cushion"]
    assert len(calls) == 3
    assert harness.rng.seen[0] == ["bookshelf", "lamp", "cushion"]
    assert harness.bridge._kids_sessions[SESSION]["ispy_recent"] == ["cushion", "lamp", "bookshelf"]


def test_one_legitimate_candidate_is_playable_even_after_repetition(monkeypatch):
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [CHAIR]})
    assert [harness.run()["target"]["object_name"] for _ in range(3)] == ["chair"] * 3
    assert len(calls) == 3


def test_model_selected_objects_are_not_overridden_by_a_local_name_blacklist(monkeypatch):
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": [candidate("person"), CHAIR]})
    assert [harness.run()["target"]["object_name"] for _ in range(3)] == ["person", "chair", "person"]
    assert len(calls) == 3


@pytest.mark.parametrize("pool", [[], [candidate("chair", frame_index=999)]])
def test_zero_usable_objects_never_fabricates_an_alternative(monkeypatch, pool):
    harness, calls, _ = subscription_harness(monkeypatch, {"candidates": pool})
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert len(calls) == 1 and harness.moderated == []
    assert SESSION not in harness.bridge._kids_sessions
