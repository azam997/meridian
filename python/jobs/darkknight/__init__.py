"""Dark Knight job package. Registers DRK with the job registry — the analyzer's
fourth TANK (after Paladin, Warrior, Gunbreaker) and its eighteenth full idealized
simulator.

Reuses the seven shared aspects in jobs._aspects/ plus the DRK scorer. Like WAR/GNB,
DRK has an OFFENSIVE gauge (Blood), so it runs the shared Overcap aspect over a real
gauge; its second economy (MP) is time-tick-fed and lives inside the simulator
(data.py explains why it is not a GaugeModel). The Blood/MP/Delirium-chain
spend-cadence fork drives the beam search (the SAM/DRG/GNB pattern). No bespoke
per-job aspects: Darkside is a derived-from-timeline self-buff (scored
symmetrically, priced as an improvement card via `improvement_contributors`), the
Living Shadow pet is a fixed-count fold on the summon cast id (the SMN pattern),
and DRK has no positionals and no guaranteed-crit ability.
"""
from __future__ import annotations

from typing import Any

from jobs._core.job import Job, register
from jobs.darkknight.data import JOB_DATA

_registered = False


def _build_aspects():
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.darkknight.scoring import DarkKnightScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        DarkKnightScoringAspect(),
    )


def _build_simulator():
    """The IdealizedSimulator wrapper — routes through the scoring cache so a given
    (duration, downtime, buffs) is simulated once. DRK's Darkside self-buff rides
    the timeline (no coverage overlay), so `coverage_intervals` is None. All
    boilerplate lives in the shared `make_simulator`."""
    from jobs._core.sim.scoring import make_simulator
    from jobs.darkknight import scoring as sc
    from jobs.darkknight.simulator import simulate_canonical_aligned
    return make_simulator(
        sc._FNS,
        score_timeline=sc._score_timeline,
        canonical_fn=simulate_canonical_aligned,
        coverage_intervals=None,
    )


def _improvement_contributors(you, idealized, enabler_values, death_windows):
    """DRK-specific priced Potential-Improvement cards the generic missed-cast
    diff can't see: Darkside downtime (the 10% amp missed while it was dropped —
    a multiplier loss, invisible to cast counting)."""
    from jobs.darkknight.scoring import improvements_from_darkside

    def _state(name: str) -> dict[str, Any]:
        ar = you.aspects.get(name)
        return ar.state if ar is not None else {}

    return improvements_from_darkside(_state("Scoring"))


def _register_self() -> None:
    global _registered
    if _registered:
        return
    register(Job(
        name="Dark Knight",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
        improvement_contributors=_improvement_contributors,
    ))
    _registered = True
