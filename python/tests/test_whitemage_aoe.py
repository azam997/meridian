"""White Mage AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the WHM ceiling swaps its Glare III filler
for Holy III (gauge-free, closed-form) and scores the cleaving burst (Glare IV /
Assize / Misery, already in splash) per-target. Byte-identical at N==1. Runs
under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.whitemage import data as wd
from jobs.whitemage import scoring
from jobs.whitemage import simulator as sim

_DUR = 180.0


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert wd.HOLY_III not in _ids(tl_none)


def test_high_n_uses_holy():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert wd.HOLY_III in _ids(tl), "expected Holy III in the 6-target ceiling"


def test_high_n_ceiling_higher():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.3, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_high_n_uses_holy()
    test_high_n_ceiling_higher()
    print("whitemage_aoe: all checks passed")


if __name__ == "__main__":
    main()
