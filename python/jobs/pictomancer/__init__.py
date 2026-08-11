"""Pictomancer job package — the third caster, first downtime-painting job.

Like RDM / BLM, adding PCT was a data + picker + scorer exercise: the shared
engine, the `HardcastGCD` timing preset and the `CASTER_HEALER` downtime policy
already existed. PCT supplies the data (`data.py`), the `RotationModel`
(`simulator.py` — the aetherhue chain + palette/paint economy + the canvas ->
muse -> portrait ladder + Hammer Time + the Starry Muse burst), and the
delivered-potency scorer (`scoring.py`). Registration is deferred to
`_register_self()` (called lazily by `jobs.get_job`) so importing the package
for a data shim doesn't trigger the full aspect-build cascade.

PCT is fully deterministic (no RNG procs), so — like BLM — there's no Procs
aspect and no proc-budget `sim_context`; the ceiling is pure (duration,
downtime, buffs) data plus the per-player GCD. The Starry Muse party buff rides
the shared `raid_buffs.PROVIDER_BUFFS` catalog; only its self effects live in
this package.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.pictomancer.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.pictomancer.scoring import PCTScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        PCTScoringAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.pictomancer import scoring as sc
    from jobs.pictomancer.simulator import simulate_canonical_aligned
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
        name="Pictomancer",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
