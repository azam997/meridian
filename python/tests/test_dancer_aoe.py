"""Dancer AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the combo + procs swap to their AoE form
(Windmill / Bladeshower / Rising Windmill / Bloodshower / Fan Dance II), and the
cleaving burst (Saber Dance / Tillana / Dance of the Dawn / Fan Dance IV /
Technical Finish / Starfall) scales per-target via AOE_POTENCIES. Byte-identical
at N==1. Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.dancer import data as dd
from jobs.dancer import scoring
from jobs.dancer import simulator as sim

_DUR = 180.0
_AOE_IDS = {dd.WINDMILL, dd.BLADESHOWER, dd.RISING_WINDMILL, dd.BLOODSHOWER,
            dd.FAN_DANCE_II}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_high_n_uses_aoe():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl) & _AOE_IDS, "expected AoE buttons at 6 targets"


def test_high_n_ceiling_higher():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.3, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_high_n_uses_aoe()
    test_high_n_ceiling_higher()
    print("dancer_aoe: all checks passed")


if __name__ == "__main__":
    main()
