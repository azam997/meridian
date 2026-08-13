"""AST mit-plan locked-GCD integration (network-free).

AST is the documented reference for the INHERITED lock hooks: Helios
Conjunction is unconditionally castable, so the base-model identity path
(`resolve_locked_gcd` returns the id, `lock_satisfiers` is the singleton) is
the whole story — unlike WHM's resource-gated lily substitution. This pins
that inherited path end-to-end:

  * a locked Helios Conjunction fires inside its window,
  * an uptime lock lowers the ceiling by about one filler Fall Malefic,
  * a lock fully inside downtime is free,
  * locked ceilings are monotone (locked <= unlocked, more locks <= fewer).

Run from python/:  python tests/test_astrologian_locks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.heal_locks import HealLockContext, LockedGcdWindow
from jobs.astrologian import data as ad
from jobs.astrologian import scoring as sc
from jobs.astrologian.simulator import simulate_idealized

HEAL = ad.HELIOS_CONJUNCTION
_D = 300.0


def _lk(start, end, count, cast=1.5):
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
    # One heal GCD displaces about one Fall Malefic (270p); allow for
    # Combust/oGCD re-timing around the displaced slot.
    assert 100.0 <= drop <= 600.0, drop


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
    print("test_astrologian_locks: all checks passed")


if __name__ == "__main__":
    main()
