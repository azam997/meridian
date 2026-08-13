"""SGE mit-plan locked-GCD integration (network-free).

SGE inherits the base-model lock hooks (Eukrasian Prognosis II is
unconditionally castable — the AST pattern, unlike WHM's resource-gated lily
substitution). Pins the inherited path end-to-end on the job with MIXED
fixed-rate GCDs: a locked shield fires in its window, an uptime lock costs
about one Dosis, a downtime lock is free, and locked ceilings are monotone.

Run from python/:  python tests/test_sage_locks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.heal_locks import HealLockContext, LockedGcdWindow
from jobs.sage import data as gd
from jobs.sage import scoring as sc
from jobs.sage.simulator import simulate_idealized

HEAL = gd.EUKRASIAN_PROGNOSIS_II
_D = 300.0


def _lk(start, end, count, cast=2.0):
    return LockedGcdWindow(ability_id=HEAL, start_s=start, end_s=end,
                           count=count, cast_s=cast)


def _ctx(*locks):
    return HealLockContext(locks=tuple(locks))


def _score(timeline):
    return sc.score_delivered_potency(list(timeline))


def test_locked_heal_fires_in_window() -> None:
    tl, _ = simulate_idealized(_D, [], sim_context=_ctx(_lk(100.0, 130.0, 1)))
    heals = [t for t, a in tl if a == HEAL]
    assert len(heals) == 1 and 100.0 <= heals[0] < 130.0, heals


def test_uptime_lock_costs_about_one_filler() -> None:
    base_tl, _ = simulate_idealized(_D, [])
    lock_tl, _ = simulate_idealized(_D, [], sim_context=_ctx(_lk(100.0, 130.0, 1)))
    drop = _score(base_tl) - _score(lock_tl)
    # One shield GCD displaces about one Dosis (380p); the mixed-rate DoT
    # cadence can re-time around it, so allow a generous band.
    assert 100.0 <= drop <= 800.0, drop


def test_downtime_lock_is_free() -> None:
    downtime = [(100.0, 130.0)]
    base_tl, _ = simulate_idealized(_D, downtime)
    lock_tl, _ = simulate_idealized(_D, downtime,
                                    sim_context=_ctx(_lk(100.0, 128.0, 1)))
    heals = [t for t, a in lock_tl if a == HEAL]
    assert len(heals) == 1 and 100.0 <= heals[0] < 130.0, heals
    assert abs(_score(base_tl) - _score(lock_tl)) < 1e-6


def test_locked_ceiling_monotone() -> None:
    base_tl, _ = simulate_idealized(_D, [])
    one_tl, _ = simulate_idealized(_D, [], sim_context=_ctx(_lk(60.0, 90.0, 1)))
    two_tl, _ = simulate_idealized(_D, [], sim_context=_ctx(
        _lk(60.0, 90.0, 1), _lk(180.0, 210.0, 1)))
    assert _score(one_tl) <= _score(base_tl) + 1e-6
    assert _score(two_tl) <= _score(one_tl) + 1e-6


def main() -> None:
    test_locked_heal_fires_in_window()
    test_uptime_lock_costs_about_one_filler()
    test_downtime_lock_is_free()
    test_locked_ceiling_monotone()
    print("test_sage_locks: all checks passed")


if __name__ == "__main__":
    main()
