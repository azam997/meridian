"""Premade ("PF") mitigation plans — hand-authored per-Ultimate JSON that pins
*which* mit each mechanic uses, fed into the planner so a healer's locked-GCD
analysis aligns with the group's real plan instead of the auto-derived one.

The JSON only says what-per-mechanic; the planner still picks the cast TIMING
(its existing `first_hit - lead` placement). One file per encounter under
``premade/<encounter_id>.json``:

    {
      "encounter_id": 1085,
      "encounter_name": "Dancing Mad (Ultimate)",
      "source": "...",
      "assignments": [
        { "mechanic": "Grand Cross", "name": "Grand Cross", "occurrence": 0,
          "mits": [ {"job": "Scholar", "action_id": 188},
                    {"job": "Sage",    "action_id": 24298} ] },
        ...
      ]
    }

Mechanic match key (per entry): ``boss_ability_id`` (the stable
``Mechanic.boss_ability_ids[0]``) when known, else ``name`` matched against
``Mechanic.name`` (normalized). ``occurrence`` / ``at_sec`` disambiguate a
mechanic that recurs. Mits are keyed on ``(job, action_id)`` — the canonical
library key (``action_id`` alone is not unique across jobs).

Loading is best-effort: an unknown ability is dropped with a warning (surfaced in
the plan response), never a hard failure. The planner itself (``planner.plan``)
does the mechanic-matching + job→slot resolution, since those need the model and
the resolved comp; this module is pure IO + validation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_DIR = Path(__file__).parent / "premade"


@dataclass(frozen=True)
class PinnedEntry:
    """One premade mechanic → its pinned mits (job, action_id pairs)."""
    label: str
    mits: tuple[tuple[str, int], ...]
    boss_ability_id: int | None = None
    name: str | None = None
    occurrence: int | None = None
    at_sec: float | None = None


@dataclass
class PremadePlan:
    encounter_id: int
    encounter_name: str
    entries: tuple[PinnedEntry, ...]
    source: str = ""
    warnings: list[str] = field(default_factory=list)


def _path(encounter_id: int) -> Path:
    return _DIR / f"{int(encounter_id)}.json"


def has_premade(encounter_id: int) -> bool:
    """Cheap existence check (drives the UI's button gate via get_catalog)."""
    try:
        return _path(encounter_id).is_file()
    except Exception:
        return False


def load_premade(encounter_id: int) -> PremadePlan | None:
    """Parse + validate ``premade/<id>.json``. Each ``(job, action_id)`` is
    checked against the mit library; unknown ones are dropped with a warning.
    Returns None if the file is absent or unparseable (analysis proceeds on the
    auto-plan, never blocks)."""
    p = _path(encounter_id)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    from mitplan.library import ROLE_JOBS
    from mitplan.planner import _ACTION_BY_JOB_ID

    warnings: list[str] = []
    entries: list[PinnedEntry] = []
    for row in raw.get("assignments") or []:
        label = str(row.get("mechanic") or row.get("name")
                    or row.get("boss_ability_id") or "?")
        # A mit is keyed by a specific "job" (healer mits) OR a "role"
        # (shared-id party mit — Feint/Addle/Reprisal — resolved to a comp job
        # by the planner). Role selectors are stored with a leading "@".
        mits: list[tuple[str, int]] = []
        for m in row.get("mits") or []:
            try:
                aid = int(m.get("action_id"))
            except (TypeError, ValueError):
                warnings.append(f"PF plan: bad action_id in {label!r}.")
                continue
            role = str(m.get("role") or "").lower()
            job = str(m.get("job") or "")
            if role:
                if role not in ROLE_JOBS:
                    warnings.append(f"PF plan: unknown role {role!r} in {label!r}.")
                    continue
                if not any((j, aid) in _ACTION_BY_JOB_ID for j in ROLE_JOBS[role]):
                    warnings.append(f"PF plan: no {role} job brings #{aid} — "
                                    f"dropped from {label!r}.")
                    continue
                mits.append(("@" + role, aid))
            elif job:
                if (job, aid) not in _ACTION_BY_JOB_ID:
                    warnings.append(f"PF plan: {job} #{aid} is not in the mit "
                                    f"library — dropped from {label!r}.")
                    continue
                mits.append((job, aid))
            else:
                warnings.append(f"PF plan: a mit in {label!r} has no job/role.")
        if not mits:
            continue
        bid = row.get("boss_ability_id")
        occ = row.get("occurrence")
        at = row.get("at_sec")
        entries.append(PinnedEntry(
            label=label, mits=tuple(mits),
            boss_ability_id=int(bid) if bid is not None else None,
            name=str(row["name"]) if row.get("name") else None,
            occurrence=int(occ) if occ is not None else None,
            at_sec=float(at) if at is not None else None,
        ))
    return PremadePlan(
        encounter_id=int(raw.get("encounter_id") or encounter_id),
        encounter_name=str(raw.get("encounter_name") or ""),
        entries=tuple(entries), source=str(raw.get("source") or ""),
        warnings=warnings,
    )
