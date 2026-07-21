"""Per-run idle-stretch detection — the building block for Tier-B
consensus downtime.

A reference player is "idle" during stretches where their cast cadence
broke by more than `policy.idle_floor_mult × eff_gcd_s`. We don't care
WHY they were idle (clip, mechanic, knockup); we just record the windows
so the Tier-B aggregator can look for cross-ref agreement.

Tier-A windows are excluded from the input so a stretch covered by
confirmed boss-untargetable downtime doesn't double-count as a consensus
candidate.

`compute_eff_gcd` is promoted from `clipping.py` so Tier B and the
clipping detector estimate effective GCD the same way. Keeping it in
one place means a future SkS-aware refinement only has to land once.
"""
from __future__ import annotations

from statistics import median
from typing import Any

from . import ability_metadata
from .job import JobData, RolePolicy


# Filter bounds used by both compute_eff_gcd here and historically by
# clipping.compute_clipping. Outside [1.5, 5.0] the gap is either inside
# a sub-rotation (Hypercharge BS chain) or itself a downtime artifact.
_GCD_FILTER_MIN_S = 1.5
_GCD_FILTER_MAX_S = 5.0
_EFF_GCD_FLOOR_S  = 2.0
_EFF_GCD_CEIL_S   = 2.6
_DEFAULT_GCD_S    = 2.5


def compute_eff_gcd(norm_casts: list[tuple[float, int]],
                    data: JobData) -> float:
    """Estimate effective GCD from the median in-bounds gap between
    consecutive regular weaponskills. Returns DEFAULT (2.5s) when the
    sample is too small.

    Same math the clipping aspect uses — shared here so Tier-B idle
    detection agrees on the per-run GCD baseline.
    """
    regular_gcds: list[float] = []
    for t, aid in norm_casts:
        if t < 0:
            continue
        if aid in data.clip_exclusions:
            continue
        if data.gcd_recast_mult.get(aid, 1.0) != 1.0:
            continue   # non-standard recast (Viper's 2.0/3.0/3.5s GCDs) — would
            #            bias the standard-GCD estimate; excluded like clip_exclusions.
        meta = ability_metadata.get_metadata(aid)
        if meta is None or meta.is_ogcd:
            continue
        regular_gcds.append(t)

    if len(regular_gcds) < 4:
        return _DEFAULT_GCD_S

    raw_gaps = [regular_gcds[i + 1] - regular_gcds[i]
                for i in range(len(regular_gcds) - 1)]
    filtered = [g for g in raw_gaps if _GCD_FILTER_MIN_S <= g <= _GCD_FILTER_MAX_S]
    if not filtered:
        return _DEFAULT_GCD_S
    return max(_EFF_GCD_FLOOR_S, min(_EFF_GCD_CEIL_S, median(filtered)))


def _subtract_windows(stretch: tuple[float, float],
                      windows: list[tuple[float, float]]
                      ) -> list[tuple[float, float]]:
    """Split `stretch` around any overlapping `windows`. Used so a stretch
    that's partially covered by Tier A produces 0 or more sub-stretches
    representing the uncovered portion only."""
    s, e = stretch
    if s >= e:
        return []
    parts = [(s, e)]
    for ws, we in windows:
        new_parts: list[tuple[float, float]] = []
        for ps, pe in parts:
            if we <= ps or ws >= pe:
                new_parts.append((ps, pe))
                continue
            if ws > ps:
                new_parts.append((ps, ws))
            if we < pe:
                new_parts.append((we, pe))
        parts = new_parts
    return [(a, b) for a, b in parts if b - a > 0.01]


def compute_idle_stretches(norm_casts: list[tuple[float, int]],
                           fight_duration_s: float,
                           eff_gcd_s: float,
                           policy: RolePolicy,
                           exclude_windows: list[tuple[float, float]],
                           recast_mult: dict[int, float] | None = None,
                           ) -> list[tuple[float, float]]:
    """Find stretches where consecutive in-fight casts are separated by
    more than `policy.idle_floor_mult × eff_gcd_s`. Excludes time
    overlapping `exclude_windows` (typically Tier-A targetability).
    Returns sorted, non-overlapping windows.

    `recast_mult` (a job's per-ability GCD recast as a multiple of the standard
    GCD) scales the allowed gap by the FROM-cast's own recast, so a slow GCD (a
    Viper 3.0s Coil) that legitimately leaves a longer gap before the next cast
    isn't flagged as idle. `None`/empty ⇒ a uniform threshold, byte-identical.

    Pure function — synthetic norm_casts is enough to drive it.
    """
    base_threshold = policy.idle_floor_mult * eff_gcd_s
    mult = recast_mult or {}
    in_fight = [(t, aid) for t, aid in norm_casts if t >= 0]
    in_fight.sort(key=lambda x: x[0])

    raw_stretches: list[tuple[float, float]] = []
    last_t = 0.0
    last_aid: int | None = None
    for t, aid in in_fight:
        threshold = base_threshold * (mult.get(last_aid, 1.0)
                                      if last_aid is not None else 1.0)
        if t - last_t > threshold:
            raw_stretches.append((last_t, t))
        last_t = t
        last_aid = aid
    if fight_duration_s - last_t > base_threshold * mult.get(last_aid, 1.0):
        raw_stretches.append((last_t, fight_duration_s))

    out: list[tuple[float, float]] = []
    for stretch in raw_stretches:
        out.extend(_subtract_windows(stretch, exclude_windows))

    # Merge any adjacent / overlapping pieces.
    if not out:
        return []
    out.sort(key=lambda x: x[0])
    merged: list[tuple[float, float]] = [out[0]]
    for s, e in out[1:]:
        ls, le = merged[-1]
        if s <= le + 0.01:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged
