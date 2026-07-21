"""Samurai job package. Registers SAM with the job registry — the analyzer's
sixth job with a full idealized simulator.

Reuses the seven shared aspects in jobs._aspects/ plus SAMScoringAspect (the
efficiency-KPI scorer). SAM's two maintained self-buffs and its Tengentsu Kenki
are handled inside scoring (Fugetsu = full-coverage overlay; Fuka = GCD base;
Tengentsu = measured `sim_context`), so no extra per-aspect class is needed.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.samurai.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.samurai.scoring import SAMScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        SAMScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs, sim_context) is simulated once.
    `delivered_potency` is the idealized ceiling scored with full Fugetsu coverage
    (matching idealized_strict). All boilerplate lives in `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.samurai import scoring as sc
    from jobs.samurai.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=sc._full_fugetsu_intervals,
    )


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Samurai",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
