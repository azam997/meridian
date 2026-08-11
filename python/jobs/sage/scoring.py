"""SGE-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is SGE-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

SGE's one bespoke damage piece is the **Eukrasian Dosis III DoT**: a ~30s DoT scored
per cast by *time-to-next-application* (capped at the duration) so an early refresh
credits less — overcap-safe and symmetric (the same function scores the player's
timeline and the ceiling's). The DoT snapshots buffs at cast time. Everything else is
flat table potency × the raid-buff / in-sim-tincture multiplier at the cast instant.

**No self-buff derivation, and no party buff of SGE's own.** SGE brings NO raid
damage buff (Kardia is a single-target heal link), so `buff_intervals` only ever
carries OTHER jobs' buffs (job-agnostically, via jobs/_core/raid_buffs.py). The
scorer is RDM/AST/SCH-shaped: `table potency × multiplier_at(buff_intervals)` + the
Eukrasian Dosis DoT. `coverage_intervals` is None (no maintained personal damage
buff overlay).

**Flat GCD => `demonstrated_cadence_anchor = True`.** SGE has no GCD-recast haste
self-buff, so there is no modeled sub-GCD haste window; a top parse sustaining a
tighter server-tick cadence than the fixed band floor is bounded by scoring the
ceiling at `uptime / player-GCD-count` (the GNB/SCH pattern). Monotone-safe.

The per-pull `sim_context` carries the mit-plan heal locks (staged on the report by
the sidecar); SGE has no seedable gauge, so there is no entry-state measurement (the
SageContext phase-continuation payload is a v1 stub).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.sage import data as gd
from jobs.sage import simulator as sge_simulator
from jobs.sage.simulator import SGE_GCD_S


def _eukrasian_dosis_dot_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None,
) -> float:
    """Eukrasian Dosis III DoT potency, summed per application. Each cast is credited
    for `min(duration, time-to-next-Eukrasian-Dosis)` of ticks — so over-refreshing
    (clipping the DoT) credits less, never double-counts. The DoT snapshots buffs at
    cast time, so the multiplier is read at the application instant (not per tick)."""
    casts = sorted(t for t, aid in timeline if aid == gd.EUKRASIAN_DOSIS_III)
    if not casts:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + gd.EUKRASIAN_DOSIS_DOT_DURATION_S
    total = 0.0
    for i, ct in enumerate(casts):
        nxt = casts[i + 1] if i + 1 < len(casts) else span_end
        covered_s = min(gd.EUKRASIAN_DOSIS_DOT_DURATION_S, max(0.0, nxt - ct))
        n_ticks = covered_s / gd.EUKRASIAN_DOSIS_DOT_TICK_S
        m = multiplier_at(ct, buff_intervals) if buff_intervals else 1.0
        total += n_ticks * gd.EUKRASIAN_DOSIS_DOT_TICK_P * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency × the raid-buff
    multiplier at its time, plus the Eukrasian Dosis III DoT (scored separately so
    over-refresh is overcap-safe). Eukrasia scores 0 (a setup GCD). There is no
    GCD/oGCD distinction: raid buffs amp all personal damage. `target_fn(t, aid) ->
    n` supplies the per-cast target count (None -> single target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier;
    # a no-op for the player's delivered timeline (no marker). Applied before the DoT
    # is read so the Eukrasian Dosis snapshot is also tincture-aware.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), gd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        total += base * m
    total += _eukrasian_dosis_dot_potency(timeline, bi)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) -----------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (SGE has no pet scalar);
    `coverage_intervals` is always None (no maintained-coverage overlay).
    `target_intervals` is the multi-target N(t) schedule (None -> single target,
    byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    gd.JOB_DATA.tincture_main_stat, gd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=sge_simulator,
    score_timeline=_score_timeline,
    enabler_ids=gd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- SageScoringAspect --------------------------------------------------------

class _SgeCtx:
    """Per-pull context: any mit-plan heal locks the sidecar staged on the report
    (`report["__heal_locks__"]` — see sidecar/main.py::_heal_lock_payload). SGE has
    no seedable gauge, so there is no offensive entry state to measure here."""
    __slots__ = ("heal_locks", "heal_lock_state")

    def __init__(self, heal_locks: tuple = (), heal_lock_state: dict | None = None):
        self.heal_locks = heal_locks
        self.heal_lock_state = heal_lock_state or {}


class SageScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a SGE run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the mit-plan heal locks (fed into the ceiling
    via `sim_context`) so the ceiling already pays the healing tax — the honest
    maximum for a healer."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # The pre-pull Dosis III channel (begincast-anchored, lands at t≈0) is real
    # in-fight damage, credited symmetrically with the channel the sim emits.
    prepull_channel_ids = frozenset({gd.DOSIS_III})
    # Per-player Spell Speed: the inference band centers on the 2.5s Dosis III
    # cadence; min(2.5, inferred) only ever speeds the ceiling.
    gcd_constant = SGE_GCD_S
    # SGE is a MIXED fixed-rate job (like NIN): Eukrasia is a fixed, speed-immune
    # ~1.0s GCD interleaved with the normal 2.5s GCDs. The demonstrated-cadence anchor
    # IS valid — but only made mixed-aware: `gcd_inference_exclusions` (below) returns
    # each Eukrasia cast as a 1.0s window, which `demonstrated_cadence` subtracts from
    # BOTH the uptime and the GCD count, yielding `(uptime − eukrasia_time) /
    # normal_gcd_count` — the sustained NORMAL-GCD cadence, without folding the fast
    # Eukrasia slots in (which would over-credit — the NIN caveat). This is load-bearing:
    # live M12S-P2 top parses sustain a tighter normal cadence than the clean-pair p15
    # inference reads (dilution), so without it the ceiling reads >100%. Monotone-safe
    # (only added when FASTER than the sweep floor → normal-speed parses byte-identical).
    demonstrated_cadence_anchor = True

    def _demonstrated_cadence(self, norm_casts, is_gcd, fight_duration_s,
                              downtime_windows, haste_windows):
        """Mixed-aware sustained cadence = (uptime − fixed_gcd_time) / normal_gcd_count.
        SGE's DoT sequence runs at fixed speed-immune recasts — Eukrasia 1.0s AND
        Eukrasian Dosis III 1.5s (gd.FIXED_RATE_GCDS) — so BOTH are removed from the
        available uptime and excluded from the counted GCDs, leaving the sustained
        NORMAL (hasted-filler) cadence. The shared `uptime / GCD-count` can't do this:
        counting the fast GCDs folds them in (over-credit, the NIN caveat), and an
        exclusion-window approach mis-attributes the tightly-spaced pair. Computing it
        directly is exact. Monotone-safe (the caller floors it at 0.95×constant and only
        adds it when faster than the sweep floor). The sub-band fixed GCDs self-exclude
        from the gear inference (below the 0.80×constant band), so no
        `gcd_inference_exclusions` hook is needed."""
        def _in(t):
            return any(s <= t < e for s, e in downtime_windows)
        fixed_time = sum(gd.FIXED_RATE_GCDS[a] for t, a in norm_casts
                         if t >= 0.0 and a in gd.FIXED_RATE_GCDS and not _in(t))
        normal_n = sum(1 for t, a in norm_casts
                       if t >= 0.0 and is_gcd(a)
                       and a not in gd.FIXED_RATE_GCDS and not _in(t))
        if normal_n < 12:
            return None
        off = sum(min(fight_duration_s, e) - max(0.0, s)
                  for s, e in downtime_windows if e > 0.0 and s < fight_duration_s)
        uptime = fight_duration_s - max(0.0, off) - fixed_time
        return uptime / normal_n if uptime > 0 else None

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        from jobs._core.heal_locks import reconcile_from_report
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        budget = reconcile_from_report(
            report, norm_casts, fight_duration_s,
            costed_ids=gd.COSTED_HEAL_GCD_IDS,
            locked_heal_id=gd.EUKRASIAN_PROGNOSIS_II,
            filler_potency=float(gd.POTENCIES[gd.DOSIS_III]))
        return _SgeCtx(heal_locks=budget.locks, heal_lock_state=budget.state)

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
