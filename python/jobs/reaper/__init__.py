"""Reaper job package. Registers RPR with the job registry — the second job
with a full idealized simulator (after Machinist).

Reuses the seven shared aspects in jobs._aspects/ plus three RPR-specific ones:
RPRScoringAspect (the efficiency-KPI scorer) and DeathsDesignAspect (the
maintained 10% debuff), with PositionalAspect held back until the live probe
confirms FFLogs exposes the positional bonus byte (see jobs/reaper/positionals).
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.reaper.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.reaper.death_design import DeathsDesignAspect
    from jobs.reaper.scoring import RPRScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        RPRScoringAspect(),
        DeathsDesignAspect(),
        # PositionalAspect() — wire in after the live bonus-byte probe.
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs) is simulated once. `delivered_potency` is
    the idealized ceiling scored with full Death's Design coverage (matching
    idealized_strict). All boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.reaper import scoring as sc
    from jobs.reaper.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=sc._full_dd_intervals,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """RPR-specific priced Potential-Improvement cards the generic missed-cast
    diff can't see: Death's Design downtime and (when wired) positional misses."""
    from jobs.reaper.death_design import improvements_from_deaths_design
    from jobs.reaper.positionals import improvements_from_positionals

    def _state(name: str) -> dict:
        ar = you.aspects.get(name)
        return ar.state if ar is not None else {}

    out: list = []
    out += improvements_from_deaths_design(_state("DeathsDesign"))
    out += improvements_from_positionals(_state("Positionals"))
    return out


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Reaper",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
