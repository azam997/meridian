"""Gunbreaker job package. Registers GNB with the job registry — the analyzer's
third TANK (after Paladin and Warrior) and its twelfth full idealized simulator.

Reuses the seven shared aspects in jobs._aspects/ plus the GNB scorer. Like Warrior,
GNB has an OFFENSIVE gauge (the Powder Gauge / cartridges), so it runs the shared
Overcap aspect over a real gauge. The cartridge spend-cadence fork drives the beam
search (the SAM/DRG pattern). No bespoke per-job aspects: No Mercy is a derived-from-
timeline self-buff (scored symmetrically), GNB has no positionals that affect scoring,
and no guaranteed-crit ability.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.gunbreaker.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.gunbreaker.scoring import GunbreakerScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        GunbreakerScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs) is simulated once. GNB's No Mercy self-buff rides the
    timeline (no coverage overlay), so `coverage_intervals` is None. All boilerplate
    lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.gunbreaker import scoring as sc
    from jobs.gunbreaker.simulator import simulate_canonical_aligned
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
        name="Gunbreaker",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
