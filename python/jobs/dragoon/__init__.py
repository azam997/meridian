"""Dragoon job package. Registers DRG with the job registry — the 11th job with a
full idealized simulator, and the first melee whose branching combo drives the
beam search (the SAM Higanbana-vs-Midare pattern, here Lance Barrage vs Spiral Blow).

Reuses the seven shared aspects in jobs._aspects/ plus three DRG-specific ones:
DRGScoringAspect (the efficiency-KPI scorer), PositionalAspect (the three combo
positionals), and LifeSurgeAspect (the guaranteed-crit targeting).
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.dragoon.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.dragoon.lifesurge import LifeSurgeAspect
    from jobs.dragoon.positionals import PositionalAspect
    from jobs.dragoon.scoring import DRGScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        DRGScoringAspect(),
        PositionalAspect(),
        LifeSurgeAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs) is simulated once. DRG's self-buffs ride the timeline
    (no coverage overlay), so `coverage_intervals` is None. All boilerplate lives in
    the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.dragoon import scoring as sc
    from jobs.dragoon.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """DRG-specific priced Potential-Improvement cards the generic missed-cast diff
    can't see: positional misses and Life Surge mistargeting."""
    from jobs.dragoon.lifesurge import improvements_from_lifesurge
    from jobs.dragoon.positionals import improvements_from_positionals

    def _state(name: str) -> dict:
        ar = you.aspects.get(name)
        return ar.state if ar is not None else {}

    out: list = []
    out += improvements_from_positionals(_state("Positionals"))
    out += improvements_from_lifesurge(_state("LifeSurge"))
    return out


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Dragoon",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
