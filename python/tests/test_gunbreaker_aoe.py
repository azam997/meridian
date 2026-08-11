"""Gunbreaker AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the GNB beam swaps to the dedicated AoE
line — Demon Slice -> Demon Slaughter (cartridge parity with Solid Barrel) and
the Fated Circle spender with its forced Fated Brand continuation — while the
innately-cleaving ST casts (Double Down / the Reign chain / Bow Shock) get
free-splash via `potency_for`. At N==1 (or no schedule) it stays byte-identical.
Pins the `_AOE_MIN_TARGETS = 3` crossover gate and the swap-never-worse
invariant. Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.gunbreaker import data as gd
from jobs.gunbreaker import scoring
from jobs.gunbreaker import simulator as sim

_DUR = 120.0
_AOE_IDS = {gd.DEMON_SLICE, gd.DEMON_SLAUGHTER, gd.FATED_CIRCLE, gd.FATED_BRAND}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule produce the same timeline and
    aux — the byte-identical guarantee — with none of the AoE line present."""
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_two_targets_stays_single_target():
    """At 2 targets GNB keeps the single-target line (`_AOE_MIN_TARGETS = 3`):
    the ST combo out-values Demon Slice's raw per-target potency at 2 because
    cartridge VALUE routes through Gnashing/Burst continuations."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    found = _ids(tl) & _AOE_IDS
    assert not found, f"2-target ceiling must stay single-target, found AoE: {found}"


def test_three_targets_engages_aoe_line():
    """At N>=3 the AoE combo engages: Demon Slice/Slaughter builders, the Fated
    Circle spender, and its forced Fated Brand continuation all appear."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 3),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    for aid, name in ((gd.DEMON_SLICE, "Demon Slice"),
                      (gd.DEMON_SLAUGHTER, "Demon Slaughter"),
                      (gd.FATED_CIRCLE, "Fated Circle"),
                      (gd.FATED_BRAND, "Fated Brand")):
        assert aid in ids, f"expected {name} in the 3-target ceiling"


def test_aoe_swap_never_worse_than_single_target():
    """For N 2..6 the AoE-aware sim's timeline never scores below the
    single-target timeline evaluated at the same schedule — the cross-job audit
    invariant (an AoE swap that loses value is the MCH-Auto-Crossbow bug class)."""
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 4, 5, 6):
        sched = ((0.0, _DUR, n),)
        ctx = MultiTargetContext(schedule=sched)
        tl_aoe, aux_aoe = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        s_aoe = scoring._score_timeline(list(tl_aoe), aux_aoe, None, None, sched)
        s_st = scoring._score_timeline(list(tl_st), aux_st, None, None, sched)
        assert s_aoe >= s_st, (n, s_aoe, s_st)


def test_high_n_ceiling_higher():
    """The 6-target ceiling out-scores the single-target ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.5, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_stays_single_target()
    test_three_targets_engages_aoe_line()
    test_aoe_swap_never_worse_than_single_target()
    test_high_n_ceiling_higher()
    print("gunbreaker_aoe: all checks passed")


if __name__ == "__main__":
    main()
