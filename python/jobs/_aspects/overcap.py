"""Gauge-overcap aspect.

Runs the overcap detector once per `GaugeModel` declared in JobData.gauges.
Each generator cast that would push a gauge past its cap produces an
OvercapFinding priced via the gauge's `value_p_per_unit`.

Fully data-driven: no per-job code beyond the GaugeModel list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.job import GaugeModel, JobData


@dataclass
class OvercapFinding:
    gauge: str
    time_s: float
    ability_id: int
    ability_name: str
    wasted: int
    lost_potency: float


# --- Core detector ----------------------------------------------------------

def _named(aid: int) -> str:
    meta = ability_metadata.get_metadata(aid)
    return meta.name if meta else f"action {aid}"


def compute_overcap_for_gauge(
    norm_casts: list[tuple[float, int]],
    gauge: GaugeModel,
) -> list[OvercapFinding]:
    """Walk casts (including pre-pull) maintaining the gauge counter. Each
    generator cast that pushes past the LIVE cap produces a finding. Spenders
    reset to 0 ("all") or subtract a fixed amount. Pre-pull casts feed the
    counter but don't fire findings (only in-fight overcaps count).

    `gauge.cap_boosts` models a temporary cap increase (GNB Bloodfest: 3 -> 6 for
    30s). While a boost is active the generator check uses the raised cap; when it
    lapses, units held above the BASE cap are lost and priced as their own finding
    (the "let the bonus expire" waste), mirroring the simulator's cap clamp. Without
    any cap_boosts this is byte-identical to the static-cap detector."""
    findings: list[OvercapFinding] = []
    current = 0
    base_cap = gauge.cap
    cap = base_cap
    boost_end = float("inf")   # no active boost
    boost_src = -1
    boost_name = ""
    boosts = gauge.cap_boosts

    for t, aid in norm_casts:
        # 1. Realize a lapsed cap boost: units above the base cap are lost at expiry.
        if t >= boost_end:
            if current > base_cap and boost_end >= 0:
                wasted = current - base_cap
                findings.append(OvercapFinding(
                    gauge=gauge.name, time_s=boost_end, ability_id=boost_src,
                    ability_name=boost_name, wasted=wasted,
                    lost_potency=wasted * gauge.value_p_per_unit))
            current = min(current, base_cap)
            cap = base_cap
            boost_end = float("inf")

        # 2. Activate a cap boost (before this cast's own generation, so its units
        #    fit the raised cap).
        if boosts and aid in boosts:
            cap, dur = boosts[aid][0], boosts[aid][1]
            boost_end = t + dur
            boost_src, boost_name = aid, _named(aid)

        # 3. Generator / spender, against the live cap.
        if aid in gauge.generators:
            projected = current + gauge.generators[aid]
            if projected > cap and t >= 0:
                wasted = projected - cap
                findings.append(OvercapFinding(
                    gauge=gauge.name, time_s=t, ability_id=aid,
                    ability_name=_named(aid), wasted=wasted,
                    lost_potency=wasted * gauge.value_p_per_unit))
            current = min(cap, projected)
        elif aid in gauge.spenders:
            spend = gauge.spenders[aid]
            if isinstance(spend, str) and spend == "all":
                current = 0
            elif isinstance(spend, (int, float)):
                current = max(0, current - int(spend))
    return findings


def compute_overcap(norm_casts: list[tuple[float, int]],
                    data: JobData) -> list[OvercapFinding]:
    out: list[OvercapFinding] = []
    for gauge in data.gauges:
        out.extend(compute_overcap_for_gauge(norm_casts, gauge))
    return out


@dataclass
class OvercapState:
    findings: list[OvercapFinding] = field(default_factory=list)


class OvercapAspect:
    """Gauge-overcap detector for every GaugeModel in JobData. Produces one
    OvercapFinding per overcap event."""

    name = "Overcap"

    def __init__(self, data: JobData):
        self._data = data

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        findings = compute_overcap(norm_casts, self._data)
        findings.sort(key=lambda f: -f.lost_potency)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"findings": findings},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[OvercapFinding] = you.state.get("findings", []) or []

        # Total per-gauge cost.
        per_gauge: dict[str, float] = {}
        for f in your_findings:
            per_gauge[f.gauge] = per_gauge.get(f.gauge, 0.0) + f.lost_potency

        finding_lines: list[str] = []
        detail_columns = ["Time", "Gauge", "Ability", "Wasted", "Lost (p)"]
        rows: list[list[Any]] = []
        for f in your_findings:
            rows.append([
                _mmss(f.time_s), f.gauge, f.ability_name,
                f.wasted, round(f.lost_potency),
            ])
            if f.lost_potency >= 50:
                finding_lines.append(
                    f"[overcap] {f.gauge.capitalize()} overcap at "
                    f"{_mmss(f.time_s)} ({f.ability_name}, -{f.lost_potency:.0f}p)"
                )

        if not finding_lines:
            finding_lines.append("No notable gauge overcap.")

        summary_lines: list[str] = []
        you_total = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Total overcap cost: -{you_total:.0f}p")
        for gauge_name, cost in per_gauge.items():
            summary_lines.append(f"  {gauge_name.capitalize()}: -{cost:.0f}p")

        if refs:
            ref_totals = [
                sum(f.lost_potency for f in (r.state.get("findings") or []))
                for r in refs
            ]
            summary_lines.append(
                f"Reference median total: -{median(ref_totals):.0f}p"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    if s < 0:
        return f"-{-s}s"
    return f"{s // 60}:{s % 60:02d}"
