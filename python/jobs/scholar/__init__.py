"""Scholar job package — the analyzer's third HEALER (after White Mage + Astrologian).

Reuses the seven shared aspects in jobs._aspects/ plus ScholarScoringAspect (the
efficiency-KPI scorer). The healer-specific story — the mit-plan heal-GCD lock
(jobs/_core/heal_locks.py) and the Chain-Stratagem / Baneful-Impaction / Aetherflow
oGCD economy — lives entirely in the data + rotation model (jobs/scholar/
simulator.py); there is no bespoke aspect.

SCH models like AST on the damage side (a nearly pure Broil IV filler line, no
GCD-recast haste window) plus one addition: the Aetherflow gauge (an Energy Drain
economy, modeled as a SimState int). There is no GCD fork, so the ceiling routes
through `engine.perfect` + `canonical_aligned_max_guard` (the AST/RDM pattern), NOT
the beam. Chain Stratagem is already a party `BuffProvider` (jobs/_core/raid_buffs.py),
so it is credited via `buff_intervals` and NEVER re-derived here as a self-buff. The
fairy (Eos/Selene) is heal-only — no pet damage is folded (aux is always 0).

Registration is deferred to `_register_self()` (called lazily by `jobs.get_job`) so
importing the package for a data shim doesn't trigger the aspect-build cascade.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.scholar.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.scholar.scoring import ScholarScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        ScholarScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs, sim_context) is simulated once. No coverage overlay
    (Chain Stratagem is an external party buff, not a self amp)."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.scholar import scoring as sc
    from jobs.scholar.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """SCH-specific priced cards: costed heal GCDs beyond the mit plan's locked
    requirement (only meaningful on a plan-locked healer run)."""
    from jobs.scholar.improvements import improvements_from_heal_gcds
    return improvements_from_heal_gcds(you)


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Scholar",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
