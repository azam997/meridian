"""Tier A (targetability) and Tier B (consensus) downtime sources.

Replaces the self-referential cast-gap heuristic in `_core/downtime.py`
as the primary signal. The legacy heuristic stays available as Tier C
fallback for fights / reports where neither Tier A events nor a viable
ref pool is available.

Three tiers:
  A. Boss `targetabilityupdate` events — confirmed, party-wide, ground
     truth. Used by both delivered-side (drift/clip) attribution and the
     idealized simulator.
  B. Consensus idle stretches across >= N reference runs, role-tuned.
     Used by the *lenient* idealized only — never subtracted from the
     delivered side so a player's mistake isn't pardoned by other
     players' coincidental gaps.
  C. Legacy cast-gap heuristic. Kept as last-ditch fallback when both
     A and B are unavailable.
"""
from __future__ import annotations

from typing import Any


# --- Tier A: reconstruct per-enemy targetable intervals --------------------


def _union_intervals(intervals: list[tuple[float, float]]
                     ) -> list[tuple[float, float]]:
    """Merge overlapping intervals. Sorted by start."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda x: x[0])
    out: list[tuple[float, float]] = [sorted_ivs[0]]
    for s, e in sorted_ivs[1:]:
        last_s, last_e = out[-1]
        if s <= last_e:
            out[-1] = (last_s, max(last_e, e))
        else:
            out.append((s, e))
    return out


def resolve_boss_actor_ids(report_summary: dict[str, Any],
                           fight: dict[str, Any]) -> set[int]:
    """Find the boss actor IDs that participate in this specific fight.

    `report_summary.masterData.actors` carries every actor across every
    fight in the report; `fight.enemyNPCs` is the per-fight subset.
    Intersect the two to get only this fight's bosses — multi-pull reports
    (e.g. a session covering M9S, M10S, M11S) have Boss-tagged actors
    from every encounter.
    """
    actors = ((report_summary or {}).get("masterData") or {}).get("actors") or []
    bosses_in_report = {a["id"] for a in actors
                        if a.get("subType") == "Boss"}
    enemy_npc_ids = {n["id"] for n in (fight.get("enemyNPCs") or [])}
    return bosses_in_report & enemy_npc_ids


# How long past its last observed activity (own cast, or a hit landed on it
# by the analyzed player) a spawn-opened enemy is assumed to remain targetable
# when its targetability tail never closed. Ultimate phase relays (Dancing Mad:
# Kefka P2 fires spawn `targetable=1` at 209s and is then removed with NO
# closing event) otherwise credit the boss as up for the rest of the fight,
# masking the real transition downtime. Sibling of ADD_TARGETABLE_TAIL_S; kept
# separate because the risk direction differs — over-capping here *creates*
# downtime and inflates efficiency, so it only ever applies with positive
# activity evidence (see `enemy_last_activity`).
SILENT_DESPAWN_TAIL_GRACE_S: float = 10.0


def actor_targetable_intervals(
    events_for_actor: list[dict[str, Any]],
    fight_start_ms: int,
    fight_end_ms: int,
    open_tail_grace_s: float | None = None,
    spawn_tail_last_activity_s: float | None = None,
) -> list[tuple[float, float]]:
    """Reconstruct one enemy's *targetable* intervals (relative to fight
    start) from its raw targetability events.

    Two facts about FFLogs targetability data drive the reconstruction:

      * **An enemy is targetable from the moment it appears.** A `0` (going
        untargetable) therefore implies it was targetable up to that point,
        and the first event we ever see for an enemy present at pull is a
        `0`. So an enemy with no events — or whose first event is `0` — is
        treated as targetable from fight start (t=0).
      * **A lone leading `1` is a spawn.** An add that pops mid-fight (e.g.
        M9S's Coffinmaker when the boss leaves) fires `targetable=1` at
        spawn with no preceding `0`. That enemy was *not* present/targetable
        before, so its targetable interval starts at the `1`, not at t=0.

    `open_tail_grace_s` governs how a still-targetable-at-the-end interval is
    closed. `None` (the default, used by downtime) closes it at fight end — a
    boss with no untargetable phase is simply always up. A value caps the open
    tail at `last_event + grace` instead: many mechanic adds emit a spawn
    `targetable=1` but NEVER a despawn `0` or a death event, so assuming they
    stay targetable to fight end stacks every transient add into a phantom
    whole-fight multi-target window (see `ADD_TARGETABLE_TAIL_S`). Only the
    multi-target detector passes a grace.

    `spawn_tail_last_activity_s` is the downtime path's narrower cap for the
    silent-despawn signature (ultimate boss relays): when the open tail was
    opened by an *explicit* spawn `targetable=1` event — never the implicit
    present-at-pull opening — the tail is capped at
    `max(last targetability event, last observed activity) + grace` instead of
    fight end. `None` (unknown activity / evidence fetch failed) keeps the
    fight-end tail, so downtime is byte-identical without positive evidence.

    Returns sorted, non-overlapping (start_s, end_s) targetable intervals.
    """
    fight_end_s = (fight_end_ms - fight_start_ms) / 1000.0
    evs = sorted(events_for_actor, key=lambda e: e["timestamp"])
    if not evs:
        return [(0.0, fight_end_s)]

    def _t(ev: dict[str, Any]) -> float:
        return (ev["timestamp"] - fight_start_ms) / 1000.0

    def _is_on(ev: dict[str, Any]) -> bool:
        return ev.get("targetable") in (1, True)

    # Leading `1` = spawn (not present before). Anything else = present and
    # targetable from pull.
    if _is_on(evs[0]):
        targetable = False
        start: float | None = None
    else:
        targetable = True
        start = 0.0
    # Whether the currently-open interval was opened by an explicit spawn
    # event (vs the implicit present-at-pull opening above).
    open_by_spawn = False

    intervals: list[tuple[float, float]] = []
    for ev in evs:
        if _is_on(ev):
            if not targetable:
                targetable = True
                start = _t(ev)
                open_by_spawn = True
        else:  # going untargetable
            if targetable and start is not None:
                intervals.append((start, _t(ev)))
                targetable = False
                start = None
    if targetable and start is not None:
        if open_tail_grace_s is not None:
            tail_end = min(_t(evs[-1]) + open_tail_grace_s, fight_end_s)
        elif open_by_spawn and spawn_tail_last_activity_s is not None:
            tail_end = min(
                max(_t(evs[-1]), spawn_tail_last_activity_s)
                + SILENT_DESPAWN_TAIL_GRACE_S,
                fight_end_s,
            )
        else:
            tail_end = fight_end_s
        if tail_end > start:
            intervals.append((start, tail_end))
    return intervals


def _complement_intervals(intervals: list[tuple[float, float]],
                          span_end: float) -> list[tuple[float, float]]:
    """Return [0, span_end] minus the union of `intervals`."""
    merged = _union_intervals(intervals)
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if s > cursor:
            out.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < span_end:
        out.append((cursor, span_end))
    return out


def enemy_last_activity(
    enemy_cast_events: list[dict[str, Any]],
    player_damage_events: list[dict[str, Any]],
    fight_start_ms: int,
    enemy_ids: set[int],
) -> dict[int, float]:
    """Per-enemy last-activity time (seconds, fight-relative): the max of the
    enemy's own cast events (abilities + autos, `sourceID`) and the analyzed
    player's damage landed on it (`targetID` — covers cast-less burn targets).
    The evidence that closes a silently-despawned actor's open targetability
    tail; an enemy absent from the map simply has no observed activity."""
    last: dict[int, float] = {}
    for ev in enemy_cast_events:
        sid = ev.get("sourceID")
        if sid in enemy_ids:
            t = (ev["timestamp"] - fight_start_ms) / 1000.0
            if t > last.get(sid, float("-inf")):
                last[sid] = t
    for ev in player_damage_events:
        tid = ev.get("targetID")
        if tid in enemy_ids:
            t = (ev["timestamp"] - fight_start_ms) / 1000.0
            if t > last.get(tid, float("-inf")):
                last[tid] = t
    return last


def compute_downtime_windows(
    events: list[dict[str, Any]],
    fight_start_ms: int,
    fight_end_ms: int,
    enemy_ids: set[int],
    boss_ids: set[int],
    last_activity_s: dict[int, float] | None = None,
) -> list[tuple[float, float]]:
    """True downtime = the stretches where *no enemy is targetable*.

    This is the player's real DPS-uptime loss, not "the boss is gone".
    Whenever the primary boss goes untargetable but an add (or any other
    enemy) is up, the player still has a target and it is NOT downtime —
    which is exactly the M9S Vamp Fatale case (Coffinmaker spawns the
    instant the boss leaves) that made the boss-only signal over-count by
    ~51s.

    Algorithm: reconstruct each enemy's targetable intervals (spawn-aware,
    see `actor_targetable_intervals`), union them, and take the complement
    over the fight span. Two classes of enemy are handled differently when
    they produce *no* targetability events:

      * **Bosses** are targetable for the whole fight when they have no
        events — a boss with no untargetable phase is simply always up
        (M11S The Tyrant → zero downtime).
      * **Non-boss enemies** with no events are skipped. They carry no
        information, and environmental/anchor NPCs that linger in
        `enemyNPCs` must never silently mask genuine downtime.

    A non-boss enemy that *does* flip (a real add) contributes its
    reconstructed targetable intervals like any other enemy.

    `last_activity_s` (from `enemy_last_activity`, None when the evidence
    fetch failed) enables the silent-despawn tail cap: a spawn-opened actor
    whose tail never closed is credited only to its last observed activity
    + grace instead of fight end. Ultimate boss relays remove a phase's boss
    without a closing `targetable=0`, and the phantom fight-long tail
    otherwise masks every later transition's real downtime. Actors with no
    targetability events (M11S Tyrant) are untouched — their fight-long
    interval is not spawn-opened.
    """
    fight_end_s = (fight_end_ms - fight_start_ms) / 1000.0
    by_actor: dict[int, list[dict[str, Any]]] = {}
    for ev in events:
        tid = ev.get("targetID")
        if tid is None:
            tid = ev.get("sourceID")
        if tid is None or tid not in enemy_ids:
            continue
        by_actor.setdefault(tid, []).append(ev)

    def _activity(aid: int) -> float | None:
        if last_activity_s is None:
            return None
        # Evidence fetched but this actor never acted or got hit: cap at its
        # last targetability event (the spawn) + grace — an inert prop's
        # phantom tail must not mask downtime either.
        return last_activity_s.get(aid, float("-inf"))

    targetable_union: list[tuple[float, float]] = []
    # Bosses always contribute (no events → targetable throughout).
    for bid in boss_ids:
        targetable_union.extend(actor_targetable_intervals(
            by_actor.get(bid, []), fight_start_ms, fight_end_ms,
            spawn_tail_last_activity_s=_activity(bid)))
    # Non-boss enemies contribute only when they actually flipped.
    for aid, evs in by_actor.items():
        if aid in boss_ids:
            continue
        targetable_union.extend(
            actor_targetable_intervals(evs, fight_start_ms, fight_end_ms,
                                       spawn_tail_last_activity_s=_activity(aid)))

    if not targetable_union:
        # No boss resolved and nothing flipped → no information → no
        # downtime claimed.
        return []
    return _complement_intervals(targetable_union, fight_end_s)


def resolve_enemy_actor_ids(fight: dict[str, Any]) -> set[int]:
    """Every enemy participating in this fight (boss + adds). The downtime
    model needs all of them, not just bosses, because an add being
    targetable keeps the player out of downtime."""
    return {n["id"] for n in (fight.get("enemyNPCs") or [])}


def fetch_tier_a_windows(client: Any, code: str,
                         report_summary: dict[str, Any],
                         fight: dict[str, Any],
                         actor: dict[str, Any] | None = None,
                         ) -> tuple[list[tuple[float, float]], bool]:
    """Fetch targetability events for `fight` and reduce to the true
    downtime windows — stretches where *no enemy* is targetable.

    Returns (windows, was_fetched). `was_fetched` is True whenever the
    API call succeeded — an empty list with `was_fetched=True` means
    "an enemy was always available", *not* "data missing". This
    distinction matters for the fallback logic: Tier C should only kick
    in when the data itself is unavailable.

    Also fetches the enemy-activity evidence (enemy casts + the analyzed
    `actor`'s damage landed) that closes silently-despawned bosses' open
    targetability tails. Strictly best-effort and gated on the *primary*
    evidence (enemy casts) succeeding: any failure disables the tail cap
    and reproduces the historic reconstruction exactly.
    """
    try:
        events = client.get_targetability_events(
            code, fight["startTime"], fight["endTime"],
        )
    except Exception:
        return [], False

    enemy_ids = resolve_enemy_actor_ids(fight)
    boss_ids = resolve_boss_actor_ids(report_summary, fight)

    last_activity: dict[int, float] | None = None
    try:
        enemy_casts = client.get_enemy_cast_events(
            code, fight["startTime"], fight["endTime"])
        player_damage: list[dict[str, Any]] = []
        if actor is not None:
            try:
                player_damage = client.get_events(
                    code, fight["startTime"], fight["endTime"],
                    actor["id"], data_type="DamageDone")
            except Exception:
                player_damage = []
        last_activity = enemy_last_activity(
            enemy_casts, player_damage, fight["startTime"], enemy_ids)
    except Exception:
        last_activity = None

    windows = compute_downtime_windows(
        events, fight["startTime"], fight["endTime"], enemy_ids, boss_ids,
        last_activity_s=last_activity,
    )
    return windows, True


# --- Multi-target window detection (targetability necessary condition) ------
#
# A fight only *affords* multi-target where >= 2 enemies are simultaneously
# targetable. That targetability overlap is the NECESSARY condition for a
# multi-target window; the SUFFICIENT signal (refs actually landing splash on
# >= 2 targets) is layered on in `multi_target_consensus_from_refs`. The two
# together (plus user trimming) decide where splash is credited. Phase 1 uses
# this targetability signal alone to disclaim the single-target efficiency model
# on a multi-target pull. Reuses the Tier-A targetable-interval reconstruction.

# A >= 2-targetable span shorter than this is treated as a boss<->add handoff
# (e.g. M9S Vamp Fatale's Coffinmaker spawning as the boss leaves), not a real
# multi-target phase.
MULTI_TARGET_MIN_WINDOW_S: float = 5.0
# How long past its LAST targetability event a non-boss add is assumed to remain
# targetable when it never logged a despawn `0` (or a death). Many mechanic adds
# — M9S Vamp Fatale's Coffinmaker, the Fatal Flails, the Charnel Cells — fire a
# spawn `targetable=1` and then vanish silently, so extending them to fight end
# stacked all of them into one phantom ~400s "7 simultaneous targets" window
# that mislabeled an essentially single-target fight as multi-target. Capping
# the tail keeps each genuine add phase localized to roughly when the add was
# actually up, without inventing whole-fight overlap. Small enough to stay well
# inside typical add-phase gaps (so a returning boss never overlaps a long-gone
# add), large enough to bridge a lone-spawn add's brief real lifetime.
ADD_TARGETABLE_TAIL_S: float = 10.0
# Total >= 2-targetable duration a pull needs before the single-target model is
# disclaimed. Separates a genuine multi-target fight (M10S, tens of seconds)
# from incidental overlap on a single-target fight.
MULTI_TARGET_DISCLAIM_MIN_TOTAL_S: float = 15.0


def simultaneous_targetable_windows(
    events: list[dict[str, Any]],
    fight_start_ms: int,
    fight_end_ms: int,
    enemy_ids: set[int],
    boss_ids: set[int],
    min_targets: int = 2,
    min_window_s: float = MULTI_TARGET_MIN_WINDOW_S,
) -> list[tuple[float, float, int]]:
    """Spans where >= `min_targets` enemies are simultaneously targetable.

    Mirrors `compute_downtime_windows`' enemy handling: bosses with no events
    are targetable for the whole fight; non-boss enemies count only when they
    actually flip (have events), so environmental / anchor NPCs can't synthesize
    a phantom multi-target window. Returns (start_s, end_s, peak_n) where
    `peak_n` is the maximum simultaneous targetable-enemy count inside the span.
    Spans shorter than `min_window_s` are dropped (boss<->add handoffs).
    """
    fight_end_s = (fight_end_ms - fight_start_ms) / 1000.0
    by_actor: dict[int, list[dict[str, Any]]] = {}
    for ev in events:
        tid = ev.get("targetID")
        if tid is None:
            tid = ev.get("sourceID")
        if tid is None or tid not in enemy_ids:
            continue
        by_actor.setdefault(tid, []).append(ev)

    per_enemy: list[list[tuple[float, float]]] = []
    # Bosses persist — no despawn event means "always up" (extend to fight end).
    for bid in boss_ids:
        per_enemy.append(actor_targetable_intervals(
            by_actor.get(bid, []), fight_start_ms, fight_end_ms))
    # Non-boss adds are transient: cap a never-closed spawn at last_event+grace
    # rather than fight end, so silently-vanishing mechanic adds can't synthesize
    # a phantom whole-fight overlap (see ADD_TARGETABLE_TAIL_S).
    for aid, evs in by_actor.items():
        if aid in boss_ids:
            continue
        per_enemy.append(actor_targetable_intervals(
            evs, fight_start_ms, fight_end_ms,
            open_tail_grace_s=ADD_TARGETABLE_TAIL_S))

    # Sweep-line coverage. Process starts (+1) before ends (-1) at equal t so a
    # handoff (one enemy's interval ending exactly as another's begins) keeps
    # coverage continuous rather than momentarily dropping below the threshold.
    deltas: list[tuple[float, int]] = []
    for ivs in per_enemy:
        for s, e in ivs:
            if e > s:
                deltas.append((s, 1))
                deltas.append((e, -1))
    deltas.sort(key=lambda x: (x[0], -x[1]))

    out: list[tuple[float, float, int]] = []
    cov = 0
    win_start: float | None = None
    peak = 0
    for t, d in deltas:
        prev = cov
        cov += d
        if prev < min_targets <= cov:
            win_start, peak = t, cov
        elif prev >= min_targets > cov:
            if win_start is not None:
                end = min(t, fight_end_s)
                if end - win_start >= min_window_s:
                    out.append((win_start, end, peak))
            win_start, peak = None, 0
        elif win_start is not None:
            peak = max(peak, cov)
    if win_start is not None and fight_end_s - win_start >= min_window_s:
        out.append((win_start, fight_end_s, peak))
    return out


def fetch_multi_target_windows(client: Any, code: str,
                               report_summary: dict[str, Any],
                               fight: dict[str, Any]
                               ) -> list[tuple[float, float, int]]:
    """Targetability-derived candidate multi-target windows for `fight`
    (>= 2 enemies simultaneously targetable). Best-effort: returns [] if the
    targetability fetch fails. This is the same fetch Tier-A downtime made, so
    under `CachedEventsClient` it's a cache hit (no extra round trip)."""
    try:
        events = client.get_targetability_events(
            code, fight["startTime"], fight["endTime"])
    except Exception:
        return []
    enemy_ids = resolve_enemy_actor_ids(fight)
    boss_ids = resolve_boss_actor_ids(report_summary, fight)
    return simultaneous_targetable_windows(
        events, fight["startTime"], fight["endTime"], enemy_ids, boss_ids)


def is_multi_target_pull(
    windows: list[tuple[float, float, int]],
    min_total_s: float = MULTI_TARGET_DISCLAIM_MIN_TOTAL_S,
) -> bool:
    """Whether a pull's candidate multi-target windows are substantial enough to
    disclaim the single-target efficiency model. True iff the total
    >= 2-targetable duration meets `min_total_s`."""
    return sum(e - s for s, e, _n in windows) >= min_total_s


# --- Tier B: consensus from refs ------------------------------------------


from dataclasses import dataclass

from .idle_stretches import compute_eff_gcd, compute_idle_stretches
from .job import JobData, RolePolicy


# A run of length < this gets no idle stretches contributed — too small
# a sample to draw consensus from. Role-agnostic guard.
_MIN_REF_DURATION_S = 30.0


@dataclass(frozen=True)
class RefRun:
    """Lightweight bundle of what Tier B needs from each ref.

    Kept minimal so the caller can build a list of these without dragging
    in every ModuleResult-side data structure.
    """
    label: str
    norm_casts: tuple[tuple[float, int], ...]
    fight_duration_s: float


@dataclass(frozen=True)
class ConsensusWindow:
    """One Tier-B window with confidence metadata.

    `n_idle` is the minimum number of refs concurrently idle anywhere
    inside this window (worst-case confidence — most honest number to
    display).
    """
    start_s: float
    end_s: float
    n_idle: int
    n_total: int


def _per_ref_idle_stretches(
    refs: list[RefRun],
    policy: RolePolicy,
    data: JobData,
    tier_a_windows: list[tuple[float, float]],
) -> list[list[tuple[float, float]]]:
    """Each ref's idle stretches (Tier A excluded so we don't double-count).
    Computed once so the two consensus thresholds share it."""
    out: list[list[tuple[float, float]]] = []
    for r in refs:
        eff = compute_eff_gcd(list(r.norm_casts), data)
        out.append(compute_idle_stretches(
            list(r.norm_casts), r.fight_duration_s, eff, policy,
            tier_a_windows, data.gcd_recast_mult,
        ))
    return out


def _consensus_tick_walk(
    ref_idle: list[list[tuple[float, float]]],
    refs: list[RefRun],
    fight_duration_s: float,
    policy: RolePolicy,
    threshold: float,
    tier_a_windows: list[tuple[float, float]],
) -> list[ConsensusWindow]:
    """Discretize the fight into `consensus_tick_s` ticks; a tick belongs to a
    window when `>= threshold` of the refs present are idle in it; merge
    contiguous above-threshold ticks; subtract Tier A. Attaches the worst-case
    (minimum) idle count seen inside each window. Shared by the Tier-B pass
    (`threshold = consensus_pct`) and the high-confidence pass
    (`threshold = consensus_high_pct`) — the higher threshold trims each window
    to the sub-span where the pool truly agrees, so a lone lucky-tick caster at an
    edge can't spoil the core (and a window that never reaches it yields nothing)."""
    tick = policy.consensus_tick_s
    n_ticks = int(fight_duration_s / tick) + 1

    windows: list[ConsensusWindow] = []
    cur_start: float | None = None
    cur_min_idle: int = 10**9
    cur_n_total: int = 0

    def _idle_at(t_mid: float) -> int:
        n = 0
        for stretches in ref_idle:
            for s, e in stretches:
                if s <= t_mid < e:
                    n += 1
                    break
        return n

    def _refs_present_at(t_mid: float) -> int:
        return sum(1 for r in refs if t_mid <= r.fight_duration_s)

    for i in range(n_ticks):
        t_mid = i * tick + tick / 2.0
        if t_mid >= fight_duration_s:
            break
        n_present = _refs_present_at(t_mid)
        if n_present < policy.min_ref_count:
            # If we've fallen below the pool floor, close any open window
            # and stop adding new ones.
            if cur_start is not None:
                windows.append(ConsensusWindow(
                    cur_start, i * tick, cur_min_idle, cur_n_total,
                ))
                cur_start = None
            continue
        n_idle = _idle_at(t_mid)
        above = n_idle / n_present >= threshold
        if above:
            if cur_start is None:
                cur_start = i * tick
                cur_min_idle = n_idle
                cur_n_total = n_present
            else:
                cur_min_idle = min(cur_min_idle, n_idle)
                cur_n_total = max(cur_n_total, n_present)
        else:
            if cur_start is not None:
                windows.append(ConsensusWindow(
                    cur_start, i * tick, cur_min_idle, cur_n_total,
                ))
                cur_start = None
    if cur_start is not None:
        windows.append(ConsensusWindow(
            cur_start, n_ticks * tick, cur_min_idle, cur_n_total,
        ))

    # Subtract Tier A overlap (defensive — idle stretches already excluded
    # them at the per-ref level, but the consensus aggregation can still
    # synthesize a window that touches a Tier A boundary).
    if not tier_a_windows:
        return windows
    final: list[ConsensusWindow] = []
    for w in windows:
        pieces = _subtract_window(w.start_s, w.end_s, tier_a_windows)
        for s, e in pieces:
            if e - s >= tick:    # drop sub-tick fragments
                final.append(ConsensusWindow(s, e, w.n_idle, w.n_total))
    return final


def consensus_windows_from_refs(
    ref_runs: list[RefRun],
    fight_duration_s: float,
    policy: RolePolicy,
    data: JobData,
    tier_a_windows: list[tuple[float, float]],
) -> list[ConsensusWindow]:
    """Tier-B (`consensus_pct`) consensus windows. See `_consensus_tick_walk`.
    Returns [] when the ref pool is smaller than `policy.min_ref_count`."""
    refs = [r for r in ref_runs if r.fight_duration_s >= _MIN_REF_DURATION_S]
    if len(refs) < policy.min_ref_count:
        return []
    ref_idle = _per_ref_idle_stretches(refs, policy, data, tier_a_windows)
    return _consensus_tick_walk(
        ref_idle, refs, fight_duration_s, policy, policy.consensus_pct,
        tier_a_windows)


def consensus_windows_tiered(
    ref_runs: list[RefRun],
    fight_duration_s: float,
    policy: RolePolicy,
    data: JobData,
    tier_a_windows: list[tuple[float, float]],
) -> tuple[list[ConsensusWindow], list[ConsensusWindow]]:
    """`(tier_b, high_confidence)` window lists, idle stretches computed once.

    `tier_b` (at `consensus_pct`) is the suspected-forced band — drives the
    lenient ceiling and the Timeline's hatched band. `high_confidence` (at
    `consensus_high_pct`) is the genuinely-forced core the idealized rotation
    skips and that is never scored against the player; it is a strict subset of
    `tier_b` by construction (a stricter threshold on the same per-tick votes),
    trimmed to where the pool near-unanimously agrees. `([], [])` below
    `min_ref_count`."""
    refs = [r for r in ref_runs if r.fight_duration_s >= _MIN_REF_DURATION_S]
    if len(refs) < policy.min_ref_count:
        return [], []
    ref_idle = _per_ref_idle_stretches(refs, policy, data, tier_a_windows)
    tier_b = _consensus_tick_walk(
        ref_idle, refs, fight_duration_s, policy, policy.consensus_pct,
        tier_a_windows)
    high = _consensus_tick_walk(
        ref_idle, refs, fight_duration_s, policy, policy.consensus_high_pct,
        tier_a_windows)
    return tier_b, high


# --- Ranged-filler consensus windows (Tier B's sibling; LENIENT only) -------
#
# A melee forced out of range by a mechanic usually does NOT go idle — they
# bridge the disconnect with their ranged filler (RPR Harpe, 300p) instead of
# their ~600p melee GCDs. That loss is structurally INVISIBLE to the Tier-B
# idle-consensus above (the player never stops GCDing), yet it is exactly as
# forced as downtime. These windows are detected the same way Tier B is
# (per-ref evidence -> tick-walk consensus vote) and consumed the same way:
# the LENIENT ceiling only — the sim swaps its melee GCDs for the ranged
# filler inside them. Never the strict ceiling, never the delivered side
# (the Tier-B philosophy: a consensus pardon must not drive rank).
#
# Measured basis (2026-06-10, scripts/probe_rpr_harpe.py): M10S top-10 Harpe
# casts cluster hard at t~290-300 (11 casts / 10 players), 350-390, 420-440;
# M9S at ~195 / ~235 (10-for-10). M12S-P2 clusters are <= 5/10, below the
# MELEE_DPS 0.75 consensus bar — so the gate-critical encounter barely fires.

# Filler casts earlier than this are the opener precast (pre-channeled Harpe
# lands at t~0-0.5), not a disconnect — excluded from the evidence.
_RANGED_OPENER_EXCLUDE_S: float = 5.0
# Each filler cast marks +/- this around itself as "this ref was out of melee
# range here". Wide enough to bridge the observed cross-ref jitter inside a
# real cluster (~10s spread), narrow enough that isolated one-off casts can't
# chain into a consensus window on their own.
_RANGED_MARK_HALF_S: float = 6.0
# A consensus window shorter than one GCD slot can't change the sim — drop it.
_RANGED_MIN_WINDOW_S: float = 2.5


def disengage_marks_for(
    norm_casts: list[tuple[float, int]],
    fight_duration_s: float,
    policy: RolePolicy,
    filler_id: int,
    tier_a_windows: list[tuple[float, float]],
    data: JobData | None = None,
) -> list[tuple[float, float]]:
    """One run's "this player was out of melee here" evidence: its ranged-filler
    casts widened by `_RANGED_MARK_HALF_S`, unioned (when `data` is given) with its
    idle stretches — exactly the per-ref marking `ranged_filler_windows_from_refs`
    votes across. Factored out so the STRICT melee-downtime credit can self-limit a
    pull to `consensus ∩ this pull's own marks` (credit only where the pool agrees
    it's forced AND the pull itself disengaged)."""
    half = _RANGED_MARK_HALF_S
    marks = [(max(0.0, t - half), t + half)
             for t, aid in norm_casts
             if aid == filler_id and t >= _RANGED_OPENER_EXCLUDE_S]
    if data is not None:
        eff = compute_eff_gcd(list(norm_casts), data)
        marks.extend(compute_idle_stretches(
            list(norm_casts), fight_duration_s, eff, policy, tier_a_windows,
            data.gcd_recast_mult))
    return _union_intervals(marks)


def _intersect_intervals(a: list[tuple[float, float]],
                         b: list[tuple[float, float]],
                         ) -> list[tuple[float, float]]:
    """Overlap of two interval lists, merged. Empty when either is empty."""
    out: list[tuple[float, float]] = []
    for s, e in a:
        for ms, me in b:
            lo, hi = max(s, ms), min(e, me)
            if hi > lo:
                out.append((lo, hi))
    return _union_intervals(out)


def ranged_filler_windows_from_refs(
    ref_runs: list[RefRun],
    fight_duration_s: float,
    policy: RolePolicy,
    filler_id: int,
    tier_a_windows: list[tuple[float, float]],
    data: JobData | None = None,
    exclude_windows: list[tuple[float, float]] | None = None,
) -> list[ConsensusWindow]:
    """Consensus windows where the refs were forced out of melee and bridged
    it with their ranged filler (`filler_id`). Same vote as Tier B:
    discretize into `policy.consensus_tick_s` ticks, a tick belongs to a
    window when `>= policy.consensus_pct` of the refs present marked it,
    runs of ticks merge, Tier A is subtracted.

    A ref marks a tick when they cast the filler within
    `_RANGED_MARK_HALF_S` of it — OR, when `data` is given, when the tick
    falls inside one of their idle stretches: the same forced disconnect
    manifests as Harpe on some refs and as a short idle on others (measured
    on M10S — neither behavior alone reaches consensus, the union does).
    The union vote is strictly MORE conservative than Tier B's treatment of
    the same evidence: a ranged window keeps the filler's potency on the
    ceiling where a Tier-B window pardons all of it. `exclude_windows`
    (the Tier-B windows themselves) are subtracted like Tier A, so a
    stretch already pardoned as downtime is never double-counted.

    `ConsensusWindow.n_idle` carries "refs disconnected here" (worst case
    inside the window). [] below `policy.min_ref_count`.
    """
    refs = [r for r in ref_runs if r.fight_duration_s >= _MIN_REF_DURATION_S]
    if len(refs) < policy.min_ref_count:
        return []

    ref_marks: list[list[tuple[float, float]]] = [
        disengage_marks_for(list(r.norm_casts), r.fight_duration_s, policy,
                            filler_id, tier_a_windows, data)
        for r in refs
    ]

    tick = policy.consensus_tick_s
    n_ticks = int(fight_duration_s / tick) + 1
    threshold = policy.consensus_pct

    windows: list[ConsensusWindow] = []
    cur_start: float | None = None
    cur_min_n: int = 10**9
    cur_n_total: int = 0

    def _casting_at(t_mid: float) -> int:
        return sum(1 for marks in ref_marks
                   if any(s <= t_mid < e for s, e in marks))

    for i in range(n_ticks):
        t_mid = i * tick + tick / 2.0
        if t_mid >= fight_duration_s:
            break
        n_present = sum(1 for r in refs if t_mid <= r.fight_duration_s)
        if n_present < policy.min_ref_count:
            if cur_start is not None:
                windows.append(ConsensusWindow(
                    cur_start, i * tick, cur_min_n, cur_n_total))
                cur_start = None
            continue
        n_casting = _casting_at(t_mid)
        if n_casting / n_present >= threshold:
            if cur_start is None:
                cur_start = i * tick
                cur_min_n = n_casting
                cur_n_total = n_present
            else:
                cur_min_n = min(cur_min_n, n_casting)
                cur_n_total = max(cur_n_total, n_present)
        else:
            if cur_start is not None:
                windows.append(ConsensusWindow(
                    cur_start, i * tick, cur_min_n, cur_n_total))
                cur_start = None
    if cur_start is not None:
        windows.append(ConsensusWindow(
            cur_start, min(n_ticks * tick, fight_duration_s),
            cur_min_n, cur_n_total))

    cut = list(tier_a_windows) + list(exclude_windows or [])
    final: list[ConsensusWindow] = []
    for w in windows:
        pieces = (_subtract_window(w.start_s, w.end_s, cut)
                  if cut else [(w.start_s, w.end_s)])
        for s, e in pieces:
            if e - s >= _RANGED_MIN_WINDOW_S:
                final.append(ConsensusWindow(s, e, w.n_idle, w.n_total))
    return final


@dataclass(frozen=True)
class RangedFillerContext:
    """`sim_context` payload wrapper carrying consensus ranged-filler windows
    into a job's `_model_for` for the LENIENT ceiling. Nested INSIDE a
    `CeilingContext` (the GCD axis stays outermost): the job unwraps the
    ceiling first, then this, then its own `inner` payload (entry state, ...).
    Frozen + tuple-of-tuples so it stays hashable for the perfect-sim LRU."""
    inner: Any = None
    windows: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class MultiTargetContext:
    """`sim_context` payload wrapper carrying the piecewise target-count schedule
    `N(t)` — the AoE-aware multi-target CEILING. Threaded into a job's
    `_model_for` (so `gcd_candidates` can expose AoE forks where `N >= 2`) and
    into the scorer (so each cast is valued per-target via
    `jobs._core.sim.aoe_potency.potency_for`).

    Canonical nesting order, outermost to innermost: `CeilingContext` (GCD axis)
    -> `MultiTargetContext` (this) -> `RangedFillerContext` -> the job's own
    payload (entry-gauge state / proc budget / None). Frozen + tuple-of-tuples so
    it stays hashable for the perfect-sim LRU. `schedule` is sorted,
    non-overlapping `(start_s, end_s, n_targets)`; absent intervals are single
    target (`n = 1`). Empty schedule -> byte-identical single-target sim.

    `ability_caps` is the observed-reach cap: `((ability_id, max_n), ...)`,
    sorted — the ceiling SCORER caps each ability's target count at the max
    anyone (you or a ref) was observed hitting with it inside the confirmed
    windows, so a front-cone/short-radius button (MCH Scattergun) can't be
    credited as hitting spread targets nobody ever reached. Ceiling-only:
    delivered keeps true measured N, and the sim's in-model pickers keep the
    uncapped schedule (an over-optimistic pick only lowers the capped final
    score — under-credit-safe). Empty -> uncapped, byte-identical."""
    inner: Any = None
    schedule: tuple[tuple[float, float, int], ...] = ()
    ability_caps: tuple[tuple[int, int], ...] = ()


def unwrap_multitarget(payload):
    """`(schedule, inner)` from a payload that may be a `MultiTargetContext` or
    the bare inner payload (no schedule -> () -> byte-identical). Mirrors how
    `_model_for` peels `RangedFillerContext`."""
    if isinstance(payload, MultiTargetContext):
        return payload.schedule, payload.inner
    return (), payload


def schedule_from_context(sim_context) -> tuple[tuple[float, float, int], ...]:
    """The target-count schedule from a full `sim_context` (peels the outer
    `CeilingContext` GCD axis first, then the `MultiTargetContext`). `()` when
    there is none — the single-target path. Used by the scorer
    (`jobs._core.sim.scoring`) and each job's perfect-sim entrypoints."""
    from jobs._core.gcd_speed import unwrap_ceiling_context
    _gcd, payload = unwrap_ceiling_context(sim_context)
    schedule, _inner = unwrap_multitarget(payload)
    return schedule


def caps_from_context(sim_context) -> tuple[tuple[int, int], ...]:
    """The observed-reach ability caps from a full `sim_context` (same peel as
    `schedule_from_context`). `()` when absent — uncapped, byte-identical."""
    from jobs._core.gcd_speed import unwrap_ceiling_context
    _gcd, payload = unwrap_ceiling_context(sim_context)
    if isinstance(payload, MultiTargetContext):
        return payload.ability_caps
    return ()


# --- Multi-target consensus (confirm candidate windows via refs) -----------


@dataclass(frozen=True)
class MultiTargetWindow:
    """One confirmed multi-target window. `target_count` (N) is the modal
    number of targets the references' splash casts hit inside it, capped at the
    window's peak simultaneous-targetable count."""
    start_s: float
    end_s: float
    target_count: int


def multi_target_consensus_from_refs(
    candidate_windows: list[tuple[float, float, int]],
    ref_mt_casts: list[tuple[tuple[float, int, int], ...]],
    policy: RolePolicy,
) -> list[MultiTargetWindow]:
    """Confirm targetability-derived candidate windows by reference consensus.

    `candidate_windows` are the user's (start_s, end_s, peak_n) spans where
    >= 2 enemies were targetable. `ref_mt_casts` is each ref's
    `observed_multi_target_casts` ((t_s, ability_id, n_targets), …). A candidate
    window is confirmed when >= `policy.consensus_pct` of refs landed at least
    one multi-target cast inside it — i.e. the refs agree the window genuinely
    afforded multi-target, not just the user cleaving a low-HP add others
    ignored. The confirmed N is the modal target count across the refs' splash
    casts in the window, capped at the window's peak targetable count.

    Returns [] when the ref pool is smaller than `policy.min_ref_count` — we
    can't confirm, so the caller leaves the pull disclaimed rather than guess.
    """
    refs = [c for c in ref_mt_casts]
    if len(refs) < policy.min_ref_count:
        return []

    out: list[MultiTargetWindow] = []
    for w_start, w_end, peak_n in candidate_windows:
        counts: list[int] = []          # per-cast target counts inside W (all refs)
        n_cleaving = 0
        for casts in refs:
            in_window = [n for (t, _aid, n) in casts if w_start <= t < w_end]
            if in_window:
                n_cleaving += 1
                counts.extend(in_window)
        if not refs or n_cleaving / len(refs) < policy.consensus_pct:
            continue
        # Modal target count across the refs' splash casts, capped at the peak
        # simultaneously-targetable enemy count for the window.
        modal = _mode(counts) if counts else 2
        target_count = min(max(2, modal), max(2, peak_n))
        out.append(MultiTargetWindow(w_start, w_end, target_count))
    return out


def _mode(values: list[int]) -> int:
    """Most common value (ties broken by the larger value)."""
    from collections import Counter
    c = Counter(values)
    top = max(c.values())
    return max(v for v, n in c.items() if n == top)


def _subtract_window(s: float, e: float,
                      windows: list[tuple[float, float]]
                      ) -> list[tuple[float, float]]:
    parts = [(s, e)]
    for ws, we in windows:
        new_parts: list[tuple[float, float]] = []
        for ps, pe in parts:
            if we <= ps or ws >= pe:
                new_parts.append((ps, pe))
                continue
            if ws > ps:
                new_parts.append((ps, ws))
            if we < pe:
                new_parts.append((we, pe))
        parts = new_parts
    return parts
