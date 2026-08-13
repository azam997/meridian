"""Monk multi-target ceiling (free-splash model + target-aware beam).

MNK has no dedicated AoE-line swap in the sim — its multi-target value is the
free splash on the blitzes/replies (Wind's/Fire's Reply, Elixir Burst, Rising
Phoenix, Phantom Rush via `splash_potencies`). What this file pins:

  * N==1 / no-schedule byte-identity (the universal AoE guard),
  * splash credit raising the ceiling at high N,
  * the v1.1 fix: the beam OBJECTIVE is bound to the N(t) schedule
    (`_make_score(_schedule_of(ctx))`) — before, the beam optimized a pure
    single-target objective and the winner was then scored WITH splash, so the
    cleave never influenced which line was chosen. The never-worse invariant
    below fails under a target-blind beam whenever the schedule-aware search
    finds a better line.

Run from python/:  python tests/test_monk_aoe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext
from jobs.monk import data as md
from jobs.monk import scoring
from jobs.monk import simulator as sim

_DUR = 120.0


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule produce the same timeline."""
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1


def test_schedule_reaches_the_beam_objective():
    """`_schedule_of` peels the MultiTargetContext (incl. under a
    CeilingContext) and `_make_score` prices splash — the two halves of the
    target-aware objective."""
    sched = ((0.0, _DUR, 3),)
    ctx = MultiTargetContext(schedule=sched)
    assert sim._schedule_of(ctx) == sched

    score_st = sim._make_score(())
    score_mt = sim._make_score(sched)
    tl = [(10.0, md.PHANTOM_RUSH)]
    assert score_mt(tl, 0, None) > score_st(tl, 0, None), \
        "splash-credited Phantom Rush must out-score its ST value at N=3"


def test_splash_raises_high_n_ceiling():
    """The 6-target ceiling out-scores single-target (free splash only, so the
    lift is modest — but it must exist)."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st, (st, aoe)


def test_mt_beam_never_worse_than_st_line():
    """For N 2..6 the schedule-aware ceiling never scores below the
    single-target timeline evaluated at the same schedule — fails under a
    target-blind beam whenever the aware search finds a better line."""
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 4, 5, 6):
        sched = ((0.0, _DUR, n),)
        ctx = MultiTargetContext(schedule=sched)
        tl_mt, aux_mt = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        score = sim._make_score(sched)
        assert score(list(tl_mt), aux_mt, None) >= score(list(tl_st), aux_st, None), n


def main() -> None:
    test_single_target_byte_identical()
    test_schedule_reaches_the_beam_objective()
    test_splash_raises_high_n_ceiling()
    test_mt_beam_never_worse_than_st_line()
    print("monk_aoe: all checks passed")


if __name__ == "__main__":
    main()
