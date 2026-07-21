"""Bard job package — the first song-cycle job.

BRD plugs into the existing stack the way the recipe intends: the shared engine,
the `InstantGCD` timing preset and the `PHYSICAL_RANGED` downtime policy already
exist. BRD supplies the data (`data.py`), the `RotationModel` (`simulator.py` —
the WM→MB→AP song cycle, the two DoTs + Iron Jaws, the budgeted Repertoire
economy, Barrage/Coda machinery, the Army's Paeon haste window) and the
delivered-potency scorer (`scoring.py` — Raging Strikes windows, Barrage ×3,
Encore Coda tiers, snapshot-scored DoTs). Registration is deferred to
`_register_self()` (called lazily by `jobs.get_job`) so importing the package
for a data shim doesn't trigger the full aspect-build cascade.

BRD's RNG resources (Repertoire, Hawk's Eye, Soul Voice) are matched to the
player's counts: the Scoring aspect measures them and threads them into the
idealized ceiling as `sim_context`, so below-average luck never costs
efficiency. Battle Voice / Radiant Finale are party buffs — the shared
raid_buffs.py catalog models them; here they are only 0-potency realism casts.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.bard.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.bard.scoring import BardScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        BardScoringAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.bard import scoring as sc
    from jobs.bard.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Bard",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
