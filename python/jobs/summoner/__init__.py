"""Summoner job package — the fourth caster, first pet-cycle job.

Like RDM / BLM / PCT, adding SMN was a data + picker + scorer exercise: the
shared engine, the `HardcastGCD` timing preset and the `CASTER_HEALER` downtime
policy already existed. SMN supplies the data (`data.py`), the `RotationModel`
(`simulator.py` — the 60s demi cycle + gem/attunement phases + favors +
Aetherflow), and the delivered-potency scorer (`scoring.py`). Registration is
deferred to `_register_self()` (called lazily by `jobs.get_job`) so importing
the package for a data shim doesn't trigger the full aspect-build cascade.

SMN is fully deterministic (Further Ruin / Ruby's Glimmer / favors are
deterministic grants), so — like BLM / PCT — there's no Procs aspect and no
proc-budget `sim_context`. The pet contributions (demi autos, Enkindle payoffs,
primal bursts) are folded onto the player's own cast ids in the data table, so
there is no pet damage stream to fetch (`bundle_extra_streams` stays None — the
MCH Queen counter-example needs one because its Queen potency is battery-scaled;
SMN's folds are constants). The Searing Light party buff rides the shared
`raid_buffs.PROVIDER_BUFFS` catalog; only its self effect (Searing Flash) lives
in this package.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.summoner.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.summoner.scoring import SMNScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        SMNScoringAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.summoner import scoring as sc
    from jobs.summoner.simulator import simulate_canonical_aligned
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
        name="Summoner",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
