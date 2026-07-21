"""Paladin job package. Registers PLD with the job registry — the analyzer's
first TANK and its fourth full idealized simulator (after MCH, RPR, RDM).

Reuses the seven shared aspects in jobs._aspects/ plus PaladinScoringAspect (the
efficiency-KPI scorer). PLD has no maintained-debuff / pet aspect of its own; its
one bespoke damage piece (Fight or Flight) is folded into the scorer's delivered
math (see jobs/paladin/scoring.py).
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.paladin.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.paladin.scoring import PaladinScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        PaladinScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs) is simulated once. All boilerplate lives in
    the shared `make_simulator`. No coverage overlay (Fight or Flight is derived
    inside the scorer, not assumed full)."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.paladin import scoring as sc
    from jobs.paladin.simulator import simulate_canonical_aligned
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
        name="Paladin",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
