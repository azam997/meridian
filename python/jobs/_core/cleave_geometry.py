"""Position-derived cleave feasibility for the confirmed multi-target windows —
ADVISORY ONLY: verdicts drive a UI chip + the frontend's auto-deny default and
never touch any potency, ceiling, or credit math (enforced structurally — no
credit path imports this module).

Data source (probed live — scripts/probe_enemy_positions.py, 2026-07-16):
FFLogs attaches `targetResources.x/y/facing` to 100% of damage rows onto
enemies when `includeResources: true` is requested, in CENTI-YALMS on the
standard map grid (10000 == 100.00y). One UNSOURCED window-scoped DamageDone
stream therefore samples every enemy any party member hit — the boss at
~8 samples/s, adds at ~0.6-4/s. Enemies are near-stationary between samples,
so nearest-in-time pairing within a couple of seconds is sound.

The question answered per window: on THIS pull, were >= 2 targetable enemies
ever within splash reach of each other — i.e. could a target-centered cleave
have hit a second enemy at all?

  * reachable    — enemy pairs sat within reach for a meaningful share of the
                   paired samples.
  * unreachable  — with a deliberately generous reach (kit max radius + enemy
                   hitbox allowance + margin) NO paired sample was ever within
                   it. The frontend auto-defaults such a window to
                   "Not possible" (user can override).
  * unknown      — thin/ambiguous evidence (too few concurrent samples,
                   positions seen for only one enemy, or the fetch failed).
                   Behaves exactly like today: no chip, no auto-deny.

Reach = the job's best cleave option: max over its splash/AoE kit of
`JobData.aoe_radii_yalm` (lines/cones stored as circles of their length —
generous, biasing AWAY from auto-denying) with a 5y default (the standard
target-centered splash circle), plus a hitbox allowance (enemy rings are
unknown to FFLogs) and a margin.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

# FFLogs resource coordinates are centi-yalms (probe finding #2).
COORD_PER_YALM = 100.0
# The standard "and all enemies nearby it" splash circle.
DEFAULT_SPLASH_RADIUS_YALM = 5.0
# Enemy hitbox rings are not in the log; a cleave centered on A hits B when
# dist(A, B) <= radius + B's ring. Generous allowance (boss rings run ~5y+),
# biasing toward reachable/unknown — an auto-deny needs clear separation.
HITBOX_ALLOWANCE_YALM = 4.0
MARGIN_YALM = 1.0
# Two enemies' samples pair when within this many seconds of each other.
PAIR_DT_S = 2.0
# Fewer paired samples than this in a window -> unknown (thin evidence).
MIN_PAIR_SAMPLES = 4
# At least this share of paired samples within reach -> reachable.
REACHABLE_FRACTION = 0.25


def job_reach_yalm(data: Any) -> float:
    """The job's best-case cleave reach: max radius across its splash/AoE kit
    (default 5y each) + hitbox allowance + margin."""
    ids = set(data.splash_potencies) | set(data.aoe_potencies)
    radii = [float(data.aoe_radii_yalm.get(aid, DEFAULT_SPLASH_RADIUS_YALM))
             for aid in ids] or [DEFAULT_SPLASH_RADIUS_YALM]
    return max(radii) + HITBOX_ALLOWANCE_YALM + MARGIN_YALM


def sample_enemy_positions(client: Any, code: str, fight: dict,
                           windows: list[dict],
                           ) -> dict[int, list[tuple[float, float, float]]]:
    """Per-enemy position tracks `{enemy_id: [(t_s, x_yalm, y_yalm), ...]}`
    sampled from party-wide DamageDone inside the given serialized windows
    (`{"startSec", "endSec", ...}` dicts). One aliased bundle round trip
    (unsourced stream per window, `includeResources`). Raises on fetch failure
    — callers treat any exception as "unknown"."""
    from fflogs_api import BundleStream
    from jobs._core.downtime_sources import resolve_enemy_actor_ids

    enemy_ids = resolve_enemy_actor_ids(fight)
    start = fight["startTime"]
    streams = [BundleStream("DamageDone",
                            start + int(float(w["startSec"]) * 1000),
                            start + int(float(w["endSec"]) * 1000),
                            include_resources=True)
               for w in windows]
    bundles = client.get_event_bundle(code, streams)
    track: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for evs in bundles:
        for ev in evs:
            if ev.get("type") not in ("damage", "calculateddamage"):
                continue
            if ev.get("targetID") not in enemy_ids:
                continue
            res = ev.get("targetResources")
            if not isinstance(res, dict):
                continue
            x, y = res.get("x"), res.get("y")
            if x is None or y is None:
                continue
            t = (ev["timestamp"] - start) / 1000.0
            track[ev["targetID"]].append(
                (t, float(x) / COORD_PER_YALM, float(y) / COORD_PER_YALM))
    for pts in track.values():
        pts.sort()
    return dict(track)


def window_verdict(start_s: float, end_s: float,
                   positions: dict[int, list[tuple[float, float, float]]],
                   reach_yalm: float) -> tuple[str, str]:
    """`(verdict, detail)` for one window — see the module docstring for the
    verdict semantics. `detail` is the chip tooltip."""
    per_enemy = {
        tid: [(t, x, y) for t, x, y in pts if start_s <= t < end_s]
        for tid, pts in positions.items()
    }
    per_enemy = {tid: pts for tid, pts in per_enemy.items() if pts}
    if len(per_enemy) < 2:
        return ("unknown",
                f"positions sampled for only {len(per_enemy)} enemy"
                f"{'' if len(per_enemy) == 1 else 'ies'} in this window")

    dists: list[float] = []
    tids = sorted(per_enemy)
    for i, a in enumerate(tids):
        for b in tids[i + 1:]:
            for ta, xa, ya in per_enemy[a]:
                near = [(tb, xb, yb) for tb, xb, yb in per_enemy[b]
                        if abs(tb - ta) <= PAIR_DT_S]
                if not near:
                    continue
                tb, xb, yb = min(near, key=lambda p: abs(p[0] - ta))
                dists.append(((xa - xb) ** 2 + (ya - yb) ** 2) ** 0.5)
    if len(dists) < MIN_PAIR_SAMPLES:
        return ("unknown",
                f"only {len(dists)} concurrent position samples in this window")

    in_reach = sum(1 for d in dists if d <= reach_yalm)
    frac = in_reach / len(dists)
    min_d = min(dists)
    if in_reach == 0:
        return ("unreachable",
                f"targets never within {reach_yalm:.0f}y of each other "
                f"(closest {min_d:.1f}y across {len(dists)} samples)")
    if frac >= REACHABLE_FRACTION:
        return ("reachable",
                f"targets within {reach_yalm:.0f}y for {round(frac * 100)}% of "
                f"{len(dists)} samples (closest {min_d:.1f}y)")
    return ("unknown",
            f"targets only briefly within {reach_yalm:.0f}y "
            f"({round(frac * 100)}% of {len(dists)} samples)")
