"""Summoner multi-target ceiling (the AoE swap layer + short-slot weave cap).

SMN swaps fillers/gem GCDs to their AoE forms under a multi-target schedule
(`_AOE_SWAP`, with `_AOE_TO_BASE` folding transitions back) and caps the 1.5s
Emerald slots at one weave (`_WEAVE_CAP` — the pattern the engine's
weave_capacity generalized). Pins the universal guards: N==1 byte-identity, a
swap id appearing once the schedule engages, the never-worse invariant, and
the high-N ceiling lift.

Run from python/:  python tests/test_summoner_aoe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext
from jobs.summoner import scoring
from jobs.summoner import simulator as sim

_DUR = 120.0
_SWAP_IDS = set(sim._AOE_SWAP.values())


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _SWAP_IDS)


def test_high_n_engages_the_swap_layer():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 4),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl) & _SWAP_IDS, "no AoE swap engaged at N=4"


def test_aoe_never_worse_than_st():
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 4, 6):
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
    test_high_n_engages_the_swap_layer()
    test_aoe_never_worse_than_st()
    test_high_n_ceiling_higher()
    print("summoner_aoe: all checks passed")


if __name__ == "__main__":
    main()
