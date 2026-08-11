"""White Mage job package — the analyzer's first HEALER (seventh full
idealized simulator, after MCH, RPR, RDM, PLD, WAR, SAM).

Reuses the seven shared aspects in jobs._aspects/ plus WHMScoringAspect (the
efficiency-KPI scorer). The healer-specific story — the dual-purpose Lily
economy (0-damage instant heal GCDs nourishing a 1,400p Afflatus Misery) and
the Presence of Mind haste window — lives entirely in the data + rotation
model (jobs/whitemage/simulator.py); there is no bespoke aspect.

Registration is deferred to `_register_self()` (called lazily by
`jobs.get_job`) so importing the package for a data shim doesn't trigger the
full aspect-build cascade.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.whitemage.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.whitemage.scoring import WHMScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        WHMScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs, sim_context) is simulated once. No
    coverage overlay (PoM's value is more GCDs, modeled in the sim's haste
    window, not a multiplier)."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.whitemage import scoring as sc
    from jobs.whitemage.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """WHM-specific priced cards: costed heal GCDs beyond the mit plan's
    locked requirement (only meaningful on a plan-locked healer run)."""
    from jobs.whitemage.improvements import improvements_from_heal_gcds
    return improvements_from_heal_gcds(you)


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="White Mage",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
