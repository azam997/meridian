"""Per-target potency for multi-target / AoE scoring.

The single source of truth for "how much potency does one cast deal when it hits
`n` targets". Used identically by the idealized ceiling (`n` from the
reconstructed `N(t)` schedule) and the delivered side (`n` = the player's
measured per-cast hit count from packetID grouping), so the two stay
apples-to-apples and `ceiling >= delivered` holds by construction.

A cast's multi-target value follows the FFXIV convention
`primary + secondary x (min(n, cap) - 1)`:

  * `primary` = the job's `POTENCIES[aid]` (the first/main target's potency).
  * `secondary` = the per-extra-target potency, read from the job's
    `splash_potencies` (free-splash: ST-rotation abilities that incidentally
    cleave) OR `aoe_potencies` (dedicated AoE buttons the AoE-aware sim casts).
    Falloff is baked into `secondary` (a "-30% to others" line stores the
    reduced value); a full-to-all AoE stores `secondary == primary`.
  * `cap` = the ability's target cap (`aoe_target_caps[aid]`, else
    `DEFAULT_AOE_CAP`).

At `n <= 1` this returns exactly `POTENCIES.get(aid, 0)`, so a single-target
pull — or any ability with no splash/aoe secondary — is byte-identical to the
pre-AoE scorer. Convention: the PRIMARY potency always lives in `POTENCIES`; an
AoE button missing there would lose its first-target potency.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobs._core.job import JobData

# Most FFXIV AoE caps at 8 targets; the rare exceptions live in
# JobData.aoe_target_caps. At the target counts these fights produce (a handful
# of adds) the cap rarely binds, but it keeps the ceiling a true upper bound.
DEFAULT_AOE_CAP: int = 8


@dataclass(frozen=True)
class TargetSpec:
    """The ceiling scorer's target axis: the `N(t)` `schedule` plus the
    per-ability observed-reach `ability_caps` (`((ability_id, max_n), ...)` —
    see `MultiTargetContext.ability_caps`). Constructed by the shared scoring
    scaffolding only when caps exist; `schedule_target_fn` / `n_at` accept
    either this or the bare schedule tuple, so the per-job `_score_timeline`
    adapters become cap-aware with no edits. Caps hold only observed abilities —
    an unobserved ability stays uncapped at the schedule's N (which is itself
    floored at the max observed hits, so an "everything-observed" fallback cap
    would never bind anyway)."""
    schedule: tuple[tuple[float, float, int], ...] = ()
    ability_caps: tuple[tuple[int, int], ...] = ()


def potency_for(aid: int, n: int, job_data: "JobData") -> float:
    """Potency of one cast of `aid` hitting `n` targets (see module docstring).

    `n <= 1`, or an ability with no splash/aoe secondary, returns the bare
    primary potency — byte-identical to `POTENCIES.get(aid, 0)`.
    """
    base = job_data.potencies.get(aid, 0)
    if n <= 1:
        return float(base)
    secondary = job_data.splash_potencies.get(aid)
    if secondary is None:
        secondary = job_data.aoe_potencies.get(aid)
    if secondary is None:
        return float(base)
    cap = job_data.aoe_target_caps.get(aid, DEFAULT_AOE_CAP)
    return float(base) + float(secondary) * (min(n, cap) - 1)


def n_at(t: float, schedule) -> int:
    """Target count active at time `t` from a piecewise `N(t)` `schedule`
    (`((start, end, n), ...)`, sorted, non-overlapping — or a `TargetSpec`
    wrapping one). Returns 1 outside every interval (single target) — the same
    default the no-schedule path assumes. Ignores any ability caps (those are
    per-ability; use `schedule_target_fn`)."""
    schedule = getattr(schedule, "schedule", schedule)
    if not schedule:
        return 1
    for s, e, n in schedule:
        if s <= t < e:
            return int(n)
    return 1


def schedule_target_fn(spec):
    """A `target_fn(t, aid) -> n` closure that reads the CEILING's `N(t)`
    schedule — from a bare schedule tuple or a `TargetSpec`. With a bare
    schedule the ability id is ignored (the ceiling assumes the optimal rotation
    hits every targetable enemy); a `TargetSpec`'s `ability_caps` additionally
    cap each observed ability at the max N anyone actually hit with it. `None`
    for an empty schedule, so the scorer's `target_fn or (lambda...: 1)` default
    keeps a single-target pull byte-identical. The delivered side builds its own
    `target_fn` from measured per-cast hit counts instead (decision 5)."""
    schedule = getattr(spec, "schedule", spec)
    if not schedule:
        return None
    caps_t = getattr(spec, "ability_caps", ())
    if not caps_t:
        return lambda _t, _aid: n_at(_t, schedule)
    caps = dict(caps_t)

    def _fn(_t, _aid):
        n = n_at(_t, schedule)
        cap = caps.get(_aid)
        return n if cap is None else min(n, cap)

    return _fn
