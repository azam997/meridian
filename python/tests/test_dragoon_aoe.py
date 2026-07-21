"""Dragoon AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the DRG beam forks into the dedicated
Doom Spike combo (Doom Spike / Draconian Fury -> Sonic Thrust -> Coerthan
Torment, full-to-all) as a whole-combo decision at combo boundaries, from
`_AOE_MIN_TARGETS` (= 3) targets. At N<=2 (or no schedule) it stays
byte-identical — the empirical crossover matches the closed form (the AoE
cycle's ~133xN per GCD + the faster Focus feed beats the ~368-430/GCD ST line
with its Chaotic Spring DoT only from 3 targets). Runs under pytest and
standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.dragoon import data as dd
from jobs.dragoon import scoring
from jobs.dragoon import simulator as sim

_DUR = 120.0
_AOE_IDS = {dd.DOOM_SPIKE, dd.DRACONIAN_FURY, dd.SONIC_THRUST, dd.COERTHAN_TORMENT}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def _score_at(timeline, aux, n: int) -> float:
    sched = ((0.0, _DUR, n),)
    return scoring._score_timeline(list(timeline), aux, None, None, sched)


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule produce the same timeline AND
    aux — the byte-identical guarantee; no AoE-combo GCD appears."""
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_two_targets_stays_single_target():
    """At 2 targets the ST line (with its splash-credited burst oGCDs) still
    beats the ~267/GCD AoE combo — the fork must not open."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    found = _ids(tl) & _AOE_IDS
    assert not found, f"2-target ceiling must stay single-target, found AoE: {found}"


def test_three_targets_swaps_and_wins():
    """At 3 targets the AoE combo engages AND out-scores the ST line scored at
    the same schedule (the crossover). The combo structure holds: every Doom
    Spike / Draconian Fury is followed by Sonic Thrust then Coerthan Torment."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 3),))
    tl, aux = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    assert ids & _AOE_IDS, "expected the Doom Spike combo at 3 targets"
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    assert _score_at(tl, aux, 3) > _score_at(tl_st, aux_st, 3)
    # Whole-combo completion: the GCD after a starter is Sonic Thrust, then
    # Coerthan Torment (oGCDs may weave between). The fight end may truncate the
    # final combo — squeezing a last starter before the kill is optimal, not a
    # broken chain — so only followers that exist are checked.
    gcds = [aid for _t, aid in tl if aid in dd.GCD_WEAPONSKILLS]
    for i, aid in enumerate(gcds):
        if aid in (dd.DOOM_SPIKE, dd.DRACONIAN_FURY):
            if i + 1 < len(gcds):
                assert gcds[i + 1] == dd.SONIC_THRUST, f"combo broken after starter @{i}"
            if i + 2 < len(gcds):
                assert gcds[i + 2] == dd.COERTHAN_TORMENT, f"combo broken mid-line @{i}"


def test_aoe_swap_never_worse_than_single_target():
    """For every N the AoE-aware ceiling scores >= the ST timeline scored at the
    same schedule — the MCH-audit invariant (an AoE swap that loses VALUE is the
    bug class this pins against)."""
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in range(2, 7):
        ctx = MultiTargetContext(schedule=((0.0, _DUR, n),))
        tl, aux = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        st_at_n = _score_at(tl_st, aux_st, n)
        aoe_at_n = _score_at(tl, aux, n)
        assert aoe_at_n >= st_at_n - 1e-6, (
            f"N={n}: AoE-sim {aoe_at_n:.0f} < ST-at-N {st_at_n:.0f}")


def test_high_n_ceiling_higher():
    """The 6-target ceiling clearly out-scores the single-target ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.5, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_stays_single_target()
    test_three_targets_swaps_and_wins()
    test_aoe_swap_never_worse_than_single_target()
    test_high_n_ceiling_higher()
    print("dragoon_aoe: all checks passed")


if __name__ == "__main__":
    main()
