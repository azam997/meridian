"""Unit tests for the GCD-timing archetype presets (jobs/_core/sim/timing.py).

`InstantGCD` is what MCH + RPR use; `HardcastGCD` is the caster-ready preset
(built ahead of the first caster sim). These pin the two shapes:

  * Instant — flat slot, full weave budget regardless of ability.
  * Hardcast — slot = max(cast_time, recast); a hardcast leaves fewer weaves
    than an instant; both clamp to the param sweep's max_weaves_per_gcd.

Run from python/:  python tests/test_gcd_timing.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim.engine import SimParamsBase
from jobs._core.sim.timing import HardcastGCD, InstantGCD


# Ability ids for the toy spell book.
INSTANT_PROC = 1
SHORT_CAST = 2     # cast < recast
LONG_CAST = 3      # cast > recast


def _params(max_weaves: int = 2) -> SimParamsBase:
    return SimParamsBase(max_weaves_per_gcd=max_weaves)


def test_instant_flat_duration_and_full_weaves() -> None:
    timing = InstantGCD(base_s=2.5)
    p = _params(2)
    # Duration is the flat recast for every ability.
    assert timing.duration(None, INSTANT_PROC, p) == 2.5
    assert timing.duration(None, LONG_CAST, p) == 2.5
    # Weave budget is exactly the param cap (this is what keeps MCH/RPR
    # byte-identical).
    assert timing.weave_budget(None, INSTANT_PROC, p) == 2
    assert timing.weave_budget(None, INSTANT_PROC, _params(3)) == 3


def test_hardcast_slot_is_max_of_cast_and_recast() -> None:
    timing = HardcastGCD(
        gcd_recast_s=2.5,
        cast_times={SHORT_CAST: 1.5, LONG_CAST: 3.5},
    )
    p = _params(2)
    # A cast shorter than the recast doesn't shorten the slot.
    assert timing.duration(None, SHORT_CAST, p) == 2.5
    # A cast longer than the recast locks you for the whole cast.
    assert timing.duration(None, LONG_CAST, p) == 3.5
    # An ability absent from the cast-time map is instant -> recast.
    assert timing.duration(None, INSTANT_PROC, p) == 2.5


def test_hardcast_weave_budget_smaller_than_instant() -> None:
    timing = HardcastGCD(
        gcd_recast_s=2.5,
        cast_times={LONG_CAST: 3.5},
        instant_weaves=2,
        hardcast_weaves=1,
    )
    p = _params(2)
    # Instant proc gets the full instant budget; a hardcast only the slidecast
    # tail's worth.
    assert timing.weave_budget(None, INSTANT_PROC, p) == 2
    assert timing.weave_budget(None, LONG_CAST, p) == 1
    # Both clamp to a tighter param cap.
    assert timing.weave_budget(None, INSTANT_PROC, _params(1)) == 1


def main() -> None:
    test_instant_flat_duration_and_full_weaves()
    test_hardcast_slot_is_max_of_cast_and_recast()
    test_hardcast_weave_budget_smaller_than_instant()
    print("test_gcd_timing: all checks passed")


if __name__ == "__main__":
    main()
