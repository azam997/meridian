"""Slot-class weave capacity + weave-loop guards (jobs/_core/sim/engine.py).

The v1.1 weave model: capacity is a physical property of the slot's length
relative to the job's standard GCD — ~1.0s slots fit no weave (DNC steps,
SGE Eukrasia, NIN mudras), ~1.5s slots exactly one (MCH Overheat, RPR
Enshroud, ninjutsu, RDM enchanted melee, SMN Emerald), standard slots the
full swept budget. Ratio-based, so neither the sub-GCD sweep nor haste can
flip a slot's class. The engine min()s capacity with the swept budget, the
borderline time-gate tie is epsilon-resolved to DROPPED, and no weave starts
after the fight ends. Capacity is never raised without FFLogs clip-proof.

Run from python/:  python tests/test_weave_capacity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim.engine import (
    BaseRotationModel,
    SimParamsBase,
    SimStateBase,
    run_rotation,
)
from jobs._core.sim.timing import HardcastGCD, InstantGCD

GCD_A = 1
OGCD_X = 99


def _params(max_weaves: int = 2) -> SimParamsBase:
    return SimParamsBase(max_weaves_per_gcd=max_weaves)


class ToyModel(BaseRotationModel):
    """Minimal model: one GCD on a fixed slot length, an oGCD always ready."""
    cooldowns: dict[int, tuple[float, int]] = {}
    timing = InstantGCD(base_s=2.5)

    def __init__(self, slot_s: float, base_s: float = 2.5) -> None:
        self.slot_s = slot_s
        self.timing = InstantGCD(base_s=base_s)

    def init_state(self) -> SimStateBase:
        return SimStateBase()

    def pick_gcd(self, state, params) -> int:
        return GCD_A

    def gcd_duration(self, state, gcd_id, params) -> float:
        return self.slot_s

    def pick_ogcd(self, state, params):
        return OGCD_X

    def apply_cast(self, state, ability_id) -> None:
        state.timeline.append((round(state.t, 4), ability_id))


def _weaves_per_gcd(timeline) -> list[int]:
    """oGCD count following each GCD, in slot order."""
    out: list[int] = []
    for _t, aid in timeline:
        if aid == GCD_A:
            out.append(0)
        elif out:
            out[-1] += 1
    return out


def test_capacity_table() -> None:
    p = _params(2)
    m = ToyModel(slot_s=2.5)
    state = SimStateBase()
    # ~1.0s slot (r=0.4): no weave. ~1.5s (r=0.6): one. 2.0s (r=0.8): full.
    assert m.weave_capacity(state, GCD_A, 1.0, p) == 0
    assert m.weave_capacity(state, GCD_A, 1.5, p) == 1
    assert m.weave_capacity(state, GCD_A, 2.0, p) == 2
    assert m.weave_capacity(state, GCD_A, 2.2, p) == 2
    assert m.weave_capacity(state, GCD_A, 2.5, p) == 2
    # Longer-than-base (hardcast) slots keep the full budget.
    assert m.weave_capacity(state, GCD_A, 3.5, p) == 2


def test_capacity_is_sweep_invariant() -> None:
    """Scaling base and slot together (the sub-GCD sweep) keeps the class:
    1.482/2.47 == 1.5/2.5, so the fast band point no longer flips capacity."""
    p = _params(2)
    nominal = ToyModel(slot_s=1.5, base_s=2.5)
    fast = ToyModel(slot_s=1.482, base_s=2.47)
    s = SimStateBase()
    assert nominal.weave_capacity(s, GCD_A, 1.5, p) \
        == fast.weave_capacity(s, GCD_A, 1.482, p) == 1


def test_capacity_reads_hardcast_base() -> None:
    m = ToyModel(slot_s=2.5)
    m.timing = HardcastGCD(gcd_recast_s=2.5, cast_times={})
    s = SimStateBase()
    p = _params(2)
    assert m.weave_capacity(s, GCD_A, 1.5, p) == 1
    assert m.weave_capacity(s, GCD_A, 2.5, p) == 2


def test_engine_enforces_capacity_in_slot() -> None:
    """End-to-end through `_commit_gcd`: a 1.5s slot weaves exactly once, a
    1.0s slot never, a 2.5s slot twice — with an oGCD always available."""
    for slot_s, expect in ((1.0, 0), (1.5, 1), (2.5, 2)):
        tl, _aux = run_rotation(ToyModel(slot_s=slot_s), 20.0, None, _params(2))
        counts = _weaves_per_gcd(tl)
        # Ignore the final slot (fight-end guard may trim it).
        body = counts[:-1] or counts
        assert body and all(c == expect for c in body), \
            f"slot {slot_s}: weave counts {counts}, expected {expect}"


def test_no_weave_after_fight_end() -> None:
    """No cast — GCD or weave — starts at/after fight end."""
    for slot_s in (1.5, 2.5):
        dur = 10.3
        tl, _aux = run_rotation(ToyModel(slot_s=slot_s), dur, None, _params(2))
        assert tl, "empty timeline"
        assert all(t < dur for t, _aid in tl), \
            f"cast at/after fight end: {[x for x in tl if x[0] >= dur]}"


def test_repeat_runs_bit_identical() -> None:
    m1 = ToyModel(slot_s=1.5)
    m2 = ToyModel(slot_s=1.5)
    tl1, _ = run_rotation(m1, 120.0, [(30.0, 40.0)], _params(2))
    tl2, _ = run_rotation(m2, 120.0, [(30.0, 40.0)], _params(2))
    assert tl1 == tl2


def main() -> None:
    test_capacity_table()
    test_capacity_is_sweep_invariant()
    test_capacity_reads_hardcast_base()
    test_engine_enforces_capacity_in_slot()
    test_no_weave_after_fight_end()
    test_repeat_runs_bit_identical()
    print("test_weave_capacity: all checks passed")


if __name__ == "__main__":
    main()
