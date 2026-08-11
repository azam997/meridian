"""LifeSurgeAspect — DRG-specific Life Surge alignment finding.

Life Surge grants a guaranteed CRITICAL hit (crit only, no direct hit) on the next
weaponskill. The cost of misuse is real: the guaranteed crit converts to the most
damage on a 460-potency finisher (Heavens' Thrust / Drakesbane); spending it on a
low-potency combo step throws away the difference. With 2 charges every 40s the
buff is plentiful — the skill is landing it on the big hits.

Mirrors the MCH `ReassembleAspect`: a guaranteed-crit-on-next-weaponskill buff with
a small set of correct targets, priced by the crit uplift forgone on a mistarget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from jobs._core import ability_metadata
from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.casts import fetch_norm_casts
from jobs.dragoon import data as dd


_LIFE_SURGE_ID: int = dd.LIFE_SURGE

# Correct Life Surge targets — the 460-potency finishers where the guaranteed crit
# converts to the most damage.
_VALID_TARGETS: frozenset[int] = frozenset({dd.HEAVENS_THRUST, dd.DRAKESBANE})
_BEST_TARGET_POTENCY: int = 460
# Crit uplift fraction (crit-only multiplier - 1) — the share of a hit's potency the
# guaranteed crit adds. Kept in sync with data.GUARANTEED_CRIT_MULT.
_CRIT_UPLIFT: float = dd.GUARANTEED_CRIT_MULT - 1.0


@dataclass
class LifeSurgeFinding:
    """One Life Surge alignment issue."""
    kind: str          # always "lifesurge_misalign" for this aspect
    time_s: float
    summary: str
    lost_potency: float


@dataclass
class LifeSurgeState:
    findings: list[LifeSurgeFinding] = field(default_factory=list)


def _mmss(seconds: float) -> str:
    if seconds < 0:
        return f"-{int(-seconds)}s"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def detect_lifesurge_misalign(
    norm_casts: list[tuple[float, int]],
) -> list[LifeSurgeFinding]:
    """For each Life Surge cast, the next weaponskill within 5s (the buff window)
    should be a 460-potency finisher (Heavens' Thrust / Drakesbane). A lower-potency
    target costs `(460 - actual) x crit_uplift`.

    Pure function — testable with a synthetic norm_casts list."""
    findings: list[LifeSurgeFinding] = []
    casts = list(norm_casts)
    for i, (t, aid) in enumerate(casts):
        if aid != _LIFE_SURGE_ID:
            continue
        # Find the next weaponskill within the 5s buff window.
        for j in range(i + 1, len(casts)):
            t2, aid2 = casts[j]
            if t2 - t > dd.LIFE_SURGE_WINDOW_S:
                break
            meta = ability_metadata.get_metadata(aid2)
            if (meta is not None and meta.is_ogcd) or aid2 not in dd.GCD_WEAPONSKILLS:
                continue
            if aid2 in _VALID_TARGETS:
                pass    # correct — no finding
            else:
                actual_pot = dd.POTENCIES.get(aid2, 0)
                cost = max(0.0, (_BEST_TARGET_POTENCY - actual_pot) * _CRIT_UPLIFT)
                name = meta.name if meta else f"action {aid2}"
                findings.append(LifeSurgeFinding(
                    kind="lifesurge_misalign",
                    time_s=t,
                    summary=(f"Life Surge at {_mmss(t)} crit {name} ({actual_pot}p) "
                             f"— prefer Heavens' Thrust / Drakesbane (460p)"),
                    lost_potency=cost,
                ))
            break
    return findings


class LifeSurgeAspect:
    """DRG Life Surge-target alignment. Hidden for non-DRG jobs."""

    name = "LifeSurge"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        norm_casts = fetch_norm_casts(client, code, fight, actor)
        findings = detect_lifesurge_misalign(norm_casts)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"findings": findings},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your_findings: list[LifeSurgeFinding] = you.state.get("findings", []) or []

        finding_lines: list[str] = [
            f"[align] {f.summary}" for f in your_findings if f.lost_potency > 0
        ]
        if not finding_lines:
            finding_lines.append("Life Surge targeting looks clean.")

        detail_columns = ["Time", "Summary", "Lost (p)"]
        rows: list[list[Any]] = []
        for f in your_findings:
            rows.append([_mmss(f.time_s), f.summary, round(f.lost_potency)])

        summary_lines: list[str] = []
        you_total = sum(f.lost_potency for f in your_findings)
        summary_lines.append(f"Total Life Surge misalign: -{you_total:.0f}p")
        if refs:
            ref_totals = [
                sum(f.lost_potency for f in (r.state.get("findings") or []))
                for r in refs
            ]
            summary_lines.append(f"Reference median: -{median(ref_totals):.0f}p")

        return AspectComparison(
            aspect_name=self.name,
            findings=finding_lines,
            detail_columns=detail_columns,
            your_detail_rows=rows,
            summary_lines=summary_lines,
        )


def improvements_from_lifesurge(state: dict) -> list:
    """Priced cards for mistargeted Life Surge (one per misalign). Empty when clean."""
    from jobs._core.improvements import Improvement
    out: list = []
    for f in state.get("findings", []) or []:
        if f.lost_potency <= 0:
            continue
        out.append(Improvement(
            kind="lifesurge", ability_id=_LIFE_SURGE_ID, ability_name="Life Surge",
            time_s=f.time_s, lost_potency=float(f.lost_potency), summary=f.summary))
    return out
