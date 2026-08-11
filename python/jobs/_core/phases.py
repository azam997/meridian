"""Boss phase segmentation for phasic (per-phase) analysis.

Pure helpers over an already-fetched report summary — no client, no network.
A fight's `phaseTransitions` (report-relative ms, one entry per phase START)
plus the fight span give per-phase ``[start, end)`` intervals; the report-level
``phases`` block supplies the human-readable names. Everything degrades to an
empty tuple when the fight carries no transitions (single-phase Savage), so
non-phased pulls emit no phase data and stay byte-identical.

Fight-relative seconds throughout (subtract ``fight["startTime"]`` from the
report-relative ms and divide by 1000), matching every other timing consumer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Phase:
    """One boss phase, in fight-relative seconds. ``[start_s, end_s)``."""
    id: int
    name: str
    start_s: float
    end_s: float
    is_intermission: bool


def encounter_phase_names(report: dict, encounter_id: int | None) -> dict[int, tuple[str, bool]]:
    """`{phase_id: (name, is_intermission)}` from the report-level ``phases``
    block. FFLogs returns one ``EncounterPhases`` entry per encounter in the
    report; match on ``encounterID`` (falling back to the sole entry when the
    id doesn't match but only one block exists). Empty dict when the report
    lacks the block (a pre-v4-cache summary, or a non-phased encounter)."""
    blocks = (report or {}).get("phases") or []
    chosen: dict | None = None
    for b in blocks:
        if b.get("encounterID") == encounter_id:
            chosen = b
            break
    if chosen is None and len(blocks) == 1:
        chosen = blocks[0]
    out: dict[int, tuple[str, bool]] = {}
    for p in ((chosen or {}).get("phases") or []):
        pid = p.get("id")
        if pid is None:
            continue
        name = p.get("name") or f"P{pid}"
        out[int(pid)] = (name, bool(p.get("isIntermission")))
    return out


def phase_segments(report: dict, fight: dict, *, full_end_ms: int | None = None) -> tuple[Phase, ...]:
    """Per-phase ``[start_s, end_s)`` intervals for a fight, in fight-relative
    seconds.

    ``phaseTransitions`` is ``[{id, startTime}]`` with report-relative ms START
    times (one per phase). We sort by start time, drop duplicate timestamps,
    convert to fight-relative seconds, and close each phase at the next
    transition — the last at ``full_end_ms`` (the FULL wipe span, so a wipe's
    phases still cover the phases the raid reached) or, absent that, the fight
    end. The opening phase is anchored to 0 so no leading gap goes unattributed
    even if the first transition lags the pull start by a hair.

    Returns ``()`` when the fight has no transitions — the caller then emits no
    phase data at all (Savage pulls stay byte-identical)."""
    transitions = (fight or {}).get("phaseTransitions") or ()
    if not transitions:
        return ()
    fight_start = fight["startTime"]
    end_ms = full_end_ms if full_end_ms is not None else fight["endTime"]

    # Sort by start time and drop duplicate timestamps (defensive against a
    # doubled transition event); keep the first id seen at each timestamp.
    rows: list[tuple[float, object]] = []
    seen_ms: set[float] = set()
    for t in sorted(transitions, key=lambda t: (t.get("startTime") if t.get("startTime") is not None else 0)):
        ms = t.get("startTime")
        if ms is None or ms in seen_ms:
            continue
        seen_ms.add(ms)
        rows.append((ms, t.get("id")))
    if not rows:
        return ()

    names = encounter_phase_names(report, (fight or {}).get("encounterID"))
    segs: list[Phase] = []
    for i, (start_ms, pid) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else end_ms
        s = 0.0 if i == 0 else max(0.0, (start_ms - fight_start) / 1000.0)
        e = max(s, (nxt - fight_start) / 1000.0)
        key = int(pid) if pid is not None else -1
        name, is_int = names.get(key, ("", False))
        if not name:
            name = f"P{pid}" if pid is not None else f"Phase {i + 1}"
        segs.append(Phase(
            id=int(pid) if pid is not None else i + 1,
            name=name, start_s=s, end_s=e, is_intermission=is_int,
        ))
    return tuple(segs)


def downtime_overlap_s(phase: Phase, windows) -> float:
    """Seconds of `windows` (an iterable of ``(start_s, end_s)``) overlapping
    the phase's ``[start_s, end_s)``."""
    total = 0.0
    for w0, w1 in windows:
        lo = max(phase.start_s, w0)
        hi = min(phase.end_s, w1)
        if hi > lo:
            total += hi - lo
    return total


def split_casts_by_phase(norm_casts, phases: tuple[Phase, ...]) -> list[list]:
    """Bucket ``(t_s, ability_id, …)`` casts into a list parallel to `phases`
    (each entry a list of the casts whose ``t_s`` falls in that phase). The
    last phase is inclusive of its end so a trailing cast at the fight end
    isn't dropped."""
    buckets: list[list] = [[] for _ in phases]
    if not phases:
        return buckets
    last = len(phases) - 1
    for cast in norm_casts:
        t = cast[0]
        for i, p in enumerate(phases):
            if p.start_s <= t < p.end_s or (i == last and t >= p.start_s):
                buckets[i].append(cast)
                break
    return buckets
