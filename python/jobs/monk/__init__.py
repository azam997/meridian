"""Monk job package. Registers MNK with the job registry — the first form-cycle
job: the opo-opo -> raptor -> coeurl wheel with three Fury economies, the
Perfect Balance -> Masterful Blitz machine (lunar-vs-solar as the beam's GCD
fork), the Riddle of Fire +15% self-window folded into the incremental beam
score (the GNB/DRG windowed-self-buff pattern), a guaranteed-crit spender
(Leaping Opo, the SAM pricing), and a crit-RNG/party-fed chakra economy modeled
as a measured BUDGET via sim_context (the DNC pattern). Reuses the seven shared
aspects plus the MNK scorer and the positional detector.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.monk.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.monk.positionals import PositionalAspect
    from jobs.monk.scoring import MonkScoringAspect

    return (
        AbilityTimelineAspect(prepull_buff_ids=JOB_DATA.prepull_buff_ids),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        MonkScoringAspect(),
        PositionalAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs, sim_context) is simulated once. MNK's
    self-buff rides the timeline (no coverage overlay), so `coverage_intervals`
    is None. All boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.monk import scoring as sc
    from jobs.monk.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """MNK-specific priced Potential-Improvement cards the generic missed-cast
    diff can't see: positional misses."""
    from jobs.monk.positionals import improvements_from_positionals

    def _state(name: str) -> dict:
        ar = you.aspects.get(name)
        return ar.state if ar is not None else {}

    return improvements_from_positionals(_state("Positionals"))


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Monk",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
