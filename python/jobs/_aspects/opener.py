"""Opener-deviation aspect.

Compares the first ~12 in-fight GCDs against a per-job canonical opener.
Cost is `max(0, expected_potency - actual_potency)` — a harmless reorder of
two same-potency abilities contributes 0p; substituting a combo step where
the opener wants a 660p tool contributes the real delta.

Job-agnostic: pulls the canonical opener and per-ability potencies from the
JobData instance handed in at construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import (
    AspectComparison,
    AspectResult,
    Track,
)
from jobs._core.casts import fetch_norm_casts
from jobs._core.job import JobData


@dataclass
class OpenerFinding:
    """One opener-deviation (per slot)."""
    position: int                             # 1-indexed cast position in the opener
    expected_id: int
    actual_id: int
    summary: str
    lost_potency: float


@dataclass
class OpenerState:
    findings: list[OpenerFinding] = field(default_factory=list)


def compute_opener(norm_casts: list[tuple[float, int]],
                   data: JobData) -> list[OpenerFinding]:
    """Diff the first in-fight GCDs against `data.canonical_opener`. Pure
    function — testable without a FFLogsClient."""
    if not data.canonical_opener:
        return []

    in_fight_gcds: list[int] = []
    for t, aid in norm_casts:
        if t < 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        if meta is None or meta.is_ogcd:
            continue
        in_fight_gcds.append(aid)
        if len(in_fight_gcds) >= len(data.canonical_opener):
            break

    findings: list[OpenerFinding] = []
    for i, expected_id in enumerate(data.canonical_opener):
        if i >= len(in_fight_gcds):
            break
        actual_id = in_fight_gcds[i]
        if actual_id == expected_id:
            continue
        expected_meta = ability_metadata.get_metadata(expected_id)
        actual_meta = ability_metadata.get_metadata(actual_id)
        expected_name = expected_meta.name if expected_meta else f"action {expected_id}"
        actual_name = actual_meta.name if actual_meta else f"action {actual_id}"
        expected_p = data.potencies.get(expected_id, 0)
        actual_p = data.potencies.get(actual_id, 0)
        lost = max(0.0, float(expected_p - actual_p))
        if lost > 0:
            summary = (f"Opener slot #{i+1}: cast {actual_name} ({actual_p}p), "
                       f"canonical uses {expected_name} ({expected_p}p) — "
                       f"-{lost:.0f}p")
        else:
            summary = (f"Opener slot #{i+1}: cast {actual_name}, "
                       f"canonical uses {expected_name} (same potency)")
        findings.append(OpenerFinding(
            position=i + 1,
            expected_id=expected_id,
            actual_id=actual_id,
            summary=summary,
            lost_potency=lost,
        ))
    return findings


class OpenerAspect:
    """Opener-deviation aspect. Constructor takes the JobData; analyze
    diffs the recorded opener against `data.canonical_opener`."""

    name = "Opener"

    def __init__(self, data: JobData):
        self._data = data

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        findings = compute_opener(norm_casts, self._data)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"findings": findings},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[OpenerFinding] = you.state.get("findings", []) or []

        finding_lines = [
            f"[opener] {f.summary}" for f in your_findings if f.lost_potency > 0
        ]
        if not finding_lines:
            finding_lines.append("Opener matches canonical (within same-potency reorders).")

        # Detail table: per-deviation row.
        detail_columns = ["Slot", "Expected", "Actual", "Lost (p)"]
        rows: list[list[Any]] = []
        for f in your_findings:
            exp_meta = ability_metadata.get_metadata(f.expected_id)
            act_meta = ability_metadata.get_metadata(f.actual_id)
            rows.append([
                f.position,
                exp_meta.name if exp_meta else f"action {f.expected_id}",
                act_meta.name if act_meta else f"action {f.actual_id}",
                round(f.lost_potency),
            ])

        # Ref comparison: median lost potency across reference runs.
        summary_lines: list[str] = []
        ref_losses = [
            sum(f.lost_potency for f in (r.state.get("findings") or []))
            for r in refs
        ]
        you_loss = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Your opener cost: {you_loss:.0f}p")
        if ref_losses:
            summary_lines.append(
                f"Reference median: {median(ref_losses):.0f}p "
                f"(across {len(refs)} runs)"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )
