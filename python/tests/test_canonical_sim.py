"""Canonical (buff-window-aligned) simulator — the 'hold burst for the 2-min
window' comparison lane.

Covers the pure forced-alignment math (`_canonical_burst_forbidden`) and that
`simulate_canonical_aligned` actually casts Wildfire + Barrel Stabilizer inside
the raid-buff windows (vs the throughput-optimal sim, which fires WF ~on
cooldown at t≈0). Sim-running cases are marked `slow`.

Run from python/:  python -m pytest tests/test_canonical_sim.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.buff_windows import expected_windows, multiplier_intervals  # noqa: E402
from jobs.machinist import simulator as sim  # noqa: E402

# Scholar + Dragoon + RedMage = three real providers with staggered opener
# offsets, so the master windows ramp into a clear full-stack segment.
_COMP = ["Machinist", "Scholar", "Dragoon", "RedMage"]


def _intervals(duration: float):
    return multiplier_intervals(expected_windows(duration, _COMP))


def _full_stack_starts(bi):
    mx = max(m for _s, _e, m in bi)
    return [s for s, _e, m in bi if m >= mx - 1e-6]


def test_canonical_forbidden_holds_anchors_to_full_stack():
    bi = _intervals(300.0)
    assert bi, "3-provider comp should yield non-empty buff intervals"
    targets = sorted(round(s, 3) for s in _full_stack_starts(bi))
    fw = sim._canonical_burst_forbidden(bi)
    # Exactly Wildfire + Barrel Stabilizer are held — nothing else.
    assert {a for a, _s, _e in fw} == set(sim._CANONICAL_ALIGN_ANCHORS)
    for aid in sim._CANONICAL_ALIGN_ANCHORS:
        ends = sorted(round(e, 3) for a, _s, e in fw if a == aid)
        assert ends == targets, f"{aid} holds end at {ends}, want {targets}"
    # Each hold starts at max(0, target - hold) and is never negative.
    for _a, s, e in fw:
        assert s == max(0.0, e - sim._CANONICAL_HOLD_S)
        assert s >= 0.0
    # Hold span < cadence, so a burst on cooldown can't be blocked past the
    # *next* window.
    assert sim._CANONICAL_HOLD_S < 120.0


def test_canonical_forbidden_empty_without_buffs():
    assert sim._canonical_burst_forbidden([]) == ()


@pytest.mark.slow
def test_canonical_aligned_casts_burst_in_window():
    dur = 300.0
    bi = _intervals(dur)
    first_window = min(_full_stack_starts(bi))
    tl, _q = sim.simulate_canonical_aligned(dur, [], buff_intervals=bi)
    wf = sorted(t for t, a in tl if a == sim.WILDFIRE)
    bs = sorted(t for t, a in tl if a == sim.BARREL_STABILIZER)
    assert wf and bs, "canonical sim should still fire Wildfire + Barrel Stabilizer"
    # The opener burst lands at/after the full-stack window start — never the
    # t≈0 cooldown cast the throughput-optimal sim produces.
    assert wf[0] >= first_window - 1e-6, f"opener WF {wf[0]} fired before window {first_window}"
    assert bs[0] >= first_window - 1e-6, f"opener BStab {bs[0]} fired before window {first_window}"


@pytest.mark.slow
def test_canonical_falls_back_to_optimal_without_buffs():
    """Without buffs the canonical lane falls back to the refined-greedy line
    (`engine.perfect`); the production ceiling (`simulate_idealized_perfect`)
    runs the tool-ordering beam ON TOP of that refinement, so it may only be
    equal or better — never below the fallback."""
    from jobs._core.sim import engine
    from jobs.machinist.scoring import score_delivered_potency

    dur = 200.0
    aligned = sim.simulate_canonical_aligned(dur, [], buff_intervals=None)
    greedy = engine.perfect(sim._MODEL, sim._score, dur, [])
    assert aligned == greedy
    beam_tl, beam_aux = sim.simulate_idealized_perfect(dur, [])
    assert score_delivered_potency(beam_tl, beam_aux) >= \
        score_delivered_potency(aligned[0], aligned[1]) - 1e-6


def main() -> None:
    test_canonical_forbidden_holds_anchors_to_full_stack()
    test_canonical_forbidden_empty_without_buffs()
    test_canonical_aligned_casts_burst_in_window()
    test_canonical_falls_back_to_optimal_without_buffs()
    print("test_canonical_sim: OK")


if __name__ == "__main__":
    main()
