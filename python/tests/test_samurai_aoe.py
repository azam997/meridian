"""Samurai AoE-rotation ceiling (beam fork).

SAM's AoE is Sen-divergent: the 2-step AoE combo (Fuko -> Mangetsu/Oka) builds
only Getsu + Ka, so the AoE Iaijutsu is the 2-Sen Tenka/Tendo Goken (never the
3-Sen Midare). At N>=2 the beam forks the AoE line in via `gcd_candidates`; the
exact DP is skipped (its single-target admissible bound can't dominate AoE), so
the diverse beam holds the multi-target ceiling. Byte-identical at N==1. Runs
under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.samurai import data as sd
from jobs.samurai import scoring
from jobs.samurai import simulator as sim

_DUR = 180.0
_AOE_IDS = {sd.FUKO, sd.MANGETSU, sd.OKA, sd.TENKA_GOKEN, sd.TENDO_GOKEN,
            sd.KAESHI_GOKEN, sd.TENDO_KAESHI_GOKEN}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert not (_ids(tl_none) & _AOE_IDS), "no AoE buttons at a single target"


def test_two_targets_stays_single_target():
    """At 2 targets SAM keeps its full single-target rotation — none of the AoE line
    appears. The AoE combo (Fuko -> Mangetsu/Oka) builds a Sen a GCD quicker and
    dumps it as the 2-Sen Tenka/Tendo Goken, but at N=2 banking the 2 Sen toward a
    3-Sen Midare Setsugekka (680p) still wins; SAM's AoE crossover is 3
    (`_AOE_MIN_TARGETS`). Regression for the 2026-06-23 audit's premature-swap
    finding (the beam was being offered the losing AoE candidates and pruning the
    correct ST line)."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    found = _ids(tl) & _AOE_IDS
    assert not found, f"2-target ceiling must stay single-target, found AoE: {found}"


def test_three_targets_swaps_and_wins():
    """At 3 targets the AoE line (Fuko combo + 2-Sen Goken) appears AND out-scores
    staying single-target at 3 targets — the genuine crossover."""
    sched = ((0.0, _DUR, 3),)
    ctx = MultiTargetContext(schedule=sched)
    tl_aoe, aux_aoe = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl_aoe) & _AOE_IDS, "expected the AoE line at 3 targets"
    score_n = sim._make_score(sched)
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    assert score_n(tl_aoe, aux_aoe, None) > score_n(tl_st, aux_st, None)


def test_high_n_uses_aoe():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl) & _AOE_IDS, "expected the AoE combo / Goken at 6 targets"


def test_high_n_ceiling_higher():
    """The AoE line out-potencies the ST line at 6 targets -> higher ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.3, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_stays_single_target()
    test_three_targets_swaps_and_wins()
    test_high_n_uses_aoe()
    test_high_n_ceiling_higher()
    print("samurai_aoe: all checks passed")


if __name__ == "__main__":
    main()
