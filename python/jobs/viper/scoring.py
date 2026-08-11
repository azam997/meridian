"""VPR-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is VPR-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

VPR's one bespoke damage piece is **Hunter's Instinct** — a maintained 10%
personal-damage SELF-buff (the SAM-Fugetsu analog, not an enemy debuff like RPR
Death's Design). Modeled as a `coverage_intervals` overlay: the idealized side
assumes full coverage; the delivered side is scaled by the *measured* coverage
(jobs/viper/buffs.py). At 100% uptime the x1.10 cancels in the ratio.

No DoT, no guaranteed crits, no per-pull luck scalar — a flat per-cast potency sum
scaled by Hunter's Instinct x raid buffs x the in-sim tincture (the simplest scorer
of the shipped jobs). The Reawakened combo's reduced recast is handled in the
simulator's `gcd_duration`; its fast GCDs are kept out of the GEAR-GCD inference via
`gcd_inference_exclusions` (BLM Ley Lines pattern).
"""
from __future__ import annotations

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.viper import data as vd
from jobs.viper import simulator as vpr_simulator


def _full_hunters_instinct_intervals(
        duration_s: float) -> list[tuple[float, float, float]]:
    """Hunter's Instinct covers the whole fight on the idealized side. Padded start
    so a t~0 opener cast is inside the window."""
    return [(-10.0, duration_s + 1.0, vd.HUNTERS_INSTINCT_MULT)]


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    hunters_instinct_intervals: list[tuple[float, float, float]] | None = None,
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Every cast's per-target table potency is
    scaled by:
      - the Hunter's Instinct multiplier active at its time (`hunters_instinct_
        intervals`, the measured coverage; default off — the form the sweep /
        refinement use internally, where HI is a constant x1.10 and so doesn't
        change the argmax),
      - the raid-buff multiplier (`buff_intervals`, default off).
    There is no GCD/oGCD distinction: Hunter's Instinct and raid buffs amp *all*
    personal damage, so the Twin/Legacy/Death-Rattle oGCDs are scaled too."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    hi = hunters_instinct_intervals or None
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), vd.JOB_DATA)
        if base <= 0:
            continue
        m = 1.0
        if hi:
            m *= multiplier_at(t, hi)
        if bi:
            m *= multiplier_at(t, bi)
        total += base * m
    return total


def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (VPR has no pet scalar);
    `coverage_intervals` is the Hunter's Instinct overlay (full on the idealized
    side, measured on the delivered side). `target_intervals` is the cleave N(t)
    schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, hunters_instinct_intervals=coverage_intervals,
        buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    vd.JOB_DATA.tincture_main_stat, vd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=vpr_simulator,
    score_timeline=_score_timeline,
    enabler_ids=vd.ENABLER_IDS,
    coverage_intervals=_full_hunters_instinct_intervals,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- VPRScoringAspect ------------------------------------------------------

class _VprCtx:
    """Per-pull context: the measured Hunter's Instinct coverage (the delivered
    multiplier) + its uptime %."""
    __slots__ = ("hi_intervals", "hi_pct")

    def __init__(self, hi_intervals, hi_pct):
        self.hi_intervals = hi_intervals
        self.hi_pct = hi_pct


class VPRScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a VPR run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the MEASURED Hunter's Instinct coverage (the
    delivered multiplier; the idealized side assumes full coverage)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill-Speed inference, capped at the Swiftscaled standard GCD floor
    # (2.5 x 0.85 = 2.125 = simulator.VPR_GCD_S). It WAS disabled (None) while the sim
    # used one blended GCD: the inference reads the tight single-weave floor, and a
    # blend then ran the SLOW GCDs (Coils/Uncoiled) at that floor too -> ~8%
    # over-credit ("the unreachable ghost"). Now `gcd_duration` is per-ability (the
    # recast mults give each GCD its true speed), so the inferred floor IS the correct
    # normal cadence and the slow GCDs stay slow — making the inference the right
    # per-player ceiling: a genuinely sub-GCD parse (e.g. a 2.09s normal cadence) is
    # scored against its own faster ceiling instead of beating a fixed constant. The
    # fast Reawakened combo is excluded from the inference below.
    gcd_constant = 2.125

    def prepare(self, client, code, fight, actor, report, norm_casts):
        from jobs.viper.buffs import (
            hunters_instinct_coverage_pct,
            measured_hunters_instinct_intervals,
        )
        hi = measured_hunters_instinct_intervals(client, code, report, fight, actor)
        dur = (fight["endTime"] - fight["startTime"]) / 1000.0
        return _VprCtx(hi, hunters_instinct_coverage_pct(hi, dur))

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None):
        return score_delivered_potency(
            in_fight_casts, hunters_instinct_intervals=ctx.hi_intervals,
            buff_intervals=buff_intervals)

    def extra_state(self, ctx):
        return {"hunterInstinctUptimePct": ctx.hi_pct}

    def gcd_inference_exclusions(self, norm_casts):
        # Keep the fast Reawakened combo (Generations + Ouroboros, ~1.7s) out of the
        # GEAR-GCD inference: it single-weaves, so it would land in the ≤1-weave
        # inference band and read as fast gear. The slow Coils / Uncoiled Fury
        # (2-weave) fall outside the band on their own. The ceiling already models
        # every tier via the per-ability `gcd_duration`.
        from jobs.viper.buffs import reawaken_windows
        return reawaken_windows(norm_casts)
