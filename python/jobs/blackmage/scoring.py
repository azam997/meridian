"""BLM-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is BLM-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

BLM's damage side is simple compared to SAM: no maintained personal amp (so
`coverage_intervals` is `None`, like RDM) and no guaranteed-crit family — every
cast scores its table potency, scaled only by raid buffs / tincture. The Astral
Fire element multiplier (fire spells land ~1.8x on the meter under AF3) is
deliberately NOT scored: the phase structure is forced for every player and for
the sim, so it cancels in delivered/idealized like crit RNG (see data.py). The one
bespoke piece is the **High Thunder DoT**, scored per cast by *time-to-next-cast*
(capped at the 30s duration) so an early refresh credits less (overcap-safe),
snapshotting the buff multiplier at the application instant. The same function
scores both the player's timeline and the idealized ceiling, so it's symmetric.

BLM is RNG-free, so there is no proc-budget `sim_context` (the RDM wrinkle); the
only per-pull context is the per-player effective GCD (threaded by the shared
`gcd_constant` machinery in `ScoringAspectBase`).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.blackmage import data as bd
from jobs.blackmage import simulator as blm_simulator


def _high_thunder_dot_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None,
) -> float:
    """High Thunder DoT potency, summed per cast. Each application is credited for
    `min(30s, time-to-next-High-Thunder)` of ticks — so over-refreshing (clipping
    the DoT) credits less, never double-counts. The DoT snapshots buffs at cast
    time, so the multiplier is read at the application instant (not per tick)."""
    casts = sorted(t for t, aid in timeline if aid == bd.HIGH_THUNDER)
    if not casts:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + bd.HIGH_THUNDER_DOT_DURATION_S
    total = 0.0
    for i, ct in enumerate(casts):
        nxt = casts[i + 1] if i + 1 < len(casts) else span_end
        covered_s = min(bd.HIGH_THUNDER_DOT_DURATION_S, max(0.0, nxt - ct))
        n_ticks = covered_s / bd.DOT_TICK_S
        m = multiplier_at(ct, buff_intervals) if buff_intervals else 1.0
        total += n_ticks * bd.HIGH_THUNDER_DOT_TICK_P * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency, scaled by the
    raid-buff multiplier active at its time (`buff_intervals`, default off — the
    form the sweep / refinement use internally). Plus the High Thunder DoT (scored
    separately so over-refresh is overcap-safe). BLM has no GCD/oGCD scoring
    distinction and no job-wide amp, so every entry in POTENCIES scores the same.
    `target_fn(t, aid) -> n` supplies the per-cast target count (None -> single
    target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker). Applied before `bi` so the
    # High Thunder DoT snapshot below is also tincture-aware.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), bd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        total += base * m
    total += _high_thunder_dot_potency(timeline, bi)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (BLM has no pet scalar);
    `coverage_intervals` is None (no job-wide overlay). `target_intervals` is the
    multi-target N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    bd.JOB_DATA.tincture_main_stat, bd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=blm_simulator,
    score_timeline=_score_timeline,
    enabler_ids=bd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- BLMScoringAspect ------------------------------------------------------

class BLMScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a BLM run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. BLM is RNG-free, so the only per-pull context is the per-player
    effective GCD (handled by the shared `gcd_constant` machinery)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Spell Speed: casters vary far more than ranged, so this is where the
    # inference matters most. min(2.5, inferred) tightens the ceiling for a fast-SpS
    # BLM (SpS scales the cast times too — handled in the model).
    gcd_constant = blm_simulator.GCD_BASE_S
    # The pre-pull Fire III (hardcast during the countdown, resolving at t≈0) is real
    # opener damage — credit the one nearest t=0 to delivered, matching the channel
    # the sim emits in prepull.
    prepull_channel_ids = frozenset({bd.FIRE_III})

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def gcd_inference_exclusions(self, norm_casts):
        """Exclude Ley Lines windows from the per-player gear-GCD inference. LL
        hastes GCDs to ~2.1s — inside the inference band — so without this the
        inference reads the LL haste as fast GEAR and the ceiling double-counts it
        (it already models the LL haste window in `simulator.gcd_duration`). Each
        LL cast opens a window [t, t+duration]."""
        return [(t, t + bd.LEY_LINES_DURATION_S)
                for t, aid in norm_casts if aid == bd.LEY_LINES and t >= 0]
