"""SCH-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is SCH-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

SCH's one bespoke damage piece is the **Biolysis DoT**: a ~30s DoT scored per cast
by *time-to-next-application* (capped at the duration) so an early refresh credits
less — overcap-safe and symmetric (the same function scores the player's timeline
and the ceiling's). The DoT snapshots buffs at cast time. Baneful Impaction's short
15s DoT is instead FOLDED into its cast potency (data.py) — fired once per Chain
Stratagem, never clipped — so it scores as a flat table cast. Everything else is
flat table potency × the raid-buff / in-sim-tincture multiplier at the cast instant.

**No self-buff derivation.** Unlike GNB (whose No Mercy rides the timeline), SCH's
only party amp is **Chain Stratagem**, which is modeled once, job-agnostically, as a
party `BuffProvider` (jobs/_core/raid_buffs.py) flowing through `buff_intervals`.
Re-deriving it here from Chain Stratagem casts would double-count. So the scorer is
RDM/AST-shaped: `table potency × multiplier_at(buff_intervals)` + the Biolysis DoT.
`coverage_intervals` is None (no maintained personal damage buff overlay).

**Flat GCD => `demonstrated_cadence_anchor = True`.** SCH has no GCD-recast haste
self-buff, so there is no modeled sub-GCD haste window; a top parse sustaining a
tighter server-tick cadence than the fixed band floor is bounded by scoring the
ceiling at `uptime / player-GCD-count` (the GNB pattern). Monotone-safe.

The per-pull `sim_context` carries the mit-plan heal locks (staged on the report by
the sidecar); SCH's Aetherflow gauge lives in the simulator's SimState (not a
seedable GaugeModel), so there is no entry-state measurement (the SchContext
phase-continuation payload is a v1 stub).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.scholar import data as sd
from jobs.scholar import simulator as sch_simulator
from jobs.scholar.simulator import SCH_GCD_S


def _biolysis_dot_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None,
) -> float:
    """Biolysis DoT potency, summed per application. Each cast is credited for
    `min(duration, time-to-next-Biolysis)` of ticks — so over-refreshing (clipping
    the DoT) credits less, never double-counts. The DoT snapshots buffs at cast
    time, so the multiplier is read at the application instant (not per tick)."""
    casts = sorted(t for t, aid in timeline if aid == sd.BIOLYSIS)
    if not casts:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + sd.BIOLYSIS_DOT_DURATION_S
    total = 0.0
    for i, ct in enumerate(casts):
        nxt = casts[i + 1] if i + 1 < len(casts) else span_end
        covered_s = min(sd.BIOLYSIS_DOT_DURATION_S, max(0.0, nxt - ct))
        n_ticks = covered_s / sd.BIOLYSIS_DOT_TICK_S
        m = multiplier_at(ct, buff_intervals) if buff_intervals else 1.0
        total += n_ticks * sd.BIOLYSIS_DOT_TICK_P * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency × the raid-buff
    multiplier at its time, plus the Biolysis DoT (scored separately so over-refresh
    is overcap-safe). Chain Stratagem and Aetherflow carry 0 potency (Chain
    Stratagem's party value is external via `buff_intervals`; Aetherflow is a
    resource refill). There is no GCD/oGCD distinction: raid buffs amp all personal
    damage. `target_fn(t, aid) -> n` supplies the per-cast target count (None ->
    single target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier;
    # a no-op for the player's delivered timeline (no marker). Applied before `bi`
    # is read so the Biolysis DoT snapshot is also tincture-aware.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), sd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        total += base * m
    total += _biolysis_dot_potency(timeline, bi)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) -----------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (SCH's fairy is heal-only, no
    pet scalar); `coverage_intervals` is always None (no maintained-coverage
    overlay). `target_intervals` is the multi-target N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=sch_simulator,
    score_timeline=_score_timeline,
    enabler_ids=sd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- ScholarScoringAspect -----------------------------------------------------

class _SchCtx:
    """Per-pull context: any mit-plan heal locks the sidecar staged on the report
    (`report["__heal_locks__"]` — see sidecar/main.py::_heal_lock_payload). SCH's
    Aetherflow gauge lives in the simulator's SimState, not a seedable GaugeModel,
    so there is no offensive entry state to measure here."""
    __slots__ = ("heal_locks", "heal_lock_state")

    def __init__(self, heal_locks: tuple = (), heal_lock_state: dict | None = None):
        self.heal_locks = heal_locks
        self.heal_lock_state = heal_lock_state or {}


class ScholarScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a SCH run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the mit-plan heal locks (fed into the ceiling
    via `sim_context`) so the ceiling already pays the healing tax — the honest
    maximum for a healer."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # The pre-pull Broil IV channel (begincast-anchored, lands at t≈0) is real
    # in-fight damage, credited symmetrically with the channel the sim emits.
    prepull_channel_ids = frozenset({sd.BROIL_IV})
    # Per-player Spell Speed: the inference band centers on the 2.5s Broil IV
    # cadence; min(2.5, inferred) only ever speeds the ceiling.
    gcd_constant = SCH_GCD_S
    # SCH has a FLAT GCD (no recast-haste self-buff — no modeled sub-GCD haste
    # window), so the demonstrated-cadence anchor is valid: a top parse sustaining a
    # tighter server-tick cadence than the fixed band floor is bounded by
    # `uptime / player-GCD-count` (monotone-safe, GNB pattern).
    demonstrated_cadence_anchor = True

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        from jobs._core.heal_locks import reconcile_from_report
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        budget = reconcile_from_report(
            report, norm_casts, fight_duration_s,
            costed_ids=sd.COSTED_HEAL_GCD_IDS, locked_heal_id=sd.CONCITATION,
            filler_potency=float(sd.POTENCIES[sd.BROIL_IV]))
        return _SchCtx(heal_locks=budget.locks, heal_lock_state=budget.state)

    def sim_context(self, ctx: Any) -> Any:
        # Mit-plan heal locks nest OUTSIDE the (empty) entry state. Absent (refs,
        # plan-less runs) the payload is None — cache keys and every unlocked
        # ceiling are byte-identical.
        if ctx.heal_locks:
            from jobs._core.heal_locks import HealLockContext
            return HealLockContext(locks=ctx.heal_locks, inner=None)
        return None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        # Absent-key discipline: the reconciled heal-lock block appears only on a
        # locked (mit-plan / prog) run, so every unlocked response keeps its
        # historic shape.
        return dict(ctx.heal_lock_state)
