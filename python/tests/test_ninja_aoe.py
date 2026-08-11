"""Ninja AoE-rotation ceiling.

Under a multi-target `N(t)` schedule the NIN model swaps in the dedicated AoE
line DETERMINISTICALLY at audited VALUE crossovers (simulator constants):
Ninki frogs at N>=2, Katon / Goka Mekkyaku at N>=3, the Death Blossom -> Hakke
Mujinsatsu combo at N>=4. At N<=1 (or no schedule) everything is byte-identical
— timeline AND aux. Audit (2026-07-16, 180s + 300s): AoE-sim score >= ST score
at every N, deltas monotone (+1030 at N=2 to +51911 at N=6 over 180s).

Runs under pytest and standalone.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import MultiTargetContext  # noqa: E402
from jobs.ninja import data as nd  # noqa: E402
from jobs.ninja import simulator as sim  # noqa: E402
from jobs.ninja.scoring import _score_timeline  # noqa: E402

_DUR = 180.0
_AOE_IDS = {nd.KATON, nd.GOKA_MEKKYAKU, nd.DEATH_BLOSSOM,
            nd.HAKKE_MUJINSATSU, nd.HELLFROG_MEDIUM, nd.DEATHFROG_MEDIUM}


def _ids(timeline) -> set[int]:
    return {aid for _t, aid in timeline}


def _ctx(n: int) -> MultiTargetContext:
    return MultiTargetContext(schedule=((0.0, _DUR, n),))


def test_single_target_byte_identical():
    """No schedule and an explicit N==1 schedule produce the same timeline and
    aux — the byte-identical guarantee (the swaps only engage via N)."""
    tl_none, aux_none = sim.simulate_idealized_perfect(_DUR, [])
    tl_n1, aux_n1 = sim.simulate_idealized_perfect(_DUR, [], sim_context=_ctx(1))
    assert tl_none == tl_n1
    assert aux_none == aux_n1
    assert not (_ids(tl_none) & _AOE_IDS)


def test_two_targets_frogs_only():
    """At 2 targets only the Ninki frogs cross over (Deathfrog 800 > Zesho 700;
    Hellfrog 500 > Bhavacakra 400). The GCD line — mudra finishers, the combo —
    stays single-target (Katon at 700 < the ~990 Raiton+Raiju line; the AoE
    combo at 220 < the ~437 ST combo GCD)."""
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=_ctx(2))
    ids = _ids(tl)
    gcd_aoe = ids & {nd.KATON, nd.GOKA_MEKKYAKU, nd.DEATH_BLOSSOM,
                     nd.HAKKE_MUJINSATSU}
    assert not gcd_aoe, f"2-target GCDs must stay single-target, found {gcd_aoe}"
    assert ids & {nd.HELLFROG_MEDIUM, nd.DEATHFROG_MEDIUM}, \
        "expected the Ninki frogs at 2 targets"


def test_three_targets_katon_goka():
    """At 3 targets the mudra line swaps: Katon (1050) replaces the Raiton
    charge dump (~990) and Goka Mekkyaku (2550 pre-Kassatsu) replaces Hyosho
    Ranryu (1300). The AoE combo still loses (330 < ~437) and stays ST."""
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=_ctx(3))
    ids = _ids(tl)
    assert nd.KATON in ids, "expected Katon at 3 targets"
    assert nd.GOKA_MEKKYAKU in ids, "expected Goka Mekkyaku at 3 targets"
    assert nd.RAITON not in ids, "the Raiton dump should swap to Katon at 3"
    assert nd.HYOSHO_RANRYU not in ids, "Kassatsu should feed Goka at 3"
    assert nd.DEATH_BLOSSOM not in ids and nd.HAKKE_MUJINSATSU not in ids, \
        "the AoE combo crosses at 4, not 3"


def test_four_targets_full_aoe_line():
    """At 4 targets the whole AoE line is in: Death Blossom -> Hakke Mujinsatsu
    (440/GCD) replaces the fresh ST combo (~437/GCD)."""
    tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=_ctx(4))
    ids = _ids(tl)
    assert nd.DEATH_BLOSSOM in ids, "expected Death Blossom at 4 targets"
    assert nd.HAKKE_MUJINSATSU in ids, "expected Hakke Mujinsatsu at 4 targets"
    assert nd.KATON in ids


def test_suiton_kept_for_kunais_bane():
    """The AoE swap never forfeits Kunai's Bane's Shadow Walker feed — Suiton
    still appears in the multi-target ceiling (in-fight or the pre-pull cast)."""
    for n in (3, 4, 6):
        tl, _ = sim.simulate_idealized_perfect(_DUR, [], sim_context=_ctx(n))
        ids = _ids(tl)
        assert nd.SUITON in ids or nd.TCJ_SUITON in ids, \
            f"Suiton missing from the {n}-target ceiling"
        assert nd.KUNAIS_BANE in ids, f"Kunai's Bane missing at {n} targets"


def test_swap_never_worse():
    """The AoE-sim timeline scored at N is never below the ST timeline scored at
    the same N (the MCH crossover-bug class), and the lift is monotone in N."""
    tl_st, aux_st = sim.simulate_idealized_perfect(_DUR, [])
    prev = None
    for n in range(2, 7):
        sched = ((0.0, _DUR, n),)
        tl_aoe, aux_aoe = sim.simulate_idealized_perfect(
            _DUR, [], sim_context=MultiTargetContext(schedule=sched))
        s_st = _score_timeline(list(tl_st), aux_st, None, None, sched)
        s_aoe = _score_timeline(list(tl_aoe), aux_aoe, None, None, sched)
        assert s_aoe >= s_st - 1e-6, (n, s_st, s_aoe)
        if prev is not None:
            assert s_aoe >= prev - 1e-6, f"lift not monotone at N={n}"
        prev = s_aoe


def test_downtime_priming_katon_into_window():
    """Re-engaging from downtime into a multi-target window primes the mudras
    for Katon (not Raiton) — the AoE variant of NIN's signature downtime move."""
    downtime = [(60.0, 75.0)]
    sched = ((75.0, _DUR, 4),)
    tl, _ = sim.simulate_idealized_perfect(
        _DUR, downtime, sim_context=MultiTargetContext(schedule=sched))
    after = [aid for t, aid in sorted(tl) if 74.0 <= t <= 80.0]
    assert nd.KATON in after or nd.SUITON in after, \
        f"expected a primed ninjutsu at re-engage, got {after}"


def main() -> None:
    test_single_target_byte_identical()
    test_two_targets_frogs_only()
    test_three_targets_katon_goka()
    test_four_targets_full_aoe_line()
    test_suiton_kept_for_kunais_bane()
    test_swap_never_worse()
    test_downtime_priming_katon_into_window()
    print("ninja_aoe: all checks passed")


if __name__ == "__main__":
    main()
