"""Viper multi-target ceiling (free-splash model).

VPR models multi-target as free splash on the ST rotation only (the dedicated
AoE line — Vicepit / the Maws — is a documented deferral, so its casts score
their splash credit through `splash_potencies` while the rotation itself is
unchanged at every N). Pins the universal guards: N==1 byte-identity, splash
credit raising the ceiling at high N, and the never-worse invariant.

Run from python/:  python tests/test_viper_aoe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext
from jobs.viper import scoring
from jobs.viper import simulator as sim

_DUR = 120.0


def test_single_target_byte_identical():
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1


def test_splash_raises_high_n_ceiling():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st, (st, aoe)


def test_mt_never_worse_than_st():
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    for n in (2, 3, 6):
        sched = ((0.0, _DUR, n),)
        ctx = MultiTargetContext(schedule=sched)
        tl_mt, aux_mt = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        score = sim._make_score(sched)
        assert score(list(tl_mt), aux_mt, None) \
            >= score(list(tl_st), aux_st, None), n


def main() -> None:
    test_single_target_byte_identical()
    test_splash_raises_high_n_ceiling()
    test_mt_never_worse_than_st()
    print("viper_aoe: all checks passed")


if __name__ == "__main__":
    main()
