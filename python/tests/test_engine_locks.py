"""Engine invariants for mit-plan locked-GCD windows
(jobs/_core/sim/engine.py lock scheduler + jobs/_core/heal_locks.py).

Exercised through a tiny toy model — a regression here is an engine bug, not
job data. The WHM integration is pinned by test_whitemage_sim.py.

Run from python/:  python tests/test_engine_locks.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.heal_locks import HealLockContext, LockedGcdWindow
from jobs._core.sim.engine import (
    BaseRotationModel,
    SimParamsBase,
    SimStateBase,
    beam_search,
    run_rotation,
)
from jobs._core.sim.timing import InstantGCD

FILLER = 10   # 100p damage GCD
HEAL = 20     # 0p locked heal GCD
HEAL_ALT = 21 # 0p substitute/satisfier heal

_POTENCY = {FILLER: 100.0}


def _score(timeline, aux, buff_intervals) -> float:
    return sum(_POTENCY.get(aid, 0.0) for _t, aid in timeline)


class LockToyModel(BaseRotationModel):
    """FILLER every GCD; locks supplied by the test. `voluntary_heal_at` makes
    the picker itself cast HEAL_ALT at the first slot >= that time (the
    voluntary-satisfier case)."""

    cooldowns = {}
    timing = InstantGCD(base_s=2.5)
    agnostic_anchors = ()
    buff_anchors = ()
    canonical_anchors = ()

    def __init__(self, locks=(), *, voluntary_heal_at: float | None = None,
                 satisfier_alt: bool = False):
        self.locked_gcd_windows = tuple(locks)
        self.voluntary_heal_at = voluntary_heal_at
        self.satisfier_alt = satisfier_alt
        self._voluntary_done = False

    def init_state(self) -> SimStateBase:
        return SimStateBase()

    def pick_gcd(self, state, params) -> int:
        if (self.voluntary_heal_at is not None and not self._voluntary_done
                and state.t >= self.voluntary_heal_at):
            self._voluntary_done = True
            return HEAL_ALT
        return FILLER

    def pick_ogcd(self, state, params):
        return None

    def apply_cast(self, state, ability_id) -> None:
        state.timeline.append((round(state.t, 6), ability_id))

    def lock_satisfiers(self, ability_id):
        if self.satisfier_alt and ability_id == HEAL:
            return frozenset((HEAL, HEAL_ALT))
        return frozenset((ability_id,))

    def sweep_params(self, extra_forbidden):
        yield SimParamsBase(forbidden_windows=extra_forbidden)


def _lk(aid, start, end, count, cast=2.5):
    return LockedGcdWindow(ability_id=aid, start_s=start, end_s=end,
                           count=count, cast_s=cast)


def _casts(timeline, aid):
    return [t for t, a in timeline if a == aid]


# --- Byte-identical without locks --------------------------------------------

def test_empty_locks_byte_identical() -> None:
    p = SimParamsBase()
    base, _ = run_rotation(LockToyModel(()), 30.0, [(7.0, 12.0)], p)
    also, _ = run_rotation(LockToyModel(()), 30.0, [(7.0, 12.0)], p)
    assert base == also
    assert all(a == FILLER for _t, a in base)
    bb, _ = beam_search(LockToyModel(()), _score, 30.0, [(7.0, 12.0)], p, 4)
    assert bb == base


# --- Quota + deadline ---------------------------------------------------------

def test_quota_honored_in_window() -> None:
    locks = (_lk(HEAL, 10.0, 25.0, 3),)
    tl, _ = run_rotation(LockToyModel(locks), 60.0, [], SimParamsBase())
    heals = _casts(tl, HEAL)
    assert len(heals) == 3, tl
    assert all(10.0 <= t < 25.0 for t in heals), heals
    # Every heal completes by the deadline.
    assert all(t + 2.5 <= 25.0 + 1e-9 for t in heals), heals


def test_lazy_placement_fires_late() -> None:
    """The greedy line holds damage GCDs until slack hits zero — the single
    locked heal lands in the last feasible slot before the deadline."""
    locks = (_lk(HEAL, 0.0, 20.0, 1),)
    tl, _ = run_rotation(LockToyModel(locks), 60.0, [], SimParamsBase())
    heals = _casts(tl, HEAL)
    assert len(heals) == 1
    # Slots at 0, 2.5, ..., the heal must fill the last one that completes by
    # 20.0 -> the 17.5 slot.
    assert abs(heals[0] - 17.5) < 1e-6, heals


def test_edf_forcing_overlapping_deadlines() -> None:
    locks = (_lk(HEAL, 0.0, 12.5, 2), _lk(HEAL_ALT, 0.0, 20.0, 2))
    tl, _ = run_rotation(LockToyModel(locks), 60.0, [], SimParamsBase())
    heals = _casts(tl, HEAL)
    alts = _casts(tl, HEAL_ALT)
    assert len(heals) == 2 and all(t + 2.5 <= 12.5 + 1e-9 for t in heals), tl
    assert len(alts) == 2 and all(t + 2.5 <= 20.0 + 1e-9 for t in alts), tl


# --- Downtime placement is free ------------------------------------------------

def test_lock_satisfied_in_downtime_is_free() -> None:
    """A lock window overlapping downtime is paid there — the damage-GCD count
    matches the no-lock run exactly."""
    downtime = [(10.0, 20.0)]
    locks = (_lk(HEAL, 8.0, 22.0, 2),)
    base, _ = run_rotation(LockToyModel(()), 40.0, downtime, SimParamsBase())
    tl, _ = run_rotation(LockToyModel(locks), 40.0, downtime, SimParamsBase())
    heals = _casts(tl, HEAL)
    assert len(heals) == 2
    assert all(10.0 <= t and t + 2.5 <= 20.0 + 1e-9 for t in heals), heals
    assert len(_casts(tl, FILLER)) == len(_casts(base, FILLER)), (tl, base)


# --- Beam consistency -----------------------------------------------------------

def test_beam_width1_equals_greedy_with_locks() -> None:
    locks = (_lk(HEAL, 10.0, 25.0, 2), _lk(HEAL_ALT, 30.0, 42.0, 1))
    p = SimParamsBase()
    greedy, _ = run_rotation(LockToyModel(locks), 60.0, [(30.0, 34.0)], p)
    beam, _ = beam_search(LockToyModel(locks), _score, 60.0, [(30.0, 34.0)], p, 1)
    # Width 1 explores the appended lock candidates too, but with 0-potency
    # heals the earliest-vs-latest placement ties on score — the quota, the
    # deadline safety and the damage-GCD count must match exactly.
    assert len(_casts(beam, HEAL)) == 2
    assert len(_casts(beam, HEAL_ALT)) == 1
    assert len(_casts(beam, FILLER)) == len(_casts(greedy, FILLER))
    assert _score(beam, 0, None) == _score(greedy, 0, None)


def test_beam_quota_and_score_vs_greedy() -> None:
    locks = (_lk(HEAL, 5.0, 30.0, 3),)
    p = SimParamsBase()
    greedy, _ = run_rotation(LockToyModel(locks), 60.0, [], p)
    beam, _ = beam_search(LockToyModel(locks), _score, 60.0, [], p, 8)
    assert len(_casts(beam, HEAL)) == 3
    assert _score(beam, 0, None) >= _score(greedy, 0, None)


# --- Voluntary satisfier retirement ---------------------------------------------

def test_voluntary_cast_retires_quota() -> None:
    """The model's own HEAL_ALT cast inside the window retires a HEAL lock via
    `lock_satisfiers` — no duplicate forced cast."""
    locks = (_lk(HEAL, 10.0, 30.0, 1),)
    model = LockToyModel(locks, voluntary_heal_at=12.0, satisfier_alt=True)
    tl, _ = run_rotation(model, 60.0, [], SimParamsBase())
    assert len(_casts(tl, HEAL_ALT)) == 1
    assert len(_casts(tl, HEAL)) == 0, tl


def test_without_satisfier_both_fire() -> None:
    locks = (_lk(HEAL, 10.0, 30.0, 1),)
    model = LockToyModel(locks, voluntary_heal_at=12.0, satisfier_alt=False)
    tl, _ = run_rotation(model, 60.0, [], SimParamsBase())
    assert len(_casts(tl, HEAL_ALT)) == 1
    assert len(_casts(tl, HEAL)) == 1, tl


# --- Context plumbing ------------------------------------------------------------

def test_heal_lock_context_hashable_and_falsy() -> None:
    empty = HealLockContext()
    assert not empty
    ctx = HealLockContext(locks=(_lk(HEAL, 0.0, 10.0, 1),), inner=None)
    assert ctx
    assert hash(ctx) != hash(HealLockContext(
        locks=(_lk(HEAL, 0.0, 10.0, 2),), inner=None))
    import pickle
    assert pickle.loads(pickle.dumps(ctx)) == ctx


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all engine-lock checks passed")


if __name__ == "__main__":
    main()
