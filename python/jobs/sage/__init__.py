"""Sage job package — the analyzer's fourth HEALER (after WHM + AST + SCH), the
second shield healer (H1).

Reuses the seven shared aspects in jobs._aspects/ plus SageScoringAspect (the
efficiency-KPI scorer). The healer-specific story — the mit-plan heal-GCD lock
(jobs/_core/heal_locks.py) — lives entirely in the shared machinery; the SGE model
adds the Eukrasia DoT sequence + the Phlegma charge economy + the Psyche oGCD in the
data + rotation model (jobs/sage/simulator.py). There is no bespoke aspect.

SGE models like AST/SCH on the damage side (a nearly pure Dosis III filler line, no
GCD-recast haste window). There is no GCD fork, so the ceiling routes through
`engine.perfect` + `canonical_aligned_max_guard` (the AST/SCH/RDM pattern), NOT the
beam. **SGE brings no party damage buff** (Kardia is a single-target heal link), so
there is no raid_buffs.py entry and `buff_intervals` only ever carries other jobs'
buffs. SGE has no pet — aux is always 0.

Registration is deferred to `_register_self()` (called lazily by `jobs.get_job`) so
importing the package for a data shim doesn't trigger the aspect-build cascade.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.sage.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.sage.scoring import SageScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        SageScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs, sim_context) is simulated once. No coverage overlay
    (SGE has no maintained personal damage buff)."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.sage import scoring as sc
    from jobs.sage.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """SGE-specific priced cards: costed heal GCDs beyond the mit plan's locked
    requirement (only meaningful on a plan-locked healer run)."""
    from jobs.sage.improvements import improvements_from_heal_gcds
    return improvements_from_heal_gcds(you)


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Sage",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
