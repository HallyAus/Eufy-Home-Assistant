"""Pure scheduling tests for the one-session adaptive warmer."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eufy_warm", ROOT / "bridge/eufy_warm.py")
warmer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(warmer)


def test_own_warmer_is_not_counted_as_external_viewer():
    streams = {
        "eufy_front": {"consumers": [{"format_name": "mpegts"}]},
        "eufy_shed": {"consumers": []},
        "other": {"consumers": [{}]},
    }
    assert warmer.external_consumer_counts(streams, "eufy_front", True) == {
        "eufy_front": 0,
        "eufy_shed": 0,
    }


def test_real_viewer_is_preserved_after_subtracting_warmer():
    streams = {"eufy_front": {"consumers": [{}, {}]}}
    assert warmer.external_consumer_counts(streams, "eufy_front", True) == {
        "eufy_front": 1
    }


def test_current_stream_wins_a_tie_to_avoid_thrashing():
    counts = {"eufy_front": 1, "eufy_shed": 1}
    assert warmer.choose_stream(counts, "eufy_shed") == "eufy_shed"


def test_largest_demand_then_stable_name_wins():
    assert warmer.choose_stream(
        {"eufy_shed": 1, "eufy_gate": 2, "eufy_front": 2}, None
    ) == "eufy_front"
    assert warmer.choose_stream({"eufy_front": 0}, None) is None
