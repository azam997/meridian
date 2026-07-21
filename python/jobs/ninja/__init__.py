"""Ninja job package. Registers NIN with the job registry — the first mudra job:
mixed fixed-rate GCDs (0.5s mudras / 1.5s ninjutsu / ~1.0s Ten Chi Jin steps
under the Huton-hasted 2.12s weaponskill GCD, via per-ability `gcd_duration` +
`gcd_recast_mult`), a shared 2-charge/20s ninjutsu pool that regenerates through
downtime (edge-of-window mudra pre-casting is the job's signature optimization),
and the Kunai's Bane +10% self-window folded into the incremental beam score
(the DRG/GNB windowed-self-buff pattern). Reuses the seven shared aspects plus
the NIN scorer.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.ninja.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.ninja.scoring import NinjaScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        NinjaScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs, sim_context) is simulated once. All
    boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.ninja import scoring as sc
    from jobs.ninja.simulator import simulate_canonical_aligned
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
        name="Ninja",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
