"""Warrior job package. Registers WAR with the job registry — the analyzer's
second TANK and its fifth full idealized simulator (after MCH, RPR, RDM, PLD).

Reuses the seven shared aspects in jobs._aspects/ plus two WAR-specific ones:
WarriorScoringAspect (the efficiency-KPI scorer) and SurgingTempestAspect (the
maintained 10% self-buff — the WAR analog of RPR's Death's Design). WAR is the
first tank with an OFFENSIVE gauge (Beast), so unlike Paladin it runs the shared
Overcap aspect over a real gauge.
"""
from __future__ import annotations

from typing import Any

from jobs._core.job import Job, register
from jobs.warrior.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.warrior.scoring import WarriorScoringAspect
    from jobs.warrior.surging_tempest import SurgingTempestAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        WarriorScoringAspect(),
        SurgingTempestAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a
    given (duration, downtime, buffs) is simulated once. `delivered_potency` is
    the idealized ceiling scored with full Surging Tempest coverage (matching
    idealized_strict). All boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.warrior import scoring as sc
    from jobs.warrior.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=sc._full_st_intervals,
    )


def _bundle_extra_streams(report: dict[str, Any], fight: dict[str, Any],
                          actor: dict[str, Any]) -> list:
    """Warm the player's DamageDone stream in the per-pull bundle so the Surging
    Tempest coverage fetch (reconstructed from each hit's buffs snapshot) is a
    cache hit. Surging Tempest is a self-buff, so the player's own damage events
    are the source (no enemy debuff stream)."""
    from fflogs_api import BundleStream
    start, end = fight["startTime"], fight["endTime"]
    return [
        BundleStream(data_type="DamageDone", start=start, end=end,
                     source_id=actor["id"]),
    ]


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """WAR-specific priced Potential-Improvement cards the generic missed-cast
    diff can't see: Surging Tempest downtime (the 10% amp missed while it was
    dropped)."""
    from jobs.warrior.surging_tempest import improvements_from_surging_tempest

    def _state(name: str) -> dict:
        ar = you.aspects.get(name)
        return ar.state if ar is not None else {}

    return improvements_from_surging_tempest(_state("SurgingTempest"))


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Warrior",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        bundle_extra_streams=_bundle_extra_streams,
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
