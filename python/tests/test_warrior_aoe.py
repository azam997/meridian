"""Warrior AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the WAR ceiling swaps in the AoE line — the
2-step Overpower -> Mythril Tempest combo (Mythril Tempest maintains Surging
Tempest, like Storm's Eye) plus Decimate / Chaotic Cyclone spenders and Orogeny.
Byte-identical at N==1. Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.warrior import data as wd
from jobs.warrior import scoring
from jobs.warrior import simulator as sim

_DUR = 180.0
_AOE_IDS = {wd.OVERPOWER, wd.MYTHRIL_TEMPEST, wd.DECIMATE, wd.CHAOTIC_CYCLONE,
            wd.OROGENY}


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
    assert _ids(tl) & _AOE_IDS, "expected AoE buttons in the 6-target ceiling"


def test_surging_tempest_maintained_in_aoe():
    """The AoE rotation still keeps Surging Tempest up — Mythril Tempest (the AoE
    combo finisher) or Storm's Eye must appear; the coverage overlay assumes it."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    assert wd.MYTHRIL_TEMPEST in ids or wd.STORMS_EYE in ids


def test_high_n_ceiling_higher():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.2, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_high_n_uses_aoe()
    test_surging_tempest_maintained_in_aoe()
    test_high_n_ceiling_higher()
    print("warrior_aoe: all checks passed")


if __name__ == "__main__":
    main()
