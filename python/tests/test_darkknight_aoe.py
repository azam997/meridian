"""Dark Knight multi-target ceiling (AoE line + free splash).

DRK ships both maps: the dedicated AoE line (Unleash combo etc., engaging at
`_AOE_MIN_TARGETS = 3`) and free splash on the ST burst. Pins the universal
guards: N==1 byte-identity, the AoE line engaging at N>=3 (the timeline
diverges from ST), the never-worse invariant, and the high-N ceiling lift.

Run from python/:  python tests/test_darkknight_aoe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext
from jobs.darkknight import scoring
from jobs.darkknight import simulator as sim

_DUR = 120.0


def test_single_target_byte_identical():
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1


def test_three_targets_diverges_from_st():
    """At N>=3 the dedicated AoE line engages — the rotation must change."""
    tl_st, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 3),))
    tl_aoe, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert list(tl_aoe) != list(tl_st), "AoE line did not engage at N=3"


def test_aoe_never_worse_than_st():
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
    test_three_targets_diverges_from_st()
    test_aoe_never_worse_than_st()
    test_high_n_ceiling_higher()
    print("darkknight_aoe: all checks passed")


if __name__ == "__main__":
    main()
