"""Machinist AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the MCH beam swaps in the AoE line — Auto
Crossbow / Bioblaster (gauge-equivalent) and the heat-DIVERGENT Scattergun fork
(its extra heat cascades into more Hypercharge / Auto Crossbow, which the beam
searches). At N==1 (or no schedule) it stays byte-identical, Queen battery and
all. Runs under pytest and standalone.
"""
from __future__ import annotations

from jobs._core.downtime_sources import MultiTargetContext
from jobs.machinist import data as md
from jobs.machinist import scoring
from jobs.machinist import simulator as sim

_DUR = 180.0
_AOE_IDS = {md.SCATTERGUN_ABILITY_ID, md.AUTO_CROSSBOW_ABILITY_ID,
            md.BIOBLASTER_ABILITY_ID}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule produce the same timeline AND
    the same Queen battery — the byte-identical guarantee."""
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    ctx_n1 = MultiTargetContext(schedule=((0.0, _DUR, 1),))
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx_n1)
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_two_targets_stays_single_target():
    """At 2 targets MCH keeps its SINGLE-TARGET rotation — none of the AoE line
    appears. Auto Crossbow / Scattergun out-potency the ST line per cast at 2
    targets, but lose on VALUE: only Blazing Shot grants the DC/CM CDR and only the
    Heated combo banks battery (-> Queen). (Regression test for the reported
    2-target AoE bug.)"""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 2),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    found = _ids(tl) & _AOE_IDS
    assert not found, f"2-target ceiling must stay single-target, found AoE: {found}"


def test_three_to_five_targets_is_hybrid():
    """At 3-5 targets MCH runs the HYBRID: Scattergun replaces the Heated combo
    (filler crossover at 3), but the Hypercharge spender stays Blazing Shot — NOT
    Auto Crossbow. Auto Crossbow's per-target potency (180n) loses to Blazing Shot
    plus its Double Check / Checkmate CDR, whose value scales with N because those
    oGCDs cleave, until n=6. Bioblaster stays Drill (until 7). (Regression test for
    the audit's N>=3 Auto-Crossbow-too-early finding.)"""
    for n in (3, 4, 5):
        ctx = MultiTargetContext(schedule=((0.0, _DUR, n),))
        tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
        ids = _ids(tl)
        assert md.SCATTERGUN_ABILITY_ID in ids, f"expected Scattergun at {n} targets"
        assert md.AUTO_CROSSBOW_ABILITY_ID not in ids, \
            f"Auto Crossbow must NOT appear at {n} targets (Blazing Shot's DC/CM " \
            f"CDR wins until 6)"
        assert md.BIOBLASTER_ABILITY_ID not in ids, f"Drill still beats Bioblaster at {n}"


def test_six_targets_uses_auto_crossbow():
    """At 6 targets Auto Crossbow's per-target potency finally overtakes Blazing
    Shot + its (cleaving) DC/CM CDR, so the Hypercharge spender swaps too."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    ids = _ids(tl)
    assert md.AUTO_CROSSBOW_ABILITY_ID in ids, "expected Auto Crossbow at 6 targets"
    assert md.SCATTERGUN_ABILITY_ID in ids, "expected Scattergun at 6 targets"


def test_high_n_uses_aoe():
    """At 6 targets the ceiling rotation casts AoE buttons."""
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=ctx)
    assert _ids(tl) & _AOE_IDS, "expected AoE buttons in the 6-target ceiling"


def test_high_n_ceiling_higher():
    """The 6-target ceiling out-scores the single-target ceiling."""
    st = scoring.idealized_at_duration(_DUR, [])
    ctx = MultiTargetContext(schedule=((0.0, _DUR, 6),))
    aoe = scoring.idealized_at_duration(_DUR, [], sim_context=ctx)
    assert aoe > st * 1.5, (st, aoe)


def test_observed_reach_cap_binds_ceiling():
    """The observed-reach cap rides the context into the scorer: holding
    Scattergun (a front cone) at an observed reach of 2 lowers the 3-target
    ceiling — the fix for crediting cone buttons as if they reached spread
    targets — but never below the single-target ceiling. An uncapped context
    is untouched (same score as before the caps existed)."""
    st = scoring.idealized_at_duration(_DUR, [])
    sched = ((0.0, _DUR, 3),)
    uncapped = scoring.idealized_at_duration(
        _DUR, [], sim_context=MultiTargetContext(schedule=sched))
    capped = scoring.idealized_at_duration(
        _DUR, [], sim_context=MultiTargetContext(
            schedule=sched,
            ability_caps=((md.SCATTERGUN_ABILITY_ID, 2),)))
    assert capped < uncapped, (capped, uncapped)
    assert capped > st, (capped, st)


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_stays_single_target()
    test_three_to_five_targets_is_hybrid()
    test_six_targets_uses_auto_crossbow()
    test_high_n_uses_aoe()
    test_high_n_ceiling_higher()
    test_observed_reach_cap_binds_ceiling()
    print("machinist_aoe: all checks passed")


if __name__ == "__main__":
    main()
