"""Observed multi-target casts from a player's DamageDone stream.

A single ability application that hits N enemies emits N damage events that
share one `packetID`. Grouping a player's own DamageDone by packetID therefore
recovers, per cast, how many distinct targets it actually hit — the ground
truth for crediting splash on the delivered side, and (aggregated across refs)
the consensus signal for which windows genuinely afforded multi-target.

`packetID` presence is unverified against a live multi-target log (the FFLogs
`events { data }` blob returns whatever the report carries); fall back to
grouping by (abilityGameID, ~timestamp) when it's absent. Best-effort: returns
() on any fetch error so callers degrade to the single-target number.
"""
from __future__ import annotations

from typing import Any

# Damage events that represent a real application (carry a target + amount).
_DAMAGE_TYPES = ("calculateddamage", "damage")
# Timestamp bucket (ms) for the packetID-absent fallback: hits of one cast land
# within a few frames of each other, well inside this.
_FALLBACK_BUCKET_MS = 100


def _group_packets(
    events: list[dict[str, Any]],
) -> dict[Any, tuple[int, int, set[int]]]:
    """Group damage events into per-cast packets.

    -> {packet_key: (ability_game_id, first_timestamp_ms, {target_id, ...})}.
    Keyed on `packetID` when present, else on (abilityGameID, timestamp//bucket)
    so a cast's multi-target hits still collapse to one packet.
    """
    groups: dict[Any, tuple[int, int, set[int]]] = {}
    for ev in events:
        if ev.get("type") not in _DAMAGE_TYPES:
            continue
        aid = ev.get("abilityGameID")
        tid = ev.get("targetID")
        if aid is None or tid is None:
            continue
        ts = int(ev.get("timestamp", 0) or 0)
        pid = ev.get("packetID")
        key = ("p", pid) if pid is not None else ("a", aid, ts // _FALLBACK_BUCKET_MS)
        existing = groups.get(key)
        if existing is None:
            groups[key] = (int(aid), ts, {int(tid)})
        else:
            existing[2].add(int(tid))
    return groups


def observed_multi_target_casts(
    client: Any, code: str, fight: dict[str, Any], actor: dict[str, Any],
) -> tuple[tuple[float, int, int], ...]:
    """Player casts that hit >= 2 distinct targets, as
    (t_s, ability_id, n_targets) in fight-relative seconds, sorted by time.

    Empty on a pure single-target pull or a fetch failure. Reuses the per-pull
    cached DamageDone stream (warmed by the prefetch bundle), so the call is a
    cache hit. Single-target packets are dropped — they carry no splash and no
    multi-target signal.
    """
    start, end = fight["startTime"], fight["endTime"]
    try:
        dmg = client.get_events(code, start, end, actor["id"],
                                data_type="DamageDone")
    except Exception:
        return ()
    out: list[tuple[float, int, int]] = []
    for aid, ts, targets in _group_packets(dmg).values():
        if len(targets) >= 2:
            out.append(((ts - start) / 1000.0, aid, len(targets)))
    out.sort(key=lambda x: x[0])
    return tuple(out)


def observed_splash_casts(
    client: Any, code: str, fight: dict[str, Any], actor: dict[str, Any],
    splash_ids: frozenset[int] | set[int],
) -> tuple[tuple[float, int, int], ...]:
    """Every cast of a splash-bearing ability and how many targets it hit, as
    (t_s, ability_id, n_targets) — INCLUDING single-target hits (n=1), so the
    "hit fewer than the window afforded" case is visible. Drives the per-cast
    multi-target highlights on the timeline. Empty on a fetch failure or when
    `splash_ids` is empty. Reuses the per-pull-cached DamageDone stream.
    """
    if not splash_ids:
        return ()
    start, end = fight["startTime"], fight["endTime"]
    try:
        dmg = client.get_events(code, start, end, actor["id"],
                                data_type="DamageDone")
    except Exception:
        return ()
    out: list[tuple[float, int, int]] = []
    for aid, ts, targets in _group_packets(dmg).values():
        if aid in splash_ids:
            out.append(((ts - start) / 1000.0, aid, len(targets)))
    out.sort(key=lambda x: x[0])
    return tuple(out)
