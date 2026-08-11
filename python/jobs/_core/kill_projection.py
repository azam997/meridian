"""Kill-time projection for in-progress (wipe) pulls.

Pure, party-scoped, no client — a peer of the other per-pull passes. Given how
far the party got (FFLogs `fightPercentage` = phase-weighted remaining % of the
WHOLE fight — probe: scripts/probe_recent_reports.py) and how long they actively
fought (wipe duration minus the pull's own Tier-A downtime), project the kill
time by extending the party's active burn rate over the remaining percentage,
then adding back the reference kill's downtime that lies beyond the wipe point.

v1 is encounter-wide (`active_rate_v1`): the burn rate is assumed uniform
across the remaining fight. `last_phase` / `phase_transitions` ride the inputs
untouched — the seam for a future phase-aware projector that rates each
ultimate phase separately.
"""
from __future__ import annotations

from dataclasses import dataclass

# Mirrors the theorizer's ceiling (`_THEORIZE_MAX_S` in sidecar/main.py): no
# projection should claim a kill longer than the longest supported sim.
_PROJECTION_MAX_S = 1800.0

# Below this burned fraction the rate estimate is noise (an opener wipe says
# nothing about sustained party output).
_MIN_BURNED_FRACTION = 0.01

_EPS = 1e-6


@dataclass(frozen=True)
class ProjectionInputs:
    """Everything `project_kill_time` needs, already resident in the sidecar
    at build time (prog context + refs)."""
    elapsed_s: float                 # full wipe duration (party fought to here)
    fight_pct_remaining: float | None  # FFLogs fightPercentage (0-100)
    own_downtime_s: float            # full-span Tier-A downtime in the wipe
    ref_downtime_windows: tuple[tuple[tuple[float, float], ...], ...]
    ref_kill_times: tuple[float, ...]  # parallel to ref_downtime_windows
    last_phase: int | None = None      # unused in v1 (phase-aware seam)
    phase_transitions: tuple = ()      # unused in v1 (phase-aware seam)


@dataclass(frozen=True)
class ProjectedKillTime:
    projected_s: float
    method: str
    elapsed_s: float
    active_s: float
    burned_pct: float
    remaining_pct: float
    downtime_beyond_s: float   # ref downtime credited past the wipe point
    ref_count: int
    ref_kill_s: float          # the reference kill the downtime came from


def project_kill_time(inp: ProjectionInputs) -> ProjectedKillTime | None:
    """Encounter-wide active-rate projection. None = no honest projection
    (missing/degenerate progress data or no references) — the UI then shows
    the pull duration only."""
    pct = inp.fight_pct_remaining
    if pct is None or inp.elapsed_s <= 0 or not inp.ref_kill_times:
        return None
    # Kills report fightPercentage ~0.01, not 0 (probe) — and a wipe at true
    # 0% left needs no projection. >= 100 means no measurable progress.
    remaining = float(pct) / 100.0
    burned = 1.0 - remaining
    if remaining <= 0.0 or burned < _MIN_BURNED_FRACTION:
        return None

    active_s = max(inp.elapsed_s - max(inp.own_downtime_s, 0.0), _EPS)
    rate = burned / active_s                      # fight-fraction per active second
    remaining_active_s = remaining / rate

    # Reference pick: the kill closest to the naive total, same construction as
    # the theorizer's `_encounter_downtime` (closest-duration ref).
    naive_total = inp.elapsed_s + remaining_active_s
    ref_i = min(range(len(inp.ref_kill_times)),
                key=lambda i: abs(inp.ref_kill_times[i] - naive_total))
    ref_kill_s = inp.ref_kill_times[ref_i]
    ref_windows = (inp.ref_downtime_windows[ref_i]
                   if ref_i < len(inp.ref_downtime_windows) else ())

    # Walk forward from the wipe point through the ref's downtime windows,
    # spending `remaining_active_s` of active wall-clock; downtime is skipped
    # over (the party can't burn during it), extending the projection.
    t = inp.elapsed_s
    left = remaining_active_s
    downtime_beyond = 0.0
    for ws, we in sorted(ref_windows):
        if we <= t:
            continue
        ws = max(ws, t)
        if we <= ws:
            continue
        gap = ws - t                      # active stretch before this window
        if left <= gap:
            break
        left -= gap
        downtime_beyond += we - ws
        t = we
    projected = t + left

    projected = min(max(projected, inp.elapsed_s), _PROJECTION_MAX_S)
    return ProjectedKillTime(
        projected_s=projected,
        method="active_rate_v1",
        elapsed_s=inp.elapsed_s,
        active_s=active_s,
        burned_pct=burned * 100.0,
        remaining_pct=remaining * 100.0,
        downtime_beyond_s=downtime_beyond,
        ref_count=len(inp.ref_kill_times),
        ref_kill_s=ref_kill_s,
    )
