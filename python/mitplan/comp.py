"""Resolve a logged pull's party composition into the planner's comp shape.

The planner wants exactly (shield healer, regen healer, 2 tanks, 4 DPS) with
library job names ("White Mage"); FFLogs reports spaceless subTypes
("WhiteMage"). This module bridges the two — including nearest-valid fallbacks
for non-standard comps (double shield / double regen healers, odd counts) so a
pull can always seed a plan, with the substitutions surfaced as warnings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mitplan.library import (ALL_JOB_NAMES, DPS_JOBS, REGEN_HEALERS,
                             SHIELD_HEALERS, TANK_JOBS)

_DEFAULT_TANKS = ("Paladin", "Dark Knight")
_DEFAULT_DPS = ("Samurai", "Dragoon", "Bard", "Pictomancer")
_SPACELESS = {name.replace(" ", ""): name for name in ALL_JOB_NAMES}


@dataclass
class CompResolution:
    shield_healer: str
    regen_healer: str
    tanks: list[str]
    dps: list[str]
    source: str = "pull"
    warnings: list[str] = field(default_factory=list)


def canonical_job_name(sub_type: str) -> str | None:
    """FFLogs spaceless subType -> library job name (None for non-combat
    subTypes like "LimitBreak")."""
    return _SPACELESS.get((sub_type or "").replace(" ", ""))


def slot_for_job(job: str) -> str | None:
    """The healer-duo slot a job plans into ("H1" shield / "H2" regen)."""
    if job in SHIELD_HEALERS:
        return "H1"
    if job in REGEN_HEALERS:
        return "H2"
    return None


def resolve_comp_from_fight(report: dict[str, Any], fight: dict[str, Any],
                            anchor_job: str | None = None) -> CompResolution:
    """The pull's 8-job comp in planner shape. `anchor_job` (the analyzed
    player's job) is kept in its own slot when a non-standard duo forces a
    substitution — their half of the plan must stay theirs."""
    from jobs._core.buff_windows import party_jobs_in_fight

    jobs = [j for j in (canonical_job_name(s)
                        for s in party_jobs_in_fight(report, fight))
            if j is not None]
    warnings: list[str] = []

    tanks = [j for j in jobs if j in TANK_JOBS]
    shields = [j for j in jobs if j in SHIELD_HEALERS]
    regens = [j for j in jobs if j in REGEN_HEALERS]
    dps = [j for j in jobs if j in DPS_JOBS]

    if len(jobs) < 8:
        warnings.append(f"Only {len(jobs)} players found in this pull — "
                        "missing slots filled with defaults.")

    # Healer duo. Standard = one shield + one regen; otherwise keep the
    # anchor's own job in its slot and substitute a default for the other.
    if shields and regens:
        shield, regen = shields[0], regens[0]
        if anchor_job in shields:
            shield = anchor_job
        if anchor_job in regens:
            regen = anchor_job
    elif regens:            # double-regen (or regen-only)
        regen = anchor_job if anchor_job in regens else regens[0]
        shield = "Sage"
        warnings.append("Non-standard healer duo (no shield healer) — "
                        "planned with Sage in the shield slot.")
    elif shields:           # double-shield (or shield-only)
        shield = anchor_job if anchor_job in shields else shields[0]
        regen = "White Mage"
        warnings.append("Non-standard healer duo (no regen healer) — "
                        "planned with White Mage in the regen slot.")
    else:
        shield, regen = "Sage", "White Mage"
        warnings.append("No healers found in this pull — planned with the "
                        "default duo.")

    # Tanks / DPS: pad from defaults (skipping duplicates), trim extras.
    for d in _DEFAULT_TANKS:
        if len(tanks) >= 2:
            break
        if d not in tanks:
            tanks.append(d)
    if len(tanks) > 2:
        warnings.append(f"{len(tanks)} tanks found — planning with the "
                        "first two.")
    for d in _DEFAULT_DPS:
        if len(dps) >= 4:
            break
        if d not in dps:
            dps.append(d)
    if len(dps) > 4:
        warnings.append(f"{len(dps)} DPS found — planning with the first four.")

    return CompResolution(shield_healer=shield, regen_healer=regen,
                          tanks=tanks[:2], dps=dps[:4],
                          source="pull", warnings=warnings)
