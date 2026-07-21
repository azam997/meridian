"""VPR per-pull buff measurement: Hunter's Instinct coverage.

Hunter's Instinct is Viper's maintained +10% personal-damage SELF-buff (granted by
Hunter's Sting / Hunter's Coil) — the SAM-Fugetsu analog, NOT an enemy debuff like
RPR Death's Design. The idealized ceiling assumes full coverage; here we measure
the ACTUAL coverage from the Hunter's-Instinct token in the `buffs` snapshot of the
player's own DamageDone events, so a dropped/late buff costs efficiency (and at
100% uptime the x1.10 cancels in the delivered/idealized ratio).

Also exposes `reawaken_windows`, the spans of the reduced-recast Reawakened combo
(Generations + Ouroboros), which the scorer feeds to `gcd_inference_exclusions` so
those fast GCDs don't contaminate the per-player GEAR-GCD inference (BLM Ley Lines
pattern — the ceiling already models the reduced recast in `gcd_duration`).
"""
from __future__ import annotations

from typing import Any

from jobs.viper import data as vd
from jobs.viper.simulator import REAWAKEN_FAST_IDS


# FFLogs encodes a status in a damage event's `buffs` string as (1000000 + id).
_FFLOGS_BUFF_OFFSET = 1000000
# A buffed hit credits coverage forward to the next hit, capped so a pause
# (movement / downtime) doesn't credit a long empty span.
_HIT_HORIZON_S = 4.0
# Group fast Reawakened GCDs within this gap into one excluded window.
_REAWAKEN_RUN_GAP_S = 3.0


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            ls, le = out[-1]
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def measured_hunters_instinct_intervals(
        client, code: str, report: dict[str, Any], fight: dict[str, Any],
        actor: dict[str, Any]) -> list[tuple[float, float, float]]:
    """Hunter's Instinct coverage as a `(start, end, 1.10)` timeline, reconstructed
    from the buff token in the `buffs` snapshot of the player's DamageDone events.
    Falls back to full coverage (assume 100%, no penalty) if the stream is
    unavailable or carries no token — never penalize on missing data."""
    s, e = fight["startTime"], fight["endTime"]
    duration = (e - s) / 1000.0
    full = [(-10.0, duration + 1.0, vd.HUNTERS_INSTINCT_MULT)]
    tokens = {str(vd.HUNTERS_INSTINCT_STATUS_ID),
              str(_FFLOGS_BUFF_OFFSET + vd.HUNTERS_INSTINCT_STATUS_ID)}

    try:
        dmg = client.get_events(code, s, e, actor["id"], data_type="DamageDone")
    except Exception:
        return full

    pts: list[tuple[float, bool]] = []
    for ev in dmg:
        if ev.get("type") != "calculateddamage":
            continue
        bs = (ev.get("buffs") or "").split(".")
        t = (ev.get("timestamp", s) - s) / 1000.0
        pts.append((t, any(tok in bs for tok in tokens)))
    pts.sort(key=lambda x: x[0])
    if not pts or not any(has for _, has in pts):
        return full

    covered: list[tuple[float, float]] = []
    for (t0, has0), (t1, _h1) in zip(pts, pts[1:]):
        if has0 and t1 > t0:
            covered.append((t0, min(t1, t0 + _HIT_HORIZON_S)))
    if pts[-1][1]:
        covered.append((pts[-1][0], min(pts[-1][0] + _HIT_HORIZON_S, duration)))
    return [(cs, ce, vd.HUNTERS_INSTINCT_MULT) for cs, ce in _union(covered)]


def hunters_instinct_coverage_pct(intervals: list[tuple[float, float, float]],
                                  duration_s: float) -> float:
    """Coverage % over the fight span (for the human-facing state alias)."""
    covered = sum(min(e, duration_s) - max(s, 0.0)
                  for s, e, _m in intervals if e > 0 and s < duration_s)
    return round(100.0 * covered / duration_s, 1) if duration_s > 0 else 100.0


def reawaken_windows(norm_casts) -> list[tuple[float, float]]:
    """Spans of the reduced-recast Reawakened combo (Generations + Ouroboros) in the
    player's casts, for the GEAR-GCD inference exclusion. Consecutive fast GCDs
    (within `_REAWAKEN_RUN_GAP_S`) form one window, padded a GCD on the entry side
    to also drop the Reawaken->First-Generation boundary pair."""
    fast = sorted(t for t, aid in norm_casts if aid in REAWAKEN_FAST_IDS)
    if not fast:
        return []
    runs: list[tuple[float, float]] = []
    start = prev = fast[0]
    for t in fast[1:]:
        if t - prev > _REAWAKEN_RUN_GAP_S:
            runs.append((start, prev))
            start = t
        prev = t
    runs.append((start, prev))
    # Pad: a main GCD before the first fast GCD (the entry boundary pair) and a
    # small tail after the last so the closing fast->normal pair is excluded too.
    from jobs.viper.simulator import VPR_GCD_S
    return [(s - VPR_GCD_S - 0.1, e + 0.6) for s, e in runs]
