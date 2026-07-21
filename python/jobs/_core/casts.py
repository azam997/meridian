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
    return out
