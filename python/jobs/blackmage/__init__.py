"""Black Mage job package — the second caster, first MP-economy job.

Like Red Mage, adding BLM was a data + picker + scorer exercise: the shared
engine, the `HardcastGCD` timing preset and the `CASTER_HEALER` downtime policy
already existed. BLM supplies the data (`data.py`), the `RotationModel`
(`simulator.py` — the Astral Fire / Umbral Ice phase machine + the MP gate +
Polyglot / Astral Soul economy + the High Thunder DoT), and the delivered-potency
scorer (`scoring.py`). Registration is deferred to `_register_self()` (called
lazily by `jobs.get_job`) so importing the package for a data shim doesn't trigger
the full aspect-build cascade.

BLM is RNG-free (Thunderhead / Firestarter / Polyglot are deterministic), so —
unlike RDM — there's no Procs aspect and no proc-budget `sim_context`; the ceiling
is pure (duration, downtime, buffs) data plus the per-player GCD.
"""
from __future__ import annotations

from jobs._core.job import Job, register
from jobs.blackmage.data import JOB_DATA

_registered = False


def _build_aspects() -> tuple:
    from jobs._aspects.abilities import AbilityTimelineAspect
    from jobs._aspects.alignment import AlignmentAspect
    from jobs._aspects.buff_drift import BuffDriftAspect
    from jobs._aspects.clipping import ClippingAspect
    from jobs._aspects.drift import DriftAspect
    from jobs._aspects.opener import OpenerAspect
    from jobs._aspects.overcap import OvercapAspect
    from jobs.blackmage.scoring import BLMScoringAspect

    return (
        AbilityTimelineAspect(),
        DriftAspect(JOB_DATA),
        ClippingAspect(JOB_DATA),
        OvercapAspect(JOB_DATA),
        OpenerAspect(JOB_DATA),
        AlignmentAspect(JOB_DATA),
        BuffDriftAspect(),
        BLMScoringAspect(),
    )


def _build_simulator():
    from jobs._core.sim.scoring import make_simulator
    from jobs.blackmage import scoring as sc
    from jobs.blackmage.simulator import simulate_canonical_aligned
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
        name="Black Mage",
        data=JOB_DATA,
        aspects=_build_aspects(),
        simulator=_build_simulator(),
    ))
    _registered = True
