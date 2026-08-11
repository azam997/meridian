"""Surging Tempest — Warrior's maintained 10% personal-damage self-buff.

Storm's Eye applies Surging Tempest to the WARRIOR (a 10% amp on the player's own
damage, 30s → bankable 60s; Inner Release extends it). The idealized rotation
holds it at 100% uptime; this module measures the *actual* coverage from the
player's own damage events so the delivered score is scaled by what really
happened — a dropped/late Surging Tempest then costs efficiency and surfaces as a
located finding.

This is the WAR analog of RPR's Death's Design — same `coverage_intervals`
machinery — but Surging Tempest is a self-BUFF (a status on the player) rather
than a debuff on the boss. FFLogs encodes the status in each damage event's
`buffs` snapshot as `(1000000 + statusID)`, so the player's own DamageDone stream
is the source either way (no enemy debuff stream needed).

`measured_st_intervals` is the multiplier-interval set the scorer multiplies in
(shape identical to a raid-buff `(start, end, multiplier)` timeline). It's fetched
once and reused by both the Scoring aspect (for the delivered multiplier) and the
SurgingTempestAspect (for the finding); the underlying `get_events` call is cached
per-pull, so the second caller is free.
"""
from __future__ import annotations

from typing import Any

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.improvements import Improvement
from jobs.warrior import data as wd


def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent (start, end) intervals."""
    out: list[tuple[float, float]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            ls, le = out[-1]
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def _complement(covered: list[tuple[float, float]],
                span: tuple[float, float]) -> list[tuple[float, float]]:
    """Gaps in [span] not in `covered` (which must be unioned/sorted)."""
    lo, hi = span
    gaps: list[tuple[float, float]] = []
    cur = lo
    for s, e in covered:
        if e <= lo or s >= hi:
            continue
        s, e = max(s, lo), min(e, hi)
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        gaps.append((cur, hi))
    return gaps


def _overlap_len(a: list[tuple[float, float]],
                 b: list[tuple[float, float]]) -> float:
    """Total length of the intersection of two interval sets."""
    total = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            lo, hi = max(s1, s2), min(e1, e2)
            if hi > lo:
                total += hi - lo
    return total


# FFLogs encodes a status in a damage event's `buffs` string as
# (1000000 + statusID). Surging Tempest (a self-buff) shows up in the buffs of the
# player's own hits.
_FFLOGS_BUFF_OFFSET = 1000000
# A Surging-Tempest-bearing hit credits coverage to the next hit up to this long
# after it (caps the span when the player pauses, e.g. movement / downtime).
_ST_HIT_HORIZON_S = 4.0


def measured_st_intervals(client, code: str, report: dict[str, Any],
                          fight: dict[str, Any], actor: dict[str, Any]
                          ) -> list[tuple[float, float, float]]:
    """Surging Tempest coverage as a `(start, end, 1.10)` timeline, reconstructed
    from the Surging Tempest token in the `buffs` snapshot of the player's
    DamageDone events.

    Each hit whose buffs include Surging Tempest marks it up at that instant; the
    span to the next hit (capped at `_ST_HIT_HORIZON_S`) is credited as covered.
    Falls back to **full coverage** (assume 100%, no penalty) if the stream is
    unavailable or carries no Surging Tempest token — never penalize on missing
    data.
    """
    s, e = fight["startTime"], fight["endTime"]
    duration = (e - s) / 1000.0
    full = [(-10.0, duration + 1.0, wd.SURGING_TEMPEST_MULT)]
    tokens = {str(wd.SURGING_TEMPEST_STATUS_ID),
              str(_FFLOGS_BUFF_OFFSET + wd.SURGING_TEMPEST_STATUS_ID)}

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
            covered.append((t0, min(t1, t0 + _ST_HIT_HORIZON_S)))
    if pts[-1][1]:
        covered.append((pts[-1][0], min(pts[-1][0] + _ST_HIT_HORIZON_S, duration)))
    return [(cs, ce, wd.SURGING_TEMPEST_MULT) for cs, ce in _union(covered)]


def _st_coverage(client, code: str, fight: dict[str, Any], actor: dict[str, Any],
                 report: dict[str, Any]) -> dict[str, Any]:
    """Compute coverage %, uncovered uptime windows, and the lost-amp potency."""
    duration = (fight["endTime"] - fight["startTime"]) / 1000.0
    st = measured_st_intervals(client, code, report, fight, actor)
    covered = _union([(s, e) for s, e, _m in st])

    downtime = list(((report.get("__downtime__") or {}).get("windows")) or [])
    uptime = _complement(_union(downtime), (0.0, duration))
    uptime_total = sum(e - s for s, e in uptime) or duration

    covered_uptime = _overlap_len(covered, uptime)
    coverage_pct = round(100.0 * covered_uptime / uptime_total, 1) if uptime_total else 100.0
    uncovered = [(s, e) for s, e in _complement(covered, (0.0, duration))
                 if any(us <= (s + e) / 2 < ue for us, ue in uptime)]

    norm_casts = fetch_norm_casts(client, code, fight, actor)
    # Per-window buckets (uncovered windows are disjoint, so each cast lands in
    # at most one) — `uncovered_lost` parallels `uncovered_windows` and feeds
    # the improvement card's per-window children. Additive: the historic keys
    # keep their exact shape/meaning.
    lost = 0.0
    uncovered_lost = [0.0] * len(uncovered)
    for t, aid in norm_casts:
        if t < 0:
            continue
        for i, (s, e) in enumerate(uncovered):
            if s <= t < e:
                amp = wd.POTENCIES.get(aid, 0) * (wd.SURGING_TEMPEST_MULT - 1.0)
                lost += amp
                uncovered_lost[i] += amp
                break

    return {
        "coverage_pct": coverage_pct,
        "covered_uptime_s": round(covered_uptime, 1),
        "uptime_s": round(uptime_total, 1),
        "uncovered_windows": uncovered,
        "uncovered_lost": [round(x, 1) for x in uncovered_lost],
        "lost_potency": round(lost, 1),
    }


class SurgingTempestAspect:
    """Measures Surging Tempest uptime on the Warrior. Drives the dashboard
    Surging Tempest card and a priced [surging-tempest] improvement card (via the
    WAR improvement contributor)."""

    name = "SurgingTempest"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        state = _st_coverage(client, code, fight, actor, report)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state=state,
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        cov = float(you.state.get("coverage_pct", 100.0))
        findings: list[str] = []
        if cov < 99.0:
            findings.append(
                f"[surging-tempest] Surging Tempest uptime {cov:.1f}% — "
                f"refresh Storm's Eye before it falls off")
        return AspectComparison(aspect_name=self.name, findings=findings)


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def improvements_from_surging_tempest(state: dict) -> list[Improvement]:
    """A priced card for the damage lost to Surging Tempest downtime — the 10%
    amp missed on everything cast while it wasn't up. Located at the first
    uncovered window; with 2+ windows the card carries one located child per
    window (a single window keeps the card a directly-jumpable leaf).
    Zero-priced (no card) at full uptime."""
    lost = float(state.get("lost_potency", 0.0) or 0.0)
    if lost <= 0.0:
        return []
    uncovered = state.get("uncovered_windows") or []
    t0 = uncovered[0][0] if uncovered else 0.0
    cov = float(state.get("coverage_pct", 100.0))
    # Old-shape states (no `uncovered_lost`) degrade to a childless card.
    per_window = state.get("uncovered_lost") or []
    children: list[Improvement] = []
    if len(uncovered) >= 2 and len(per_window) == len(uncovered):
        for (s, e), wl in zip(uncovered, per_window):
            children.append(Improvement(
                kind="surging_tempest", ability_id=wd.STORMS_EYE,
                ability_name="Surging Tempest", time_s=float(s),
                lost_potency=float(wl),
                summary=f"{_mmss(s)}–{_mmss(e)}: Surging Tempest down "
                        f"{e - s:.0f}s — refresh with Storm's Eye before it "
                        f"expires"))
    return [Improvement(
        kind="surging_tempest", ability_id=wd.STORMS_EYE,
        ability_name="Surging Tempest", time_s=t0, lost_potency=lost,
        summary=f"Surging Tempest dropped to {cov:.1f}% uptime — "
                f"the 10% amp was missing from {_mmss(t0)}",
        children=children)]
