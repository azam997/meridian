"""Red Mage job package — the first caster.

Adding a caster proved a data + picker + scorer exercise (per the repeatable
recipe): the shared engine, the `HardcastGCD` timing preset and the
`CASTER_HEALER` downtime policy already existed. RDM supplies the data
(`data.py`), the `RotationModel` (`simulator.py` — Dualcast filler loop +
enchanted melee combo + proc economy), and the delivered-potency scorer
(`scoring.py`). Registration is deferred to `_register_self()` (called lazily by
`jobs.get_job`) so importing the package for a data shim doesn't trigger the
full aspect-build cascade.

Procs (Verfire/Verstone Ready) are matched to the player's count: the Scoring
aspect measures it and threads it into the idealized ceiling as `sim_context`,
so below-average proc luck never costs efficiency. The ProcsAspect (Phase 3)
surfaces the delivered-side proc *misuse* story.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.redmage.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.redmage.procs import ProcsAspect
    from jobs.redmage.scoring import RDMScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        RDMScoringAspect(),
        ProcsAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.redmage import scoring as sc
    from jobs.redmage.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """RDM-specific priced cards the generic missed-cast diff can't see: wasted
    Verfire/Verstone procs."""
    from jobs.redmage.procs import improvements_from_procs
    ar = you.aspects.get("Procs")
    return improvements_from_procs(ar.state if ar is not None else {})


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Red Mage",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
