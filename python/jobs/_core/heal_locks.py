"""Mitigation-plan "locked" heal GCDs — the healer-simulator integration.

The healer-appropriate ceiling can't be the pure damage optimum: a healer's
plan (python/mitplan/) schedules GCD heals/shields the rotation MUST pay for.
This module turns a `mitplan.planner.Plan` into `LockedGcdWindow`s — count-in-
window obligations the shared sim engine honors (engine.py's lock scheduler) —
so the idealized ceiling is an **honest maximum** that already spends those
GCDs. Top-parsing healers skip them for score; the locked ceiling doesn't.

Tolerance by construction: a lock is "cast N of ability X inside [start, end)"
(deadline = the mechanic's hit time, start = a lead before the plan's own
placement), NOT an exact timestamp — so a player who orders their healing a
little differently than the plan is never penalized; only spending MORE GCDs
than the plan requires leaves a gap (priced by the job's improvement
contributor).

Duck-typed over the mitplan output (no mitplan import at module level): this
module is imported by job scoring aspects and the engine's consumers, and the
sidecar is the only caller of the plan-walking helpers.

Resurrection pardon (`_rez_pardon` / the `rez_ids` kwarg): a rez the analyzed
healer cast during uptime is death recovery, not a throughput choice — the
ceiling pays for it like any other locked GCD (a credit lock pinned at the rez's
own cast time), and the costed heals that follow it (topping the rezzed player
back up, `_REZ_RECOVERY_WINDOW_S`) rank most-necessary and lift the kill-pull
cap so they never card as over-healing. Hardcast pricing: `norm_casts` anchors a
hardcast to its begincast, so the rez's ~8s cast bar shows up as the inter-cast
gap to the NEXT cast; when that gap genuinely contains the bar (>=
`_REZ_HARDCAST_MIN_GAP_S`, not explained by a downtime window opening first) the
rez is priced as `ceil(bar / 2.5)` locked slots capped at `_REZ_MAX_SLOTS`;
otherwise one locked GCD is the honest floor (a Swiftcast rez costs exactly
one). Swiftcast state itself is out of scope. Follow-up: RDM Verraise / SMN
Resurrection have no lock plumbing (DPS jobs carry no heal budget) and are NOT
pardoned here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# How far before the plan's own cast placement a lock window opens. The plan
# places heals just ahead of the mechanic (LEAD_SHIELD_S + GCD pacing); the sim
# may cast anywhere from this lead up to the mechanic itself — count-in-window
# is the tolerance mechanism, and a wider window lets the optimizer place the
# heal favorably (e.g. a Rapture right before a buffed Misery).
LOCK_LEAD_S: float = 10.0

# Conservative GCD slot length used only for count-fitting a window at
# derivation time (the engine re-derives real slot lengths live, haste-aware).
_SLOT_S: float = 2.5


@dataclass(frozen=True)
class LockedGcdWindow:
    """One count-in-window heal-GCD obligation. Hashable + picklable — these
    ride the `sim_context` into the scoring LRU cache keys and the sim-pool
    workers."""
    ability_id: int      # the planned heal GCD (e.g. WHM Medica III / Rapture)
    start_s: float       # earliest honest placement
    end_s: float         # deadline — the mechanic's hit time (half-open)
    count: int           # casts required inside [start_s, end_s)
    cast_s: float        # nominal cast time (informational; the engine takes
                         # slot length from model.gcd_duration)


@dataclass(frozen=True)
class HealLockContext:
    """`sim_context` nesting layer carrying the locks (canonical unwrap order:
    CeilingContext -> MultiTargetContext -> HealLockContext -> the job's own
    payload). Falsy when empty so cold paths stay byte-identical."""
    locks: tuple[LockedGcdWindow, ...] = ()
    inner: Any = None

    def __bool__(self) -> bool:
        return bool(self.locks)


def _window_for(mech_time_s: float, cast_at_s: float, count: int,
                cast_s: float, ability_id: int, fight_duration_s: float,
                ) -> LockedGcdWindow | None:
    """One lock window: deadline = the mechanic's hit, start = the plan's own
    (earliest) placement minus the lead, widened so `count` casts always fit.
    Clipped to the player's fight span — a mechanic past their kill never
    happened for them, so its heals aren't owed."""
    end = float(mech_time_s)
    if end > fight_duration_s or end <= 0:
        return None
    start = max(0.0, min(float(cast_at_s), end - count * _SLOT_S) - LOCK_LEAD_S)
    if start >= end:
        return None
    fit = int((end - start) / _SLOT_S)
    n = min(int(count), fit)
    if n <= 0:
        return None
    return LockedGcdWindow(ability_id=int(ability_id), start_s=round(start, 2),
                           end_s=round(end, 2), count=n,
                           cast_s=float(cast_s))


def locks_from_plan(plan: Any, slot: str, fight_duration_s: float
                    ) -> tuple[LockedGcdWindow, ...]:
    """The locked windows a mit plan imposes on one party slot ("H1"/"H2").

    Sources, per mechanic: the HP-sweep's inserted AoE GCD heals (`gcd_heals` —
    the WHM Medica III / Rapture path) and any real GCD assignment (`is_gcd`,
    not a suggestion, not a carryover — the future GCD-shield shape).
    Deterministic: the plan itself is deterministic and the output is sorted.
    """
    out: list[LockedGcdWindow] = []
    for pm in plan.mechanics:
        mech_t = float(pm.mech.time_s)
        for gh in pm.gcd_heals:
            if gh.slot != slot:
                continue
            w = _window_for(mech_t, gh.cast_at_s, gh.count, gh.cast_time_s,
                            gh.action_id, fight_duration_s)
            if w is not None:
                out.append(w)
        for a in pm.assignments:
            if (a.slot != slot or not a.is_gcd
                    or a.is_suggestion or a.is_carryover):
                continue
            w = _window_for(mech_t, a.cast_at_s, 1, max(a.cast_time_s, _SLOT_S),
                            a.action_id, fight_duration_s)
            if w is not None:
                out.append(w)
    # Merge same-(ability, deadline) windows (two sources can target one
    # mechanic); keep the earliest start and sum the counts.
    merged: dict[tuple[int, float], LockedGcdWindow] = {}
    for w in out:
        key = (w.ability_id, w.end_s)
        prev = merged.get(key)
        if prev is None:
            merged[key] = w
        else:
            merged[key] = LockedGcdWindow(
                ability_id=w.ability_id,
                start_s=min(prev.start_s, w.start_s),
                end_s=w.end_s,
                count=prev.count + w.count,
                cast_s=max(prev.cast_s, w.cast_s))
    return tuple(sorted(merged.values(),
                        key=lambda w: (w.end_s, w.start_s, w.ability_id)))


def plan_gcd_cost(plan: Any, slot: str) -> tuple[int, float, int]:
    """(total GCD-heal count, lost DPS potency, count of COSTED casts) the plan
    schedules on `slot` — the headline/improvements meta. A costed cast is one
    with a positive `gcd_cost_potency` (WHM: Medica III yes, the free lily
    Rapture no). Sidecar-only helper — imports mitplan lazily."""
    from mitplan.library import FILLER_GCD_POTENCY
    from mitplan.planner import _ACTION_BY_JOB_ID

    def _cost(job: str, action_id: int) -> float:
        row = _ACTION_BY_JOB_ID.get((job, action_id))
        if row is not None:
            return float(row.gcd_cost_potency)
        return float(FILLER_GCD_POTENCY.get(job, 300.0))

    count = 0
    costed = 0
    potency = 0.0
    for pm in plan.mechanics:
        for gh in pm.gcd_heals:
            if gh.slot != slot:
                continue
            c = _cost(gh.job, gh.action_id)
            count += gh.count
            potency += gh.count * c
            if c > 0:
                costed += gh.count
        for a in pm.assignments:
            if (a.slot != slot or not a.is_gcd
                    or a.is_suggestion or a.is_carryover):
                continue
            c = _cost(a.job, a.action_id)
            count += 1
            potency += c
            if c > 0:
                costed += 1
    return count, round(potency, 1), costed


# --- Honest-budget reconciliation (the player's ACTUAL healing) ---------------
# The plan's heal count is a TOP-PARSE floor: it models the cleanest party's
# damage taken, so it under-counts the healing a normal — or, worst of all, a
# progging — party actually has to do. Scoring the ceiling against that floor
# punishes real, necessary healing as "missed damage" (harshest on a wipe, where
# the party takes the most avoidable damage). `reconcile_heal_budget` lifts the
# ceiling's healing tax to the healing the analyzed player actually delivered,
# capped so genuine over-healing is still surfaced — the honest maximum for THIS
# pull rather than for a hypothetical clean kill.
#
# "You only need HP by the time the next attack hits" (user directive): a heal
# GCD the player cast is presumed necessary and credited AT ITS OWN CAST TIME, so
# neither out-of-order healing nor imperfect sub-slot timing is ever penalized —
# the ceiling is forced to spend that same GCD healing right where the player did
# (free in downtime, costed in uptime, symmetric on both sides of the ratio).

# Credit window around each actual heal cast (cast-START constraint, half-open).
# Wider than a GCD so the engine's downtime satisfier can place a downtime heal
# for free — kept symmetric with the player, who also lost no damage healing in
# downtime. Mostly ahead of the cast (heals lead the HP they top).
_CREDIT_LEAD_S: float = 2.0
_CREDIT_TRAIL_S: float = 1.0

# Kill-pull tolerance before a heal counts as beyond-plan over-healing (mirrors
# the historic improvement-card threshold): the first couple of extras — or 20%
# of a heal-heavy plan — are free. A wipe waives the cap entirely (below).
_KILL_SLACK_MIN: int = 2
_KILL_SLACK_FRAC: float = 0.2

# --- Resurrection pardon (see module docstring) -------------------------------
# Costed heals within this window after a rez cast completes are rez recovery:
# they rank most-necessary and lift the kill-pull cap by their count.
_REZ_RECOVERY_WINDOW_S: float = 15.0
# Base healer rez cast bar (Raise / Ascend / Resurrection / Egeiro: 8s hardcast;
# spell speed shaves a few tenths, slidecast a few more — hence the MIN_GAP).
_REZ_HARDCAST_BAR_S: float = 8.0
# The following inter-cast gap must genuinely contain the bar before a rez is
# priced as a hardcast (below this, one locked GCD is the honest floor).
_REZ_HARDCAST_MIN_GAP_S: float = 7.0
# ceil(8s / 2.5s) = 4, capped: the sim pays at most 3 locked slots per rez.
_REZ_MAX_SLOTS: int = 3


@dataclass(frozen=True)
class HealBudget:
    """The reconciled healing tax for one pull. `locks` go into the ceiling;
    `state` is the Scoring aspect's `extra_state` heal-lock block (the headline
    and the shared improvement card both read it). `applied` is False for a
    non-healer / plan-less non-wipe run, leaving the ceiling byte-identical."""
    applied: bool
    locks: tuple[LockedGcdWindow, ...]
    state: dict


def _empty_budget() -> HealBudget:
    return HealBudget(applied=False, locks=(), state={})


def _credit_window(t: float, ability_id: int, fight_duration_s: float
                   ) -> LockedGcdWindow | None:
    """A single actual heal cast, turned into a count-1 lock pinned near its own
    time so the ceiling pays for it exactly where it happened."""
    start = max(0.0, t - _CREDIT_LEAD_S)
    end = min(float(fight_duration_s), t + _CREDIT_TRAIL_S)
    if end - start < 0.1:
        return None
    return LockedGcdWindow(ability_id=int(ability_id), start_s=round(start, 2),
                           end_s=round(end, 2), count=1, cast_s=_SLOT_S)


@dataclass(frozen=True)
class RezPardon:
    """One pull's pardoned resurrection casts: the locks the ceiling must pay,
    the post-rez recovery intervals (costed heals inside them are rez recovery),
    and the per-cast info `(t, ability_id, locked_slots)` for the state block.
    Falsy when the pull had no pardonable rez, so cold paths stay
    byte-identical."""
    locks: tuple[LockedGcdWindow, ...] = ()
    recovery: tuple[tuple[float, float], ...] = ()
    casts: tuple[tuple[float, int, int], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.casts)


def _rez_pardon(norm_casts: Any, rez_ids: frozenset,
                dt_windows: Any, fight_duration_s: float) -> RezPardon | None:
    """Build the rez pardon from the normalized cast stream (module docstring:
    "Resurrection pardon"). Uptime-only — a downtime rez displaces no damage on
    either side of the ratio, exactly like a downtime heal. Hardcast detection:
    the gap to the next cast, clipped at the first downtime window opening
    inside it (idle explained by a disconnect is not a cast bar)."""
    if not rez_ids:
        return None
    casts = sorted((float(t), int(aid)) for t, aid in norm_casts
                   if t is not None)
    dt = tuple(dt_windows or ())

    def _uptime(t: float) -> bool:
        return not any(s <= t < e for s, e in dt)

    locks: list[LockedGcdWindow] = []
    recovery: list[tuple[float, float]] = []
    info: list[tuple[float, int, int]] = []
    for i, (t, aid) in enumerate(casts):
        if aid not in rez_ids or not (0.0 <= t < fight_duration_s) \
                or not _uptime(t):
            continue
        next_t = next((t2 for t2, _a in casts[i + 1:] if t2 > t),
                      fight_duration_s)
        gap = next_t - t
        for s, _e in dt:
            if t < s < next_t:
                gap = min(gap, s - t)
        if gap >= _REZ_HARDCAST_MIN_GAP_S:
            bar = min(gap, _REZ_HARDCAST_BAR_S)
            slots = min(_REZ_MAX_SLOTS, math.ceil(bar / _SLOT_S))
        else:
            bar = 0.0
            slots = 1
        if slots <= 1:
            w = _credit_window(t, aid, fight_duration_s)
        else:
            start = max(0.0, t - _CREDIT_LEAD_S)
            end = min(float(fight_duration_s), t + bar + _CREDIT_TRAIL_S)
            n = min(slots, int((end - start) / _SLOT_S))
            w = (LockedGcdWindow(ability_id=int(aid), start_s=round(start, 2),
                                 end_s=round(end, 2), count=n, cast_s=_SLOT_S)
                 if n > 1 else _credit_window(t, aid, fight_duration_s))
        if w is None:
            continue
        locks.append(w)
        recovery.append((t, t + bar + _REZ_RECOVERY_WINDOW_S))
        info.append((round(t, 2), int(aid), int(w.count)))
    if not info:
        return None
    return RezPardon(
        locks=tuple(sorted(locks,
                           key=lambda w: (w.end_s, w.start_s, w.ability_id))),
        recovery=tuple(recovery), casts=tuple(info))


def _necessary_rank(casts: list[tuple[float, int]],
                    windows: tuple[LockedGcdWindow, ...],
                    recovery: tuple[tuple[float, float], ...] = (),
                    ) -> list[tuple[float, int]]:
    """The player's costed heal casts, MOST-necessary first: a cast inside a
    post-rez recovery interval is surely forced (the party is being topped back
    up); then a cast inside (or nearest to) a plan mechanic window is surely
    mechanic-driven; an isolated cast far from every window is the likeliest
    discretionary over-heal, so it sorts last and is the first to fall outside a
    capped budget. Stable — with no rez and no plan windows every key ties and
    the order stays chronological (the excess then falls on the latest casts,
    the historic card heuristic)."""
    def dist(t: float) -> float:
        best = math.inf
        for w in windows:
            if w.start_s <= t < w.end_s:
                return 0.0
            best = min(best, abs(t - w.start_s), abs(t - w.end_s))
        return best if best != math.inf else 0.0

    def key(ta: tuple[float, int]) -> tuple[int, float]:
        in_rec = any(s <= ta[0] < e for s, e in recovery)
        return (0 if in_rec else 1, dist(ta[0]))
    return sorted(casts, key=key)


def reconcile_heal_budget(
    *,
    plan_locks: tuple[LockedGcdWindow, ...],
    plan_meta: dict,
    actual_costed_casts: list[tuple[float, int]],
    costed_ids: frozenset,
    locked_heal_id: int,
    filler_potency: float,
    fight_duration_s: float,
    is_prog: bool,
    rez: RezPardon | None = None,
) -> HealBudget:
    """Reconcile the plan's (top-parse) heal windows against the healing the
    player actually delivered. Pure — the sidecar/report plumbing lives in
    `reconcile_from_report`. `rez` (default None — byte-identical) adds the
    resurrection pardon: its locks join the output in every branch, its recovery
    intervals rank/cap-lift the costed heals that follow a rez."""
    # Re-clip to the SCORED span: `_heal_lock_payload` builds the plan against the
    # full wipe, but a prog pull is scored only to the terminal death.
    plan_locks = tuple(w for w in plan_locks
                       if w.end_s <= fight_duration_s + 1e-6)
    free_windows = tuple(w for w in plan_locks if w.ability_id not in costed_ids)
    costed_windows = tuple(w for w in plan_locks if w.ability_id in costed_ids)
    plan_costed = sum(w.count for w in costed_windows)
    plan_free = sum(w.count for w in free_windows)

    actual = sorted((float(t), int(aid)) for t, aid in actual_costed_casts
                    if 0.0 <= float(t) < fight_duration_s)
    n_actual = len(actual)

    rez_locks = rez.locks if rez else ()
    rez_recovery = rez.recovery if rez else ()
    n_recovery = sum(1 for t, _a in actual
                     if any(s <= t < e for s, e in rez_recovery))

    comp_state = {
        "mit_plan_comp": list(plan_meta.get("comp") or []),
        "mit_plan_comp_source": str(plan_meta.get("source") or "defaults"),
        "mit_plan_warnings": list(plan_meta.get("warnings") or []),
    }
    excess: list[tuple[float, int]] = []

    if n_actual <= plan_costed:
        # The player healed no more than the plan's mechanical minimum: keep the
        # plan's own windows (the floor). A carried healer who under-heals can
        # still exceed this ceiling — the ">100% above the honest ceiling" case.
        credited = plan_costed
        locks = plan_locks
        if rez_locks:
            locks = tuple(sorted(
                plan_locks + rez_locks,
                key=lambda w: (w.end_s, w.start_s, w.ability_id)))
    else:
        # Credit the healing the player actually did, at the times they did it.
        if is_prog:
            cap = n_actual          # a wipe is non-competitive — full benefit of the doubt
        else:
            slack = max(_KILL_SLACK_MIN,
                        math.ceil(_KILL_SLACK_FRAC * (plan_costed + plan_free)))
            # Post-rez recovery heals are forced by the death, not the plan —
            # each lifts the cap so it can never land in the excess.
            cap = plan_costed + slack + n_recovery
        credited = min(n_actual, max(cap, plan_costed))
        ranked = _necessary_rank(actual, plan_locks, rez_recovery)
        credited_casts = ranked[:credited]
        excess = sorted(ranked[credited:])
        credit_locks = tuple(
            w for w in (_credit_window(t, locked_heal_id, fight_duration_s)
                        for t, _aid in credited_casts)
            if w is not None)
        locks = tuple(sorted(
            free_windows + credit_locks + rez_locks,
            key=lambda w: (w.end_s, w.start_s, w.ability_id)))

    # Nothing to lock and nothing to card (a fully-clipped plan on a heal-less
    # pull): stay byte-identical to the historic unlocked path.
    if not locks and credited == 0 and not excess:
        return _empty_budget()

    state = {
        "heal_locks_applied": True,
        "heal_lock_count": int(credited + plan_free),
        "heal_lock_costed_count": int(credited),
        "heal_lock_potency": round(credited * float(filler_potency), 1),
        "heal_lock_plan_costed": int(plan_costed),
        "heal_lock_excess": [[round(t, 2), int(aid)] for t, aid in excess],
        "heal_lock_filler_potency": float(filler_potency),
        "heal_lock_is_prog": bool(is_prog),
        **comp_state,
    }
    if rez:
        # Additive rez block — absent on every rez-less pull (byte-identity).
        state["heal_lock_rez_count"] = len(rez.casts)
        state["heal_lock_rez_casts"] = [[t, aid, n] for t, aid, n in rez.casts]
        state["heal_lock_rez_gcds"] = int(sum(n for _t, _a, n in rez.casts))
        state["heal_lock_rez_recovery_count"] = int(n_recovery)
    return HealBudget(applied=True, locks=locks, state=state)


def reconcile_from_report(
    report: Any, norm_casts: Any, fight_duration_s: float, *,
    costed_ids: frozenset, locked_heal_id: int, filler_potency: float,
    rez_ids: frozenset = frozenset(),
) -> HealBudget:
    """`prepare`-time convenience: read the sidecar-staged plan (`__heal_locks__`)
    and prog flag (`__prog__`), pull the player's costed heal casts from
    `norm_casts`, and reconcile. Empty (byte-identical historic path) when there
    is neither a staged plan nor a wipe with healing to credit. `rez_ids`
    (default empty — today's behavior exactly) enables the resurrection pardon
    for the job's rez GCDs (module docstring)."""
    hl = report.get("__heal_locks__") or {}
    is_prog = bool(report.get("__prog__"))
    if not hl and not is_prog:
        return _empty_budget()
    # Costed heal GCDs cast during UPTIME only. A downtime heal (boss untargetable)
    # displaces no damage on EITHER side of the ratio, so it must neither tax the
    # ceiling nor count as over-healing — never card a heal that cost nothing.
    dt = (report.get("__downtime__") or {}).get("windows") or ()

    def _uptime(t: float) -> bool:
        return not any(s <= t < e for s, e in dt)

    actual = [(float(t), int(aid)) for t, aid in norm_casts
              if aid in costed_ids and t is not None and float(t) >= 0.0
              and _uptime(float(t))]
    # The rez pardon shares the SAME uptime rule: only an uptime rez displaced
    # damage, so only an uptime rez is locked into the ceiling.
    rez = _rez_pardon(norm_casts, rez_ids, dt, fight_duration_s)
    # A plan-less, healing-less, rez-less run has nothing to lock either way.
    if not hl and not actual and not rez:
        return _empty_budget()
    return reconcile_heal_budget(
        plan_locks=tuple(hl.get("locks") or ()), plan_meta=hl,
        actual_costed_casts=actual, costed_ids=costed_ids,
        locked_heal_id=locked_heal_id, filler_potency=filler_potency,
        fight_duration_s=fight_duration_s, is_prog=is_prog, rez=rez)


def improvements_from_heal_gcds(you) -> list:
    """Shared healer Potential-Improvements card: the costed heal GCDs the player
    cast BEYOND the honest budget the ceiling already pays for. With the budget
    reconciled to the player's ACTUAL healing (`reconcile_heal_budget`), this
    fires only on genuine over-healing — a wipe never cards (its budget credits
    every heal), and a plan-conformant OR merely out-of-order kill never cards
    either. The excess casts are the exact ones with no mechanic to cover, so the
    card names and locates them precisely."""
    from jobs._core.ability_metadata import get_metadata
    from jobs._core.improvements import Improvement, _mmss

    sc = getattr(you, "aspects", {}).get("Scoring")
    state = sc.state if sc is not None else {}
    if not state.get("heal_locks_applied"):
        return []
    excess = state.get("heal_lock_excess") or []
    per = float(state.get("heal_lock_filler_potency") or 0.0)
    if not excess or per <= 0:
        return []
    credited = int(state.get("heal_lock_costed_count") or 0)

    children = []
    for t, aid in excess:
        meta = get_metadata(int(aid))
        name = meta.name if meta is not None else f"#{int(aid)}"
        children.append(Improvement(
            kind="extra_heal_gcds", ability_id=int(aid), ability_name=name,
            time_s=float(t), lost_potency=per,
            summary=f"{name} at {_mmss(float(t))}, a damage GCD the plan didn't need",
        ))
    n = len(children)
    first = children[0]
    return [Improvement(
        kind="extra_heal_gcds", ability_id=first.ability_id,
        ability_name=first.ability_name, time_s=first.time_s,
        lost_potency=n * per,
        summary=(f"Healing GCDs beyond the honest budget ×{n}. The ceiling "
                 f"already credits the {credited} heal GCD"
                 f"{'s' if credited != 1 else ''} this pull needed; these were "
                 f"cast with no mechanic to cover."),
        children=children,
    )]
