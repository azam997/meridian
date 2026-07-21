"""Paladin AoE-rotation ceiling.

Under a multi-target `N(t)` schedule PLD's cleaving rotation abilities — the
magical Confiteor combo (Confiteor / Blade of Faith / Truth / Valor), Imperator,
Circle of Scorn, Expiacion — scale per-target (via AOE_POTENCIES), and the Divine
Might spend swaps Holy Spirit -> Holy Circle at high N. Byte-identical at N==1.
(The ST-vs-AoE *filler combo* fork is a deferred refinement; until then a
very-high-N pull disclaims rather than ever showing >100%.) Runs under pytest and
standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.paladin import data as pd
from jobs.paladin import scoring
from jobs.paladin import simulator as sim

_DUR = 180.0


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert pd.HOLY_CIRCLE not in _ids(tl_none)


def test_high_n_ceiling_higher():
    """The cleaving magic combo / oGCDs scale per-target -> a higher ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.2, (st, aoe)


def test_holy_circle_at_high_n():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 8),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert pd.HOLY_CIRCLE in _ids(tl), "expected Holy Circle at 8 targets"


def main() -> None:
    test_single_target_byte_identical()
    test_high_n_ceiling_higher()
    test_holy_circle_at_high_n()
    print("paladin_aoe: all checks passed")


if __name__ == "__main__":
    main()
