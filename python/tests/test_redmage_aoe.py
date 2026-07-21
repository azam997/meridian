"""Red Mage AoE ceiling (minimal-safe).

Under a multi-target `N(t)` schedule the cleaving finisher chain (Scorch /
Resolution / Verflare / Verholy / Grand Impact) + Contre Sixte scale per-target
via AOE_POTENCIES (55% falloff; Contre Sixte full-to-all). Byte-identical at
N==1. (The dedicated AoE filler / Enchanted Moulinet combo are a deferred
refinement — until then a very-high-N pull disclaims rather than showing >100%.)
Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.redmage import scoring
from jobs.redmage import simulator as sim

_DUR = 180.0


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1


def test_high_n_ceiling_higher():
    """The cleaving finisher chain + Contre Sixte scale per-target -> higher ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.2, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_high_n_ceiling_higher()
    print("redmage_aoe: all checks passed")


if __name__ == "__main__":
    main()
