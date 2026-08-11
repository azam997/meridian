"""ReassembleAspect — MCH-specific Reassemble alignment finding.

Reassemble grants a guaranteed crit-DH on the next weaponskill. The cost
of misuse is real: a 660p tool with Reassemble effectively becomes a
~858p hit; spending the buff on a 220p combo step wastes the upside.

This was previously folded into the legacy `ExecutionAspect.alignment_findings`
alongside burst-window misalignment. Burst alignment is now job-agnostic
(`jobs._aspects.alignment.AlignmentAspect`); the Reassemble-specific check
lives here as MCH-only because the buff (and the "FMF is already crit-DH"
exemption) are entirely MCH mechanics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs.machinist import data as md


_REASSEMBLE_ID: int = 2876
_FMF_ID: int = 36982

# Valid Reassemble targets — 660p tools where the crit-DH buff converts
# to real damage. FMF is already a guaranteed crit-DH; Reassemble on it
# overlaps uselessly. Heated combo / Blazing Shot are wasted targets
# (low potency, no big upside).
_VALID_REASSEMBLE_TARGETS: frozenset[int] = frozenset({16498, 16500, 25788, 36981})

# Reassemble's effective value when correctly used. ~30% crit-DH uplift
# on a 660p tool ≈ 200p. The same number lives in machinist/data.py as
# COOLDOWN_VALUE_P[2876]; duplicated here for cost accounting.
_REASSEMBLE_VALUE: float = 200.0


@dataclass
class ReassembleFinding:
    """One Reassemble alignment issue."""
    kind: str          # always "reassemble_misalign" for this aspect
    time_s: float
    summary: str
    lost_potency: float


@dataclass
class ReassembleState:
    findings: list[ReassembleFinding] = field(default_factory=list)


def _mmss(seconds: float) -> str:
    if seconds < 0:
        return f"-{int(-seconds)}s"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def detect_reassemble_misalign(
    norm_casts: list[tuple[float, int]],
) -> list[ReassembleFinding]:
    """For each Reassemble cast, the next weaponskill within 5s should be a
    660p tool (Drill, Air Anchor, Chain Saw, Excavator). FMF is wasted
    (already crit-DH). Lower-potency targets cost (660 - actual) × 0.30.

    Pure function — testable with a synthetic norm_casts list.
    """
    findings: list[ReassembleFinding] = []
    casts = list(norm_casts)
    for i, (t, aid) in enumerate(casts):
        if aid != _REASSEMBLE_ID:
            continue
        # Find the next weaponskill within 5s of the buff.
        for j in range(i + 1, len(casts)):
            t2, aid2 = casts[j]
            if t2 - t > 5.0:
                break
            meta = ability_metadata.get_metadata(aid2)
            if meta is None or meta.is_ogcd:
                continue
            if aid2 in _VALID_REASSEMBLE_TARGETS:
                pass    # correct — no finding
            elif aid2 == _FMF_ID:
                findings.append(ReassembleFinding(
                    kind="reassemble_misalign",
                    time_s=t,
                    summary=(f"Reassemble at {_mmss(t)} buffed Full Metal Field "
                             f"— already crit-DH; ~{_REASSEMBLE_VALUE:.0f}p wasted"),
                    lost_potency=_REASSEMBLE_VALUE,
                ))
            else:
                actual_pot = md.POTENCIES.get(aid2, 0)
                cost = max(0.0, (660 - actual_pot) * 0.30)
                name = meta.name if meta else f"action {aid2}"
                findings.append(ReassembleFinding(
                    kind="reassemble_misalign",
                    time_s=t,
                    summary=(f"Reassemble at {_mmss(t)} buffed {name} "
                             f"({actual_pot}p) — prefer Drill/AA/CS/Excavator (660p)"),
                    lost_potency=cost,
                ))
            break
    return findings


class ReassembleAspect:
    """MCH Reassemble-target alignment. Hidden for non-MCH jobs."""

    name = "Reassemble"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        findings = detect_reassemble_misalign(norm_casts)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"findings": findings},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[ReassembleFinding] = you.state.get("findings", []) or []

        finding_lines: list[str] = [
            f"[align] {f.summary}" for f in your_findings if f.lost_potency > 0
        ]
        if not finding_lines:
            finding_lines.append("Reassemble targeting looks clean.")

        detail_columns = ["Time", "Summary", "Lost (p)"]
        rows: list[list[Any]] = []
        for f in your_findings:
            rows.append([_mmss(f.time_s), f.summary, round(f.lost_potency)])

        summary_lines: list[str] = []
        you_total = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Total Reassemble misalign: -{you_total:.0f}p")
        if refs:
            ref_totals = [
                sum(f.lost_potency for f in (r.state.get("findings") or []))
                for r in refs
            ]
            summary_lines.append(
                f"Reference median: -{median(ref_totals):.0f}p"
            )

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )
