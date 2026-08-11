"""GCD-timing presets — the archetype layer of the simulator.

`RolePolicy` (jobs/_core/job.py) already encodes the *downtime-detection* side
of the three FFXIV archetypes (physical-ranged / melee-tank / melee-dps /
caster-healer). This is its companion on the *rotation* side: how long a GCD
slot takes and how many oGCDs weave into it.

Two archetype shapes:

  * `InstantGCD` — every weaponskill is instant (physical-ranged + melee). The
    slot is the flat GCD recast (2.5s base; a job overlays reduced-GCD windows
    like MCH Overheated / RPR Enshroud in its `gcd_duration` override) and two
    oGCDs weave (a 3rd clips, handled by the engine). This is what MCH and RPR
    use — their numbers stay byte-identical.

  * `HardcastGCD` — casters (and caster-like healers) have *cast-time* GCDs.
    The slot is `max(cast_time, recast)` (you're locked for the cast), and a
    hardcast leaves a smaller weave budget than an instant because only the
    slidecast tail is free. Red Mage (the first caster) uses this, layering its
    Dualcast instant logic on top via a `gcd_duration` override.

A model holds one preset as `self.timing`; the engine's `BaseRotationModel`
defers `gcd_duration` / `weave_budget` to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GcdTiming(Protocol):
    def duration(self, state, gcd_id: int, params) -> float: ...
    def weave_budget(self, state, gcd_id: int, params) -> int: ...


@dataclass(frozen=True)
class InstantGCD:
    """All-instant weaponskills: flat recast, full weave budget. What every
    non-caster sim (MCH/RPR/PLD/WAR/SAM) uses."""
    base_s: float = 2.5

    def duration(self, state, gcd_id: int, params) -> float:
        return self.base_s

    def weave_budget(self, state, gcd_id: int, params) -> int:
        # Two oGCDs standard; a 3rd is allowed by the param sweep and clips
        # (the engine applies triple_weave_clip_s).
        return params.max_weaves_per_gcd


@dataclass(frozen=True)
class HardcastGCD:
    """Cast-time GCDs (casters / caster-like healers).

    `cast_times` maps an ability id to its cast time; abilities absent from the
    map are instant (procs, instant-cast spells, oGCD-adjacent GCDs). The slot
    duration is `max(cast_time, gcd_recast_s)` — you can't press the next GCD
    until the longer of the two elapses. A hardcast yields `hardcast_weaves`
    oGCDs (only the slidecast tail is free); an instant cast yields the full
    `instant_weaves`. Both are clamped to the param sweep's `max_weaves_per_gcd`
    so the sweep can still explore a tighter budget.
    """
    gcd_recast_s: float = 2.5
    cast_times: dict[int, float] = field(default_factory=dict)
    slidecast_s: float = 0.5
    instant_weaves: int = 2
    hardcast_weaves: int = 1

    def _cast_time(self, gcd_id: int) -> float:
        return self.cast_times.get(gcd_id, 0.0)

    def duration(self, state, gcd_id: int, params) -> float:
        return max(self._cast_time(gcd_id), self.gcd_recast_s)

    def weave_budget(self, state, gcd_id: int, params) -> int:
        base = self.instant_weaves if self._cast_time(gcd_id) <= 0.0 \
            else self.hardcast_weaves
        return min(base, params.max_weaves_per_gcd)
