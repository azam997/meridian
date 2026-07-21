"""Viper job package. Registers VPR with the job registry — a fast, deterministic
instant-melee job modeled on the RPR template (InstantGCD + greedy/perfect, no
beam/DP) with two SAM-style self-buffs: Swiftscaled (haste) baked into the GCD
constant, and Hunter's Instinct (10% damage) as a measured `coverage_intervals`
overlay (jobs/viper/scoring + buffs). Reuses the seven shared aspects plus the
VPR scorer; the Reawakened combo's cleave is credited via SPLASH_POTENCIES.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.viper.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.viper.scoring import VPRScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        VPRScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs) is simulated once. `delivered_potency` is the
    idealized ceiling scored with full Hunter's Instinct coverage (matching
    idealized_strict). All boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.viper import scoring as sc
    from jobs.viper.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=sc._full_hunters_instinct_intervals,
    )


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Viper",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
