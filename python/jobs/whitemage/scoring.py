"""WHM-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is WHM-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

WHM's one bespoke damage piece is the **Dia DoT**: a 30 s DoT scored per cast
by *time-to-next-application* (capped at 30 s) so an early refresh credits
less — overcap-safe and symmetric (the same function scores the player's
timeline and the ceiling's). The DoT snapshots buffs at cast time. Everything
else is flat table potency × the raid-buff / in-sim-tincture multiplier at the
cast instant. No coverage overlay (WHM has no maintained personal damage buff
— Presence of Mind's value is *more GCDs*, modeled in the simulator's haste
window, not a multiplier), no guaranteed-crit family.

The per-pull `sim_context` is the phase-continuation entry lily state
(`WhmContext`): a fight logged as a later phase (M12S-P2) starts mid-combat
with carried lilies / a bloomed Blood Lily, so an elite continuation parse
opens with a Misery the cold-start sim couldn't afford. `measure_entry_lily_state`
infers it from the player's own early casts (deepest-deficit), and the ceiling
is seeded with the same state — symmetric, preserving the <=100% guard.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.whitemage import data as wd
from jobs.whitemage import simulator as whm_simulator
from jobs.whitemage.simulator import WHM_GCD_S, WhmContext


def _dia_dot_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None,
) -> float:
    """Dia DoT potency, summed per application. Each cast is credited for
    `min(30s, time-to-next-Dia)` of ticks — so over-refreshing (clipping the
    DoT) credits less, never double-counts. The DoT snapshots buffs at cast
    time, so the multiplier is read at the application instant (not per tick)."""
    casts = sorted(t for t, aid in timeline if aid == wd.DIA)
    if not casts:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + wd.DIA_DOT_DURATION_S
    total = 0.0
    for i, ct in enumerate(casts):
        nxt = casts[i + 1] if i + 1 < len(casts) else span_end
        covered_s = min(wd.DIA_DOT_DURATION_S, max(0.0, nxt - ct))
        n_ticks = covered_s / wd.DIA_DOT_TICK_S
        m = multiplier_at(ct, buff_intervals) if buff_intervals else 1.0
        total += n_ticks * wd.DIA_DOT_TICK_P * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency × the
    raid-buff multiplier at its time, plus the Dia DoT (scored separately so
    over-refresh is overcap-safe). The lily heals (Solace/Rapture) carry 0
    potency by design — their value IS the Misery they nourish. There is no
    GCD/oGCD distinction: raid buffs amp all personal damage. `target_fn(t, aid)
    -> n` supplies the per-cast target count (None -> single target,
    byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast
    # multiplier; a no-op for the player's delivered timeline (no marker).
    # Applied before `bi` is read so the Dia DoT snapshot is also tincture-aware.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), wd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        total += base * m
    total += _dia_dot_potency(timeline, bi)
    return total


# --- Phase-continuation entry state ------------------------------------------

def measure_entry_lily_state(norm_casts) -> tuple[int, int]:
    """Infer the lily gauge carried into the pull from the player's own casts
    (deepest-deficit): the lilies / Blood Lily nourishment they must have
    started with to afford their early spends. 0/0 on a cold start (the lily
    timer alone covers every spend) -> a fresh pull stays byte-identical.

    Lily accrual is TIME-based (1 / 20 s), so the deficit compares each spend
    against the maximum the timer could have produced by then — an upper bound
    on accrual, hence a LOWER bound on the carried gauge (never over-seeds the
    ceiling)."""
    spends = sorted(t for t, a in norm_casts
                    if a in (wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE) and t >= 0)
    miseries = sorted(t for t, a in norm_casts
                      if a == wd.AFFLATUS_MISERY and t >= 0)
    entry_lilies = 0
    for i, t in enumerate(spends):
        accrued = int(t // wd.LILY_INTERVAL_S)
        entry_lilies = max(entry_lilies, (i + 1) - accrued)
    entry_lilies = min(max(entry_lilies, 0), wd.LILY_CAP)
    # Each Misery needs 3 nourishes; nourishes available = lily spends before it.
    entry_blood = 0
    for k, t in enumerate(miseries):
        spends_before = sum(1 for s in spends if s < t)
        entry_blood = max(entry_blood,
                          wd.BLOOD_LILY_CAP * (k + 1) - spends_before)
    entry_blood = min(max(entry_blood, 0), wd.BLOOD_LILY_CAP)
    return entry_lilies, entry_blood


# --- Scoring scaffolding (cached ceiling, enabler valuation) -----------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (WHM has no pet scalar);
    `coverage_intervals` is always None (no maintained-coverage overlay).
    `target_intervals` is the multi-target N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    wd.JOB_DATA.tincture_main_stat, wd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=whm_simulator,
    score_timeline=_score_timeline,
    enabler_ids=wd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- WHMScoringAspect ---------------------------------------------------------

class _WhmCtx:
    """Per-pull context: the measured phase-continuation entry lily state,
    plus any mit-plan heal locks the sidecar staged on the report
    (`report["__heal_locks__"]` — see sidecar/main.py::_heal_lock_payload)."""
    __slots__ = ("entry_lilies", "entry_blood", "heal_locks", "heal_lock_state")

    def __init__(self, entry_lilies: int, entry_blood: int,
                 heal_locks: tuple = (), heal_lock_state: dict | None = None):
        self.entry_lilies = entry_lilies
        self.entry_blood = entry_blood
        self.heal_locks = heal_locks
        self.heal_lock_state = heal_lock_state or {}


class WHMScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a WHM run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights
    up unchanged. The per-pull context is the measured entry lily state, fed
    into the ceiling via `sim_context` so a phase-continuation log's loaded
    opener (carried Blood Lily -> early Misery) is matched symmetrically."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # The pre-pull Glare III channel (begincast-anchored, lands at t≈0) is real
    # in-fight damage, credited symmetrically with the channel the sim emits.
    prepull_channel_ids = frozenset({wd.GLARE_III})
    # Per-player Spell Speed: the inference band centers on the 2.5 s Glare
    # cadence; min(2.5, inferred) only ever speeds the ceiling.
    gcd_constant = WHM_GCD_S

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        entry_l, entry_b = measure_entry_lily_state(norm_casts)
        from jobs._core.heal_locks import reconcile_from_report
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        budget = reconcile_from_report(
            report, norm_casts, fight_duration_s,
            costed_ids=wd.COSTED_HEAL_GCD_IDS, locked_heal_id=wd.MEDICA_III,
            filler_potency=float(wd.POTENCIES[wd.GLARE_III]))
        return _WhmCtx(entry_l, entry_b,
                       heal_locks=budget.locks, heal_lock_state=budget.state)

    def sim_context(self, ctx: Any) -> Any:
        whm = WhmContext(entry_lilies=ctx.entry_lilies,
                         entry_blood=ctx.entry_blood)
        # Mit-plan heal locks nest OUTSIDE the entry state (canonical unwrap
        # order in simulator._model_for). Absent (refs, plan-less runs) the
        # payload stays exactly the historic WhmContext/None — cache keys and
        # every unlocked ceiling are byte-identical.
        if ctx.heal_locks:
            from jobs._core.heal_locks import HealLockContext
            return HealLockContext(locks=ctx.heal_locks,
                                   inner=(whm if whm else None))
        # None on a cold start so the warm-cache / direct-call cache keys match.
        return whm if whm else None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts,
                                       buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        state = {
            "entryLilies": ctx.entry_lilies,
            "entryBlood": ctx.entry_blood,
        }
        # Absent-key discipline: the reconciled heal-lock block appears only on a
        # locked (mit-plan / prog) run, so every unlocked response keeps its
        # historic shape.
        state.update(ctx.heal_lock_state)
        return state
