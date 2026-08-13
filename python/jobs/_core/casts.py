"""Shared cast-event normalization used by every aspect.

Wraps the FFLogs cast-event stream into a sorted list of
(t_relative_s, ability_id) tuples, **GCD-aligned to each cast's start**.
Pre-pull casts get negative timestamps; in-fight ones are >= 0.

FFLogs logs a hardcast (cast-time spell) as a `begincast` at the GCD start
followed by a `cast` at completion, while an instant cast / oGCD logs only a
`cast` at execution (= its GCD start). Anchoring every GCD at its *start* —
the `begincast` time for a hardcast, the `cast` time otherwise — makes the
spacing of consecutive GCDs uniform for casters too, so the idle/clip gap-walk
and drift/buff timing see true GCD cadence instead of the alternating
short/long pattern raw `cast`-completion events produce. Instant-only jobs
(MCH/RPR/SAM) emit no `begincast` events, so their normalized stream is
byte-identical to anchoring on `cast`.
"""
from __future__ import annotations

import re
from typing import Any

from .aspect import PRE_PULL_LOOKBACK_S

# Longest hardcast (RDM Verthunder/Veraero III @5s) + slidecast slop. A `cast`
# whose pending `begincast` precedes it by more than this didn't come from that
# begincast (the hardcast was cancelled, then a later instant of the SAME spell
# fired), so it's treated as instant rather than mis-anchored to the stale start.
_MAX_HARDCAST_S = 5.5


def fetch_norm_casts(client: Any, code: str, fight: dict[str, Any],
                     actor: dict[str, Any]) -> list[tuple[float, int]]:
    """Fetch this fight's cast events for `actor` and normalize them.

    Returns a sorted list of (t_seconds_relative_to_fight_start, ability_id),
    one entry per landed `cast`, each anchored to its GCD start (see module
    docstring). The pre-pull look-back lets aspects observe canonical pre-pull
    patterns (Reassemble at -5s, etc.). Cached automatically via
    CachedEventsClient so multiple aspects calling this collapse to one
    paginated round-trip.
    """
    start, end = fight["startTime"], fight["endTime"]
    fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
    cast_events = client.get_events(code, fetch_start, end, actor["id"],
                                     data_type="Casts")
    cast_events.sort(key=lambda e: e["timestamp"])

    max_hardcast_ms = _MAX_HARDCAST_S * 1000.0
    out: list[tuple[float, int]] = []
    pending: tuple[float, int] | None = None   # (begincast_ts_ms, ability_id)
    for ev in cast_events:
        typ = ev.get("type")
        aid = ev.get("abilityGameID")
        if not aid:
            continue
        if typ == "begincast":
            # A new begincast supersedes any unmatched one (the prior hardcast
            # was cancelled — it never landed, so it produces no GCD).
            pending = (ev["timestamp"], aid)
            continue
        if typ != "cast":
            continue
        ts = ev["timestamp"]
        if (pending is not None and pending[1] == aid
                and (ts - pending[0]) <= max_hardcast_ms):
            ts = pending[0]          # hardcast → anchor to its begincast (GCD start)
        pending = None               # cast consumed / orphan begincast dropped
        out.append(((ts - start) / 1000.0, aid))
    _inject_proven_prepull_instants(client, code, fight, actor, out)
    return out


# Injection time for a buff-proven countdown instant. The real press time is
# unrecoverable (FFLogs drops the cast event entirely); any t < 0 satisfies
# every consumer — drift's initial-charge reduction, the entry-gauge
# generation-before-spend ordering, the Timeline pre-zone (the sidecar
# re-aligns the display copy to the sim's canonical precast time).
_PREPULL_INJECT_T_S = -2.0


def _prepull_buff_map(actor: dict[str, Any] | None) -> dict[int, int]:
    """The actor's job `prepull_buff_ids` ({ability_id: status_id}), resolved
    from the FFLogs subType ("DarkKnight" → registry name "Dark Knight").
    Empty for jobs without countdown instants worth reconstructing — the whole
    injection path is then a no-op."""
    sub = (actor or {}).get("subType") or ""
    if not sub:
        return {}
    from jobs import get_job   # lazy: casts.py is imported by the job packages
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sub)
    for name in (spaced, sub):
        try:
            return dict(get_job(name).data.prepull_buff_ids or {})
        except Exception:
            continue
    return {}


def _inject_proven_prepull_instants(client: Any, code: str,
                                    fight: dict[str, Any],
                                    actor: dict[str, Any],
                                    out: list[tuple[float, int]]) -> None:
    """Reconstruct countdown instants FFLogs dropped, INTO the shared stream.

    An instant pressed during the countdown (MCH Reassemble, SAM Meikyo, GNB
    Bloodfest, MNK Form Shift) produces no cast event, but its buff survives:
    the status's first Buffs event in the fight is a remove/refresh, never an
    apply. Injecting the proven cast at a small negative t lets every
    norm_casts consumer see it — the drift detector's initial-charge
    reduction (no more phantom "capped since t=0" on the first in-fight
    press), the entry-gauge walk (pre-pull generation precedes the opener's
    spends), and the Timeline pre-zone. Only ids in the job's
    `prepull_buff_ids` are considered, and never when a real t < 0 cast of
    that id is already in the stream (a local-capture recording that DID see
    the press must not double it)."""
    buff_map = _prepull_buff_map(actor)
    if not buff_map:
        return
    have_prepull = {aid for t, aid in out if t < 0}
    want = {aid: sid for aid, sid in buff_map.items() if aid not in have_prepull}
    if not want:
        return
    start, end = fight["startTime"], fight["endTime"]
    fetch_start = start - int(PRE_PULL_LOOKBACK_S * 1000)
    try:
        auras = client.get_aura_events(code, fetch_start, end, actor["id"],
                                       data_type="Buffs")
    except Exception:
        return
    injected = False
    for ability_id, status_id in want.items():
        # FFLogs encodes a status as either its raw id or 1000000 + id.
        toks = {status_id, 1_000_000 + status_id}
        evs = sorted((e for e in auras if e.get("abilityGameID") in toks),
                     key=lambda e: e.get("timestamp", 0))
        # removebuffstack included: a STACKED countdown buff (SAM Meikyo, 3
        # stacks) first loses a stack, not the whole buff.
        if evs and evs[0].get("type") in ("removebuff", "refreshbuff",
                                          "removebuffstack"):
            out.append((_PREPULL_INJECT_T_S, ability_id))
            injected = True
    if injected:
        out.sort(key=lambda c: c[0])   # stable: same-t order preserved
