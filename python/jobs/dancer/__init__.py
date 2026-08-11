"""Dancer job package — the first physical-ranged proc job.

DNC plugs into the existing stack the way the recipe intends: the shared engine,
the `InstantGCD` timing preset and the `PHYSICAL_RANGED` downtime policy already
exist. DNC supplies the data (`data.py`), the `RotationModel` (`simulator.py` —
the Cascade/Fountain combo + budgeted proc/feather/esprit economy + step dances +
burst), the delivered-potency scorer (`scoring.py` — three derived self-buff
windows) and the proc-utilization aspect (`procs.py`). Registration is deferred to
`_register_self()` (called lazily by `jobs.get_job`) so importing the package for
a data shim doesn't trigger the full aspect-build cascade.

DNC's RNG/external resources (Silken procs, Fourfold Feathers, party-fed Esprit)
are matched to the player's counts: the Scoring aspect measures them and threads
them into the idealized ceiling as `sim_context`, so below-average luck never
costs efficiency. The ProcsAspect surfaces the delivered-side proc *misuse* story.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.dancer.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.dancer.procs import ProcsAspect
    from jobs.dancer.scoring import DancerScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        DancerScoringAspect(),
        ProcsAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.dancer import scoring as sc
    from jobs.dancer.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """DNC-specific priced cards the generic missed-cast diff can't see: wasted
    Silken / Fan Dance procs."""
    from jobs.dancer.procs import improvements_from_procs
    ar = you.aspects.get("Procs")
    return improvements_from_procs(ar.state if ar is not None else {})


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Dancer",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
