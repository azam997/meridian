"""Black Mage AoE-rotation ceiling (phase-divergent).

BLM has no beam — the deterministic phase machine itself forks. At N>=2 the fire
filler swaps Fire IV -> Flare (+3 Astral Soul; two Flares -> Flare Star), and the
gauge-equivalent spells take their higher-potency variant (Blizzard III/IV <->
High Blizzard II / Freeze, Xenoglossy <-> Foul). The crossovers differ on purpose:
the fire filler / Foul win from N==2, but the ice spells (interchangeable) only
swap to their AoE form at N>=3 (Blizzard III 290 > High Blizzard II 200 at N==2).
Byte-identical at N==1. Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.blackmage import data as bd
from jobs.blackmage import scoring
from jobs.blackmage import simulator as sim

_DUR = 180.0
_FIRE_AOE = {bd.FLARE, bd.FLARE_STAR, bd.FOUL}
_ICE_AOE = {bd.HIGH_BLIZZARD_II, bd.FREEZE}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    tl_none, _ = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    # No AoE-only buttons at a single target (Flare Star aside — it's a normal ST
    # cast that merely happens to cleave, so it appears in both).
    assert not (_ids(tl_none) & ({bd.FLARE, bd.FOUL} | _ICE_AOE))


def test_n2_fire_aoe_but_st_ice():
    """At N==2 the fire filler is Flare and Polyglot is Foul, but the
    interchangeable ice spells stay ST (Blizzard III/IV out-potency the AoE pair)."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    assert bd.FLARE in ids and bd.FOUL in ids, "fire AoE + Foul expected at N==2"
    assert not (ids & _ICE_AOE), "ice should stay ST at N==2"


def test_n6_full_aoe():
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    assert _FIRE_AOE & ids, "fire AoE at 6 targets"
    assert _ICE_AOE & ids, "ice AoE (High Blizzard II / Freeze) at 6 targets"


def test_high_n_ceiling_higher():
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.3, (st, aoe)


def main() -> None:
    test_single_target_byte_identical()
    test_n2_fire_aoe_but_st_ice()
    test_n6_full_aoe()
    test_high_n_ceiling_higher()
    print("blackmage_aoe: all checks passed")


if __name__ == "__main__":
    main()
