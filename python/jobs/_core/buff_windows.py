"""Raid-buff windows and time-varying damage multipliers.

Job-agnostic engine that turns raid-buff presence into a `(start, end,
multiplier)` timeline the scorer and simulator can apply uniformly. Two
sources of windows feed the same machinery:

  * **observed** — the buffs that actually landed in *this* pull, from
    FFLogs events (party buffs on the player ∪ boss debuffs). This is the
    "circumstances you faced" lens: a fair, player-accountable ceiling.
  * **expected** — buffs assumed to land on the standard 2-minute cadence,
    regardless of how the party actually played. This is the "if everyone
    played perfectly" lens: the master-optimal ceiling.

Both reduce to a list of `BuffWindow`s, which `multiplier_intervals`
collapses into non-overlapping segments whose multiplier is the product of
every buff active in that segment. `multiplier_at(t, …)` is what the scorer
calls per cast.

Reusable across jobs — nothing here knows about Machinist. The per-job
scorer decides how a cast's potency is scaled by `multiplier_at`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .raid_buffs import (
    PROVIDER_BUFFS,
    present_providers,
    resolve_cast_action_id,
    resolve_status_ids,
)

# Standard FFXIV burst cadence: opener, then every two minutes.
BURST_CADENCE_S: float = 120.0


@dataclass(frozen=True)
class BuffWindow:
    start_s: float
    end_s: float
    multiplier: float
    label: str        # provider buff name, for drift reporting / debugging


# --- Observed windows (from real events) -----------------------------------

def pair_aura_intervals(events: list[dict],
                        fight_start_ms: int,
                        fight_end_ms: int) -> list[tuple[float, float]]:
    """Pair apply/refresh → remove events for ONE status into intervals
    (start_s, end_s) relative to fight start. An interval left open at fight
    end auto-closes. Robust to refresh-without-apply (treat as still on) and
    remove-without-apply (ignored)."""
    fight_end_s = (fight_end_ms - fight_start_ms) / 1000.0
    evs = sorted(events, key=lambda e: e["timestamp"])
    out: list[tuple[float, float]] = []
    open_start: float | None = None
    for ev in evs:
        typ = ev.get("type", "")
        t = (ev["timestamp"] - fight_start_ms) / 1000.0
        if typ in ("applybuff", "applydebuff", "refreshbuff", "refreshdebuff"):
            if open_start is None:
                open_start = t
        elif typ in ("removebuff", "removedebuff"):
            if open_start is not None:
                out.append((open_start, t))
                open_start = None
    if open_start is not None:
        out.append((open_start, fight_end_s))
    return out


def observed_windows_from_events(
    events: list[dict],
    status_map: dict[int, "object"],
    fight_start_ms: int,
    fight_end_ms: int,
) -> list[BuffWindow]:
    """Build BuffWindows from aura events. `status_map` maps a status
    (FFLogs `abilityGameID`) to its `BuffProvider`. Events for statuses not
    in the map are ignored. Each paired interval becomes one BuffWindow at
    the provider's multiplier."""
    by_status: dict[int, list[dict]] = {}
    for ev in events:
        sid = ev.get("abilityGameID")
        if sid in status_map:
            by_status.setdefault(sid, []).append(ev)
    out: list[BuffWindow] = []
    for sid, evs in by_status.items():
        prov = status_map[sid]
        for s, e in pair_aura_intervals(evs, fight_start_ms, fight_end_ms):
            if e > s:
                out.append(BuffWindow(s, e, prov.dmg_multiplier, prov.name))
    return out


def party_jobs_in_fight(report: dict[str, Any], fight: dict[str, Any]
                        ) -> list[str]:
    """Friendly-player job names for this fight (subTypes)."""
    actors = ((report or {}).get("masterData") or {}).get("actors") or []
    friendly = set(fight.get("friendlyPlayers") or [])
    return [a.get("subType") for a in actors
            if a.get("type") == "Player" and a.get("id") in friendly
            and a.get("subType")]


def _friendly_actor_jobs(report: dict[str, Any], fight: dict[str, Any]
                         ) -> list[tuple[int, str]]:
    """(actor_id, job) for each friendly player in this fight."""
    actors = ((report or {}).get("masterData") or {}).get("actors") or []
    friendly = set(fight.get("friendlyPlayers") or [])
    return [(a["id"], a.get("subType")) for a in actors
            if a.get("type") == "Player" and a.get("id") in friendly
            and a.get("subType")]


def provider_cast_streams(report: dict[str, Any], fight: dict[str, Any]) -> list:
    """`BundleStream`s for the on-enemy raid-buff provider casts this pull needs
    (Chain Stratagem, Dokumori, …), keyed IDENTICALLY to the per-provider
    `get_events` fetch in `fetch_observed_buff_windows` below — so priming them
    into the per-pull cache turns that fetch into a hit instead of a separate
    round trip. Job-agnostic: depends on the party comp, not the analyzed job.
    Empty when no on-enemy provider is present (the common no-SCH/NIN case)."""
    from fflogs_api import BundleStream
    abilities = ((report or {}).get("masterData") or {}).get("abilities") or []
    actor_jobs = _friendly_actor_jobs(report, fight)
    present_jobs = [j for _, j in actor_jobs]
    s, e = fight["startTime"], fight["endTime"]
    streams: list = []
    seen: set[tuple[int, int]] = set()
    for prov in present_providers(present_jobs):
        if not prov.on_enemy:
            continue
        cast_id = resolve_cast_action_id(abilities, prov.name)
        if cast_id is None:
            continue
        for aid, job in actor_jobs:
            if PROVIDER_BUFFS.get(job) is not prov:
                continue
            if (aid, cast_id) in seen:
                continue
            seen.add((aid, cast_id))
            streams.append(BundleStream(data_type="Casts", start=s, end=e,
                                        source_id=aid, ability_id=cast_id))
    return streams


def fetch_observed_buff_windows(client: Any, code: str,
                                report: dict[str, Any],
                                fight: dict[str, Any],
                                player_id: int) -> list[BuffWindow]:
    """Recover the raid-buff windows that actually occurred in this pull, for
    exactly the providers present. Two paths:

      * **party buffs on the player** (Battle Litany, Embolden, Divination…):
        read from the player's received-buff stream (FFLogs surfaces these
        under `sourceID = player`).
      * **on-enemy debuffs** (Chain Stratagem, Dokumori): FFLogs doesn't
        surface these on a boss stream cleanly, so we infer them from the
        provider's *cast* of the action — the debuff applies on hit, so the
        window is `[cast, cast + duration]`.

    Returns [] when no providers are present / no data.
    """
    abilities = ((report or {}).get("masterData") or {}).get("abilities") or []
    actor_jobs = _friendly_actor_jobs(report, fight)
    present_jobs = [j for _, j in actor_jobs]
    s, e = fight["startTime"], fight["endTime"]
    fight_dur = (e - s) / 1000.0
    out: list[BuffWindow] = []

    # On-player party buffs, from the player's received-buff stream.
    status_map = {sid: p for sid, p in
                  resolve_status_ids(abilities, present_jobs).items()
                  if not p.on_enemy}
    if status_map:
        try:
            evs = client.get_aura_events(code, s, e, player_id, "Buffs")
            out += observed_windows_from_events(evs, status_map, s, e)
        except Exception:
            pass

    # On-enemy debuffs, inferred from the provider's casts.
    for prov in present_providers(present_jobs):
        if not prov.on_enemy:
            continue
        cast_id = resolve_cast_action_id(abilities, prov.name)
        if cast_id is None:
            continue
        for aid, job in actor_jobs:
            if PROVIDER_BUFFS.get(job) is not prov:
                continue
            try:
                casts = client.get_events(code, s, e, aid,
                                          data_type="Casts", ability_id=cast_id)
            except Exception:
                continue
            for ev in casts:
                t = (ev.get("timestamp", s) - s) / 1000.0
                if t < 0:
                    continue
                out.append(BuffWindow(
                    t, min(t + prov.duration_s, fight_dur),
                    prov.dmg_multiplier, prov.name))
    return out


# --- Expected windows (on-cadence, comp-driven) ----------------------------

def expected_windows(fight_duration_s: float,
                     present_jobs: list[str]) -> list[BuffWindow]:
    """On-cadence windows assuming every present provider bursts on the standard
    2-minute cadence, with each provider's *opener* phased at its own canonical
    timing (`BuffProvider.opener_offset_s`, ~3rd GCD, job-specific) rather than
    at t=0. One window per provider per burst, at the provider's own
    duration/multiplier. The "if the whole party played perfectly" lens.

    Per-provider phasing matters: the buffs stagger across the opener (Dokumori
    ~4.6s → Embolden ~8.4s), so `multiplier_intervals` builds a realistic
    ramp-in instead of one synchronized step at zero. This stops the model from
    pretending the opener buffs cover t=0, while preserving each provider's
    2-min cadence (and thus the relative alignment of MCH's own tools)."""
    out: list[BuffWindow] = []
    for p in present_providers(present_jobs):
        t = p.opener_offset_s
        while t < fight_duration_s:
            end = min(t + p.duration_s, fight_duration_s)
            if end > t:
                out.append(BuffWindow(t, end, p.dmg_multiplier, p.name))
            t += BURST_CADENCE_S
    return out


# --- Collapse to a multiplier timeline -------------------------------------

def multiplier_intervals(windows: list[BuffWindow]
                         ) -> list[tuple[float, float, float]]:
    """Collapse overlapping BuffWindows into non-overlapping
    (start_s, end_s, multiplier) segments where `multiplier` is the product
    of every buff active in that segment. Segments at multiplier 1.0 (no buff)
    are omitted — a cast outside every segment is unbuffed by definition.
    """
    if not windows:
        return []
    bounds = sorted({w.start_s for w in windows} | {w.end_s for w in windows})
    out: list[tuple[float, float, float]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        mid = (a + b) / 2.0
        mult = 1.0
        for w in windows:
            if w.start_s <= mid < w.end_s:
                mult *= w.multiplier
        if mult != 1.0:
            out.append((a, b, mult))
    # Coalesce adjacent equal-multiplier segments for compactness.
    merged: list[tuple[float, float, float]] = []
    for seg in out:
        if merged and abs(merged[-1][2] - seg[2]) < 1e-9 \
                and abs(merged[-1][1] - seg[0]) < 1e-9:
            ls, _, lm = merged[-1]
            merged[-1] = (ls, seg[1], lm)
        else:
            merged.append(seg)
    return merged


def multiplier_at(t: float,
                  intervals: list[tuple[float, float, float]]) -> float:
    """Damage multiplier active at time `t` (1.0 if outside every segment)."""
    for s, e, m in intervals:
        if s <= t < e:
            return m
    return 1.0
