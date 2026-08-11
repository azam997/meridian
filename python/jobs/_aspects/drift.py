"""Cooldown-drift aspect.

For each cooldown ability declared in `JobData.cooldowns`, computes how
much capped-cooldown time was wasted and translates that to lost potency.

Supports two job-agnostic quirks via JobData:
- **Shared charge pools** (`charge_sharing`): e.g. MCH Bioblaster eats Drill
  charges. When analyzing the source ability, consumers' casts also drain
  charges.
- **Cross-cooldown effects** (`cdr_rules`): e.g. MCH Blazing Shot reduces
  Double Check / Checkmate recasts by 15s per cast. When analyzing a
  target, the source's casts subtract from the remaining cooldown.
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
from jobs._core.job import CDRRule, JobData


@dataclass
class DriftFinding:
    """Per-ability cooldown drift result."""
    ability_id: int
    ability_name: str
    casts: int
    capped_seconds: float
    lost_casts: float
    lost_potency: float
    cdr_overflow_seconds: float = 0.0
    shared_consumers: int = 0    # casts of consumers sharing this ability's charges


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


def _cdr_for(ability_id: int, rules: tuple[CDRRule, ...]) -> CDRRule | None:
    """Return the CDR rule whose `targets` includes `ability_id`, if any.
    (Assumes at most one rule per target — true for MCH; extend if needed.)"""
    for r in rules:
        if ability_id in r.targets:
            return r
    return None


def _consumers_of(source_id: int, sharing: dict[int, int]) -> set[int]:
    """All consumer ability IDs that share `source_id`'s charge pool."""
    return {consumer for consumer, src in sharing.items() if src == source_id}


# --- Core detector ----------------------------------------------------------

def compute_drift_for_ability(
    ability_id: int,
    data: JobData,
    norm_casts: list[tuple[float, int]],
    fight_duration_s: float,
    downtime_windows: list[tuple[float, float]],
    dead_windows: list[tuple[float, float]] | None = None,
) -> DriftFinding:
    """Charges-over-time loop, parameterized on JobData. Pure function —
    fully testable with a synthetic norm_casts list and a JobData instance.

    Same charges-over-time math as the rest of the analyzer, with MCH
    constants replaced by data-driven lookups (charge_sharing, cdr_rules,
    cooldown_value_p, potencies). `dead_windows` (player death) are excluded
    from capped time alongside downtime — a cooldown sitting idle while the
    player is dead isn't drift; death is scored separately."""

    recast_s, max_charges = data.cooldowns[ability_id]
    # Both downtime and death are no-fault for cooldown drift.
    excluded_windows = list(downtime_windows) + list(dead_windows or [])
    consumers = _consumers_of(ability_id, data.charge_sharing) | {ability_id}
    cdr_rule = _cdr_for(ability_id, data.cdr_rules)
    cdr_source = cdr_rule.source if cdr_rule else None
    cdr_reduction_s = cdr_rule.reduction_s if cdr_rule else 0.0

    def is_consumer(aid: int) -> bool:
        return aid in consumers

    def is_cdr_trigger(aid: int) -> bool:
        return cdr_source is not None and aid == cdr_source

    # 1. Pre-pull casts reduce starting charges.
    initial_charges = max_charges
    for t, aid in norm_casts:
        if t >= 0:
            break
        if is_consumer(aid):
            initial_charges -= 1
    initial_charges = max(0, initial_charges)

    # 2. Filter to in-fight events that matter for this detector.
    events: list[tuple[float, int]] = [
        (t, aid) for t, aid in norm_casts
        if t >= 0 and (is_consumer(aid) or is_cdr_trigger(aid))
    ]

    # 3. Charges-over-time.
    charges: float = float(initial_charges)
    last_t: float = 0.0
    cap_start: float | None = 0.0 if initial_charges == max_charges else None
    cap_intervals: list[tuple[float, float]] = []
    cdr_overflow_s: float = 0.0
    casts_count: int = 0
    shared_consumed: int = 0

    for t, aid in events:
        delta = t - last_t
        if cap_start is None:
            new_charges = charges + delta / recast_s
            if new_charges >= max_charges:
                time_to_cap = (max_charges - charges) * recast_s
                cap_start = last_t + time_to_cap
                charges = float(max_charges)
            else:
                charges = new_charges

        if is_cdr_trigger(aid):
            cdr_charge_bonus = cdr_reduction_s / recast_s
            if cap_start is not None:
                cdr_overflow_s += cdr_reduction_s
            else:
                projected = charges + cdr_charge_bonus
                if projected > max_charges:
                    overflow_charge = projected - max_charges
                    cdr_overflow_s += overflow_charge * recast_s
                    charges = float(max_charges)
                    cap_start = t
                else:
                    charges = projected

        if is_consumer(aid):
            if cap_start is not None:
                cap_intervals.append((cap_start, t))
                cap_start = None
            charges = max(0.0, charges - 1.0)
            if aid == ability_id:
                casts_count += 1
            else:
                shared_consumed += 1

        last_t = t

    # 4. Tail interval.
    if last_t < fight_duration_s:
        if cap_start is not None:
            cap_intervals.append((cap_start, fight_duration_s))
        else:
            delta = fight_duration_s - last_t
            new_charges = charges + delta / recast_s
            if new_charges >= max_charges:
                time_to_cap = (max_charges - charges) * recast_s
                cap_intervals.append((last_t + time_to_cap, fight_duration_s))

    # 5. Subtract downtime + death overlap from cap intervals.
    total_capped_s = 0.0
    for cap_start_t, cap_end_t in cap_intervals:
        net = (cap_end_t - cap_start_t) - _overlap_seconds(
            cap_start_t, cap_end_t, excluded_windows)
        total_capped_s += max(0.0, net)

    # Potency is quantized by cast count: you can't lose a *fractional* cast.
    # Wasted/overflowed cooldown time only costs potency once it adds up to a
    # WHOLE recast (a cast you genuinely couldn't fit). Drifting Drill 7s when
    # you still fit every Drill by the kill time costs nothing — so floor the
    # fractional drift to whole casts. `capped_seconds` keeps the continuous
    # drift magnitude for display; the authoritative "you dropped N casts vs
    # the field" signal is the ref-comparative Tools aspect.
    drift_fraction = total_capped_s / recast_s if recast_s > 0 else 0.0
    lost_casts = float(int(drift_fraction))   # whole casts only
    per_cast_value = data.cooldown_value_p.get(
        ability_id, data.potencies.get(ability_id, 0))
    lost_potency = lost_casts * per_cast_value

    meta = ability_metadata.get_metadata(ability_id)
    ability_name = meta.name if meta else f"action {ability_id}"

    return DriftFinding(
        ability_id=ability_id,
        ability_name=ability_name,
        casts=casts_count,
        capped_seconds=total_capped_s,
        lost_casts=lost_casts,
        lost_potency=lost_potency,
        cdr_overflow_seconds=cdr_overflow_s,
        shared_consumers=shared_consumed,
    )


@dataclass
class DriftState:
    findings: list[DriftFinding] = field(default_factory=list)


class DriftAspect:
    """Per-ability cooldown drift across every cooldown in JobData. Returns
    one DriftFinding per non-excluded ability."""

    name = "Drift"

    def __init__(self, data: JobData):
        self._data = data

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        downtime_windows, downtime_source = read_downtime_from_report(
            report, norm_casts, fight_duration_s,
        )
        dead_windows = read_deaths_from_report(report)

        findings: list[DriftFinding] = []
        for ability_id in self._data.cooldowns:
            if ability_id in self._data.drift_exclusions:
                continue
            findings.append(compute_drift_for_ability(
                ability_id, self._data,
                norm_casts, fight_duration_s, downtime_windows,
                dead_windows=dead_windows,
            ))

        # Worst first for the UI.
        findings.sort(key=lambda f: -f.lost_potency)

        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={
                "findings": findings,
                "downtime_windows": downtime_windows,
                "downtime_source": downtime_source,
                "fight_duration_s": fight_duration_s,
            },
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[DriftFinding] = you.state.get("findings", []) or []

        # Per-ability ref median for the Δ column.
        per_ability_ref_loss: dict[int, list[float]] = {}
        for r in refs:
            for f in (r.state.get("findings") or []):
                per_ability_ref_loss.setdefault(f.ability_id, []).append(f.lost_potency)

        finding_lines: list[str] = []
        detail_columns = ["Ability", "Casts", "Drift (s)", "Lost (p)",
                          "Ref avg (p)", "Δ"]
        rows: list[list[Any]] = []
        for f in your_findings:
            ref_med = (
                median(per_ability_ref_loss[f.ability_id])
                if f.ability_id in per_ability_ref_loss else 0.0
            )
            delta = f.lost_potency - ref_med
            rows.append([
                f.ability_name,
                f.casts,
                round(f.capped_seconds, 1),
                round(f.lost_potency),
                round(ref_med),
                f"{delta:+.0f}",
            ])
            if f.lost_potency >= 100:
                finding_lines.append(
                    f"[drift] {f.ability_name}: {f.casts} casts, "
                    f"drifted {f.capped_seconds:.1f}s ≈ -{f.lost_potency:.0f}p "
                    f"(ref avg -{ref_med:.0f}p)"
                )

        if not finding_lines:
            finding_lines.append("No notable cooldown drift.")

        summary_lines: list[str] = []
        you_total = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Total drift cost: -{you_total:.0f}p")
        if refs:
            ref_totals = [
                sum(f.lost_potency for f in (r.state.get("findings") or []))
                for r in refs
            ]
            summary_lines.append(
                f"Reference median: -{median(ref_totals):.0f}p "
                f"(across {len(refs)} runs)"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )
