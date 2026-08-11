"""GCD-pacing aspect — separates *time spent idle* from true *GCD clipping*.

Estimates the run's effective GCD from a median of in-bounds inter-cast gaps,
then walks adjacent regular-weaponskill pairs and splits any excess gap
(gap − effective GCD) into two distinct, separately-priced phenomena:

  * **Time spent idle** — the GCD came back late and the player wasn't holding
    a weave to blame: a gap the pilot simply left empty (movement, reaction,
    target swaps). This is the bulk of what the old detector lumped under
    "clipping".

  * **GCD clipping** — the next GCD was pushed *late* because an oGCD's
    animation lock spilled past the moment the GCD was ready. Weaving up to two
    oGCDs under a 2.5s GCD is safe; a third (or a late weave) animation-locks
    the player past GCD-ready and clips the global. The realized clip is how
    far the last oGCD in the pair locked past `t1 + effective_gcd`.

Both cost throughput at the same rate (a fraction of a GCD slot delayed off the
end of the fight ≈ `seconds / effective_gcd × avg GCD potency`), but they're
different mistakes with different fixes, so we report and price them apart.

Pure GCD-timing math — no per-job special cases beyond the `clip_exclusions`
set (abilities whose recast doesn't match the global GCD, e.g. MCH Blazing Shot
at 1.5s during Hypercharge) and `clip_skip_windows` (stretches excluded wholesale).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.deaths import read_deaths_from_report
from jobs._core.downtime import read_downtime_from_report
from jobs._core.job import JobData

# Standard oGCD animation lock. A weave whose lock ends after the GCD is ready
# delays (clips) the next GCD by the overrun. ~0.6s is the canonical lock; the
# practical floor with latency is a touch higher, which is why a third weave
# under a 2.5s GCD usually clips.
OGCD_ANIM_LOCK_S = 0.6
# Sub-this excess is rounding noise, not a mistake.
_EXCESS_TOL_S = 0.05


@dataclass
class ClippingFinding:
    effective_gcd_s: float = 2.5
    avg_gcd_potency: float = 350.0

    # --- Time spent idle (GCD gaps not explained by weaving) ---------------
    total_idle_s: float = 0.0
    idle_lost_gcds: float = 0.0
    idle_lost_potency: float = 0.0
    # (time_s, idle_seconds) for the worst pairs, sorted desc.
    worst_idle: list[tuple[float, float]] = field(default_factory=list)

    # --- True GCD clipping (oGCD over-weave pushed the next GCD late) -------
    total_clip_s: float = 0.0
    clip_lost_gcds: float = 0.0
    clip_lost_potency: float = 0.0
    # (time_s, clip_seconds, n_ogcds_in_pair) for the worst pairs, sorted desc.
    worst_clips: list[tuple[float, float, int]] = field(default_factory=list)


# --- Helpers ----------------------------------------------------------------

def _overlap_seconds(interval_start: float, interval_end: float,
                     windows: list[tuple[float, float]]) -> float:
    total = 0.0
    for w_start, w_end in windows:
        a = max(interval_start, w_start)
        b = min(interval_end, w_end)
        if b > a:
            total += b - a
    return total


def _in_intervals(t: float, intervals: list[tuple[float, float]]) -> bool:
    for s, e in intervals:
        if s <= t < e:
            return True
    return False


def _avg_gcd_potency(norm_casts: list[tuple[float, int]],
                     data: JobData) -> float:
    """Weighted mean potency of weaponskills cast in-fight. Falls back to a
    rough 350p (combo / tools mix) if no data."""
    total = 0
    n = 0
    for t, aid in norm_casts:
        if t < 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        if meta is None or meta.is_ogcd:
            continue
        pot = data.potencies.get(aid, 0)
        if pot <= 0:
            continue
        total += pot
        n += 1
    return total / n if n > 0 else 350.0


# --- Core detector ----------------------------------------------------------

def compute_clipping(
    norm_casts: list[tuple[float, int]],
    fight_duration_s: float,
    downtime_windows: list[tuple[float, float]],
    skip_intervals: list[tuple[float, float]],
    data: JobData,
    dead_windows: list[tuple[float, float]] | None = None,
) -> ClippingFinding:
    """Pure function. Splits GCD-gap excess into idle vs. true clip.

    `skip_intervals` is e.g. MCH's Hypercharge windows where Blazing Shot's
    1.5s recast would otherwise read as a gap problem. `dead_windows` (player
    death → resurrection) and `downtime_windows` are subtracted from each gap —
    while dead or with no target the player casts nothing, so the empty time is
    no-fault for both idle and clipping (death is priced separately by the
    improvements panel)."""
    avg_gcd_pot = _avg_gcd_potency(norm_casts, data)
    # Idle/clip excused exactly like downtime: it's no-fault.
    no_fault_windows = list(downtime_windows) + list(dead_windows or [])

    # 1. Split in-fight casts into regular weaponskills (the GCD spine) and
    #    oGCD weaves (the things that can clip the spine).
    regular_gcds: list[tuple[float, int]] = []
    ogcd_times: list[float] = []
    for t, aid in norm_casts:
        if t < 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        if meta is not None and meta.is_ogcd:
            ogcd_times.append(t)
            continue
        if aid in data.clip_exclusions:
            continue
        if _in_intervals(t, skip_intervals):
            continue
        regular_gcds.append((t, aid))

    if len(regular_gcds) < 4:
        return ClippingFinding(effective_gcd_s=2.5, avg_gcd_potency=avg_gcd_pot)

    regular_gcds.sort()
    ogcd_times.sort()
    recast_mult = data.gcd_recast_mult

    # 2. Estimate the STANDARD effective GCD: median of in-bounds gaps that FOLLOW a
    #    standard-recast GCD only. A job whose kit mixes GCD speeds (Viper's 2.0s
    #    Generations / 3.0s Coils / 3.5s Uncoiled Fury) would otherwise bias the
    #    estimate off its true 2.5s-equivalent. Empty map -> all gaps, byte-identical.
    raw_gaps = [regular_gcds[i + 1][0] - regular_gcds[i][0]
                for i in range(len(regular_gcds) - 1)
                if recast_mult.get(regular_gcds[i][1], 1.0) == 1.0]
    filtered = [g for g in raw_gaps if 1.5 <= g <= 5.0]
    if filtered:
        effective_gcd_s = max(2.0, min(2.6, median(filtered)))
    else:
        effective_gcd_s = 2.5

    # 3. Walk adjacent pairs (excluding pairs that span a skip interval — the
    #    resync after, e.g., Hypercharge is hard to score without mechanic-level
    #    modeling, so silent > wrong). Classify each pair's excess as clip (a
    #    weave's animation lock spilled past GCD-ready) vs. idle (the rest).
    total_idle_s = 0.0
    total_clip_s = 0.0
    idle_events: list[tuple[float, float]] = []
    clip_events: list[tuple[float, float, int]] = []
    for i in range(len(regular_gcds) - 1):
        t1, a1 = regular_gcds[i]
        t2 = regular_gcds[i + 1][0]
        if any(t1 < skip_end and t2 > skip_start
               for skip_start, skip_end in skip_intervals):
            continue
        gap = (t2 - t1) - _overlap_seconds(t1, t2, no_fault_windows)
        # The from-GCD's OWN recast is the baseline (eff GCD x its recast multiple):
        # a 3.0s Coil or 3.5s Uncoiled Fury naturally leaves a longer gap than the
        # 2.5s standard, which is NOT idle. Default 1.0 -> byte-identical for the
        # uniform-GCD jobs.
        expected = effective_gcd_s * recast_mult.get(a1, 1.0)
        excess = gap - expected
        if excess <= _EXCESS_TOL_S:
            continue

        ogcds_in = [o for o in ogcd_times if t1 < o < t2]
        clip = 0.0
        if ogcds_in:
            ready = t1 + expected
            weave_overrun = (ogcds_in[-1] + OGCD_ANIM_LOCK_S) - ready
            clip = max(0.0, min(excess, weave_overrun))
        idle = excess - clip

        if clip > _EXCESS_TOL_S:
            total_clip_s += clip
            clip_events.append((t1, clip, len(ogcds_in)))
        if idle > _EXCESS_TOL_S:
            total_idle_s += idle
            idle_events.append((t1, idle))

    idle_events.sort(key=lambda x: -x[1])
    clip_events.sort(key=lambda x: -x[1])

    idle_lost_gcds = total_idle_s / effective_gcd_s if effective_gcd_s > 0 else 0.0
    clip_lost_gcds = total_clip_s / effective_gcd_s if effective_gcd_s > 0 else 0.0

    return ClippingFinding(
        effective_gcd_s=effective_gcd_s,
        avg_gcd_potency=avg_gcd_pot,
        total_idle_s=total_idle_s,
        idle_lost_gcds=idle_lost_gcds,
        idle_lost_potency=idle_lost_gcds * avg_gcd_pot,
        worst_idle=idle_events[:8],
        total_clip_s=total_clip_s,
        clip_lost_gcds=clip_lost_gcds,
        clip_lost_potency=clip_lost_gcds * avg_gcd_pot,
        worst_clips=clip_events[:8],
    )


class ClippingAspect:
    name = "Clipping"

    def __init__(self, data: JobData):
        self._data = data

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        downtime, downtime_source = read_downtime_from_report(
            report, norm_casts, fight_duration_s,
        )
        # `fetch_norm_casts` anchors every GCD at its start (begincast for
        # hardcasts), so caster cast cadence is now uniform and the idle/clip
        # gap-walk runs for casters too — no per-job short-circuit needed.
        dead_windows = read_deaths_from_report(report)
        # Job-declared skip windows (e.g. MCH Hypercharge): each in-fight
        # cast of `source_id` spawns a [t, t + window_s] exclusion.
        skip_intervals: list[tuple[float, float]] = []
        for t, aid in norm_casts:
            if t < 0:
                continue
            window_s = self._data.clip_skip_windows.get(aid)
            if window_s is not None:
                skip_intervals.append((t, t + window_s))

        finding = compute_clipping(
            norm_casts, fight_duration_s, downtime, skip_intervals, self._data,
            dead_windows=dead_windows,
        )

        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"clipping": finding,
                   "downtime_source": downtime_source},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        f: ClippingFinding | None = you.state.get("clipping")
        if f is None:
            return AspectComparison(
                aspect_name=self.name,
                findings=["No pacing data."],
            )

        # Detail table: worst clip pairs (the novel, located signal).
        detail_columns = ["#", "Time (s)", "Clip (s)", "oGCDs"]
        rows: list[list[Any]] = []
        for i, (t, clip, n) in enumerate(f.worst_clips, 1):
            rows.append([i, round(t, 1), round(clip, 2), n])

        finding_lines: list[str] = []
        if f.total_idle_s >= 0.5:
            finding_lines.append(
                f"[idle] {f.total_idle_s:.1f}s idle "
                f"({f.idle_lost_gcds:.1f} GCDs) ≈ -{f.idle_lost_potency:.0f}p"
            )
        if f.total_clip_s >= 0.3:
            finding_lines.append(
                f"[clip] {f.total_clip_s:.1f}s GCD clipping from over-weaving "
                f"({f.clip_lost_gcds:.1f} GCDs) ≈ -{f.clip_lost_potency:.0f}p"
            )
        if not finding_lines:
            finding_lines.append(
                f"Clean GCD pacing — eff GCD {f.effective_gcd_s:.2f}s, "
                f"no notable idle time or clipping."
            )

        summary_lines = [
            f"Effective GCD: {f.effective_gcd_s:.2f}s",
            f"Avg GCD potency: {f.avg_gcd_potency:.0f}p",
        ]

        # Cross-ref: median idle+clip cost vs you.
        ref_costs = []
        for r in refs:
            rf = r.state.get("clipping")
            if rf:
                ref_costs.append(rf.idle_lost_potency + rf.clip_lost_potency)
        if ref_costs:
            summary_lines.append(
                f"Reference median idle+clip cost: -{median(ref_costs):.0f}p"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )
