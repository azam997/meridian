"""Reaper AoE-rotation ceiling.

Under a multi-target `N(t)` schedule (a `MultiTargetContext` on `sim_context`)
the RPR ceiling swaps its single-target buttons for the gauge-equivalent AoE
line and scores every cast per-target. At N==1 (or no schedule) it stays
byte-identical to the pre-AoE single-target ceiling. Runs under pytest and
standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.reaper import data as rd
from jobs.reaper import scoring
from jobs.reaper import simulator as sim

_DUR = 180.0
_AOE_IDS = {
    rd.SPINNING_SCYTHE, rd.NIGHTMARE_SCYTHE, rd.GUILLOTINE, rd.EXEC_GUILLOTINE,
    rd.GRIM_SWATHE, rd.GRIM_REAPING, rd.LEMURES_SCYTHE, rd.SOUL_SCYTHE,
    rd.WHORL_OF_DEATH,
}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule both produce the ST timeline
    (no AoE buttons) — the byte-identical guarantee."""
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_grim_reaping_uses_fast_enshroud_gcd():
    """Grim Reaping is the AoE replacement for Void/Cross Reaping INSIDE Enshroud
    and shares their 1.5s recast, so it must get the fast Enshroud GCD. If it ran
    at the 2.5s base GCD the AoE Enshroud window drifts ~4s long and every later
    Enshroud slips back until the last one can't finish before the kill — the lost
    5th Communio (~7%) the 2026-06-23 N=3 audit flagged. Regression for that fix."""
    model = sim.ReaperRotationModel()
    state = model.init_state()
    state.enshrouded = True
    params = sim.SimParams()
    assert model.gcd_duration(state, rd.GRIM_REAPING, params) == sim.GCD_ENSHROUD_S
    assert model.gcd_duration(state, rd.VOID_REAPING, params) == sim.GCD_ENSHROUD_S
    # Outside Enshroud it is a normal-cadence GCD (defensive: the swap only ever
    # fires inside Enshroud, but the duration must not claim otherwise).
    state.enshrouded = False
    assert model.gcd_duration(state, rd.GRIM_REAPING, params) == sim.GCD_BASE_S


def test_aoe_swap_never_worse_than_single_target():
    """RPR's AoE buttons are gauge-equivalent, so the per-slot swap (deterministic
    `_maybe_aoe`) must never score below staying single-target at the same target
    count. Before the Grim Reaping cadence fix the N=3 AoE line lost ~7% to an
    un-finished Enshroud cycle; now the swap is never a regression and wins from its
    true crossover (N=3) up. N=2 keeps the ST line (the swap doesn't trigger)."""
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 4, 5, 6):
        sched = ((0.0, _DUR, n),)
        score_n = sim._make_score(sched)
        st_at_n = score_n(tl_st, aux_st, None)
        ctx = MultiTargetContext(schedule=sched)
        tl_aoe, aux_aoe = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        aoe_at_n = score_n(tl_aoe, aux_aoe, None)
        assert aoe_at_n >= st_at_n - 1e-6, (n, st_at_n, aoe_at_n)


def test_high_n_uses_aoe():
    """At 6 targets the ceiling rotation casts AoE buttons."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl) & _AOE_IDS, "expected AoE buttons in the 6-target ceiling"


def test_high_n_ceiling_higher():
    """The 6-target ceiling out-scores the single-target ceiling (cleave adds
    real potency across the rotation)."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.5, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_grim_reaping_uses_fast_enshroud_gcd()
    test_aoe_swap_never_worse_than_single_target()
    test_high_n_uses_aoe()
    test_high_n_ceiling_higher()
    print("reaper_aoe: all checks passed")


if __name__ == "__main__":
    main()
