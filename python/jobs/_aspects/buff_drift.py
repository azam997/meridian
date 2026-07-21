"""Party-buff drift — context, not blame.

The observed-circumstance ceiling (idealized under the buffs that actually
landed) and the master ceiling (idealized under buffs on the perfect 2-min
cadence) can diverge when the *party* used its raid buffs late, short, or
not at all. That gap isn't the analyzed player's fault — they may have burst
perfectly on time. This aspect surfaces the divergence as context so the
dashboard can explain "your master-efficiency gap is partly party-buff
timing, not you".

Deliberately conservative: it reports missing uses and clearly drifted gaps
(well beyond the 2-min cadence), and never assigns lost potency to the
player. It is job-agnostic — it reads the pull's comp and observed buff
windows, nothing job-specific.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jobs._core.aspect import AspectComparison, AspectResult, Track
from jobs._core.buff_windows import (
    BURST_CADENCE_S,
    fetch_observed_buff_windows,
    party_jobs_in_fight,
)
from jobs._core.raid_buffs import present_providers

# A consecutive-use gap beyond this reads as drift (the buff is on a ~120s
# cooldown; a gap much larger than that means a use slipped).
_GAP_DRIFT_S: float = BURST_CADENCE_S + 30.0
# Allow the count to be one short before flagging (end-of-fight bursts that
# never come around are normal).
_COUNT_SLACK: int = 1


@dataclass
class BuffDriftFinding:
    kind: str            # "missing" | "gap"
    provider: str
    time_s: float
    summary: str


@dataclass
class BuffDriftState:
    findings: list[BuffDriftFinding] = field(default_factory=list)


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def compute_buff_drift(observed_windows: list,
                       present_jobs: list[str],
                       fight_duration_s: float) -> list[BuffDriftFinding]:
    """Pure function. Compare observed raid-buff windows to the expected
    2-minute cadence, per present provider. Flags providers used fewer times
    than the cadence implies, and consecutive uses spaced well beyond 120s."""
    provs = present_providers(present_jobs)
    if not provs:
        return []
    # Expected burst count: opener + every 120s that fits.
    expected_bursts = sum(1 for i in range(50)
                          if i * BURST_CADENCE_S < fight_duration_s)

    by_provider: dict[str, list[float]] = {}
    for w in observed_windows:
        by_provider.setdefault(w.label, []).append(w.start_s)

    findings: list[BuffDriftFinding] = []
    for prov in provs:
        starts = sorted(by_provider.get(prov.name, []))
        # Missing uses (only meaningful when we actually observed some — an
        # empty list usually means the buff stream wasn't captured, not that
        # the provider never bursted).
        if starts and len(starts) < expected_bursts - _COUNT_SLACK:
            findings.append(BuffDriftFinding(
                kind="missing", provider=prov.name, time_s=0.0,
                summary=(f"{prov.name}: {len(starts)} uses observed vs "
                         f"~{expected_bursts} on a 2-min cadence — a party "
                         f"burst was skipped or very late (context).")))
        # Drifted gaps between consecutive uses.
        for a, b in zip(starts, starts[1:]):
            if b - a > _GAP_DRIFT_S:
                findings.append(BuffDriftFinding(
                    kind="gap", provider=prov.name, time_s=a,
                    summary=(f"{prov.name} gap of {b - a:.0f}s "
                             f"({_mmss(a)}→{_mmss(b)}) vs the ~120s cadence — "
                             f"party buff drifted (context).")))
    return findings


class BuffDriftAspect:
    """Party-buff timing context. Hidden when the party has no raid-buff
    providers or no buff data was captured."""

    name = "BuffDrift"

    def analyze(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any]) -> AspectResult:
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        observed = fetch_observed_buff_windows(client, code, report, fight,
                                               actor["id"])
        present_jobs = party_jobs_in_fight(report, fight)
        findings = compute_buff_drift(observed, present_jobs, fight_duration_s)
        return AspectResult(
            name=self.name,
            track=Track(name=self.name, events=[]),
            state={"findings": findings},
        )

    def compare(self, you: AspectResult,
                refs: list[AspectResult]) -> AspectComparison:
        your: list[BuffDriftFinding] = you.state.get("findings", []) or []
        lines = [f"[buffctx] {f.summary}" for f in your]
        if not lines:
            lines.append("Party raid buffs landed on the expected cadence "
                         "(no timing drift detected).")
        rows = [[f.kind, f.provider, _mmss(f.time_s), f.summary] for f in your]
        return AspectComparison(
            aspect_name=self.name,
            findings=lines,
            detail_columns=["Kind", "Provider", "Time", "Detail"],
            your_detail_rows=rows,
        )
