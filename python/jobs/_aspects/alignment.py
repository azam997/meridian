"""Burst-window alignment aspect.

Models the standard 2-min raid burst cadence (~20s windows) and flags each
`JobData.burst_abilities` cast that landed just BEFORE a window as
shiftable. The cost of the miss is the potency it would have gained inside
the window.

Composition-aware: the benefit (and whether we say anything at all) is
driven by the *actual party comp* of the pull, read from
`masterData.actors[].subType`. If the party has no raid-buff providers we
emit nothing — the analyzer never implies "bring a Dancer", it only talks
about aligning your own cooldowns to the buffs that were really present.
When the comp can't be determined (e.g. an aspect invoked outside the
pipeline with no report), it falls back to a conservative synthetic ~10%
uplift so the detector still functions.

Job-agnostic: the only per-job inputs are `JobData.burst_abilities`
(which tools to watch) and `JobData.potencies` (for cost).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs._core.job import JobData
from jobs._core.raid_buffs import (
    combined_multiplier,
    burst_window_duration_s,
    present_providers,
)


# --- Tunables (job-agnostic defaults) --------------------------------------

# Standard FFXIV 2-min raid-buff cadence.
SYNTHETIC_BURST_INTERVAL_S: float = 120.0
SYNTHETIC_BURST_DURATION_S: float = 20.0

# Cast within Ns BEFORE a window start counts as shiftable.
BURST_SHIFT_WINDOW_S: float = 8.0

# Approximate raid-buff multiplier the shift would have captured. 10% is the
# conservative end of "average composition" — under-estimate cost rather
# than overstate.
SHIFT_BENEFIT_MULTIPLIER: float = 0.10

# Noise floor — don't flag shifts worth less than this many potency.
NOISE_FLOOR_P: float = 40.0


@dataclass
class AlignmentFinding:
    kind: str                 # "burst_misalign" for this aspect
    time_s: float
    summary: str
    lost_potency: float


def _synthetic_burst_windows(fight_duration_s: float,
                             window_s: float = SYNTHETIC_BURST_DURATION_S,
                             ) -> list[tuple[float, float]]:
    """First burst at 2:00, `window_s` each, then every 2min thereafter."""
    out: list[tuple[float, float]] = []
    t = SYNTHETIC_BURST_INTERVAL_S
    while t < fight_duration_s:
        end = min(t + window_s, fight_duration_s)
        out.append((t, end))
        t += SYNTHETIC_BURST_INTERVAL_S
    return out


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def compute_burst_alignment(
    norm_casts: list[tuple[float, int]],
    fight_duration_s: float,
    data: JobData,
    party_jobs: list[str] | None = None,
) -> list[AlignmentFinding]:
    """Pure function. Empty list if `data.burst_abilities` is empty.

    `party_jobs` is the list of job names in the pull's party:
      * ``None`` — comp unknown. Falls back to the synthetic ~10% uplift so
        the detector still works outside the pipeline.
      * a list with raid-buff providers — benefit and window length come
        from the buffs *actually present*.
      * a list with no providers — returns ``[]``. With no party damage
        buffs there is nothing to align to, and we will not invent one.
    """
    if not data.burst_abilities:
        return []

    if party_jobs is None:
        uplift = SHIFT_BENEFIT_MULTIPLIER
        window_s = SYNTHETIC_BURST_DURATION_S
        source_note = "synthetic 2-min window"
    else:
        provs = present_providers(party_jobs)
        if not provs:
            return []
        uplift = combined_multiplier(party_jobs) - 1.0
        window_s = burst_window_duration_s(party_jobs)
        source_note = "+".join(p.name for p in provs)

    windows = _synthetic_burst_windows(fight_duration_s, window_s)

    findings: list[AlignmentFinding] = []
    for t, aid in norm_casts:
        if t < 0 or aid not in data.burst_abilities:
            continue
        next_burst_start: float | None = None
        for ws, _ in windows:
            if ws > t:
                next_burst_start = ws
                break
        if next_burst_start is None:
            continue
        gap = next_burst_start - t
        if gap > BURST_SHIFT_WINDOW_S:
            continue
        meta = ability_metadata.get_metadata(aid)
        name = meta.name if meta else f"action {aid}"
        potency = data.potencies.get(aid, 660)
        benefit = potency * uplift
        if benefit < NOISE_FLOOR_P:
            continue
        findings.append(AlignmentFinding(
            kind="burst_misalign",
            time_s=t,
            summary=(f"{name} at {_mmss(t)} could have shifted into burst "
                     f"window at {_mmss(next_burst_start)} "
                     f"(~+{benefit:.0f}p; {source_note})"),
            lost_potency=benefit,
        ))
    return findings


@dataclass
class AlignmentState:
    findings: list[AlignmentFinding] = field(default_factory=list)


def party_jobs_from_report(report: dict[str, Any] | None,
                           fight: dict[str, Any]) -> list[str] | None:
    """The job names of the friendly players in this fight, or ``None`` if
    the comp can't be determined (no report / no actors). Used to drive
    composition-aware buff modeling — see `compute_burst_alignment`."""
    if not report:
        return None
    actors = ((report.get("masterData") or {}).get("actors")) or []
    if not actors:
        return None
    friendly = set(fight.get("friendlyPlayers") or [])
    jobs = [a.get("subType") for a in actors
            if a.get("type") == "Player" and a.get("id") in friendly
            and a.get("subType")]
    return jobs or None


class AlignmentAspect:
    """Burst-window alignment. Hidden when JobData has no `burst_abilities`
    declared (other jobs without burst-relevant tools). Benefit is scaled
    to the pull's actual raid-buff providers; emits nothing when the party
    has none."""

    name = "Alignment"

    def __init__(self, data: JobData):
        self._data = data

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        party_jobs = party_jobs_from_report(report, fight)
        findings = compute_burst_alignment(
            norm_casts, fight_duration_s, self._data, party_jobs)
        providers = (present_providers(party_jobs)
                     if party_jobs is not None else [])
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={
                "findings": findings,
                "comp_known": party_jobs is not None,
                "providers": [p.name for p in providers],
            },
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[AlignmentFinding] = you.state.get("findings", []) or []

        finding_lines: list[str] = [
            f"[align] {f.summary}" for f in your_findings
        ]
        if not finding_lines:
            comp_known = you.state.get("comp_known", False)
            providers = you.state.get("providers") or []
            if comp_known and not providers:
                finding_lines.append(
                    "No raid-buff providers in this party — "
                    "burst alignment N/A."
                )
            else:
                finding_lines.append(
                    "No burst-alignment opportunities flagged."
                )

        detail_columns = ["Time", "Summary", "Benefit (p)"]
        rows: list[list[Any]] = []
        for f in your_findings:
            rows.append([_mmss(f.time_s), f.summary, round(f.lost_potency)])

        summary_lines: list[str] = []
        you_total = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Total potential benefit: +{you_total:.0f}p")
        if refs:
            ref_totals = [
                sum(f.lost_potency for f in (r.state.get("findings") or []))
                for r in refs
            ]
            summary_lines.append(
                f"Reference median: +{median(ref_totals):.0f}p"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )
