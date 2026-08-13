"""Bard multi-target ceiling (AoE swap layer + shared-charge spender).

BRD's AoE layer: Burst Shot -> Ladonsbite (140 full-to-all beats 220 from
N>=2), Refulgent -> Shadowbite where it wins, Heartbreak Shot -> Rain of Death
(shared charge pool), with the cleaving burst riding SPLASH/AOE potencies.
What this file pins:

  * N==1 / no-schedule byte-identity (the universal AoE guard),
  * the Ladonsbite swap engaging at N>=2,
  * the never-worse invariant (the swap can only add value),
  * the high-N ceiling lift.

Run from python/:  python tests/test_bard_aoe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext
from jobs.bard import data as bd
from jobs.bard import scoring
from jobs.bard import simulator as sim

_DUR = 120.0
_AOE_IDS = {bd.LADONSBITE, bd.SHADOWBITE, bd.RAIN_OF_DEATH}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_two_targets_engages_ladonsbite():
    """140 full-to-all out-potencies the 220 Burst Shot from N=2 (280 vs 220)."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert bd.LADONSBITE in _ids(tl), "expected the Ladonsbite swap at N=2"


def test_aoe_swap_never_worse_than_single_target():
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 4, 5, 6):
        sched = ((0.0, _DUR, n),)
        ctx = MultiTargetContext(schedule=sched)
        tl_aoe, aux_aoe = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        s_aoe = scoring._score_timeline(list(tl_aoe), aux_aoe, None, None, sched)
        s_st = scoring._score_timeline(list(tl_st), aux_st, None, None, sched)
        assert s_aoe >= s_st, (n, s_aoe, s_st)


def test_high_n_ceiling_higher():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_engages_ladonsbite()
    test_aoe_swap_never_worse_than_single_target()
    test_high_n_ceiling_higher()
    print("bard_aoe: all checks passed")


if __name__ == "__main__":
    main()
