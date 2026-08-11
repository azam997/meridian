"""SAM-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is SAM-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` + `ScoringAspectBase`.

SAM's three bespoke damage pieces:

  * **Fugetsu** — a maintained 13% personal-damage self-buff. Modeled as a
    `coverage_intervals` overlay (like WAR Surging Tempest / RPR Death's Design):
    the idealized side assumes full coverage; the delivered side is scaled by the
    *measured* coverage (jobs/samurai/buffs.py). At 100% uptime the x1.13 cancels.
  * **Guaranteed critical hits** — Midare / Tendo Setsugekka (+ their Kaeshi) and
    Ogi / Kaeshi Namikiri always crit (crit only, no DH). Priced with a flat crit
    multiplier, symmetric on delivered + idealized.
  * **Higanbana DoT** — a 60s DoT scored per cast by *time-to-next-cast* (capped
    at 60s) so an early refresh credits less (overcap-safe), snapshotting the buff
    multiplier at cast time. Symmetric: the same function scores both timelines.

The per-pull `sim_context` is the player's measured Tengentsu Kenki, threaded into
the idealized ceiling so it spends the same Kenki the player got (the RDM
proc-budget pattern); see jobs/samurai/data.py.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.samurai import data as sd
from jobs.samurai import simulator as sam_simulator
from jobs.samurai.simulator import SamContext


# Fugetsu covers the whole fight on the idealized side. Padded start so a t~0
# opener cast is inside the window.
def _full_fugetsu_intervals(duration_s: float) -> list[tuple[float, float, float]]:
    return [(-10.0, duration_s + 1.0, sd.FUGETSU_MULT)]


def _higanbana_dot_potency(
    timeline: list[tuple[float, int]],
    fugetsu_intervals: list[tuple[float, float, float]] | None,
    buff_intervals: list[tuple[float, float, float]] | None,
    optimistic: bool = False,
) -> float:
    """Higanbana DoT potency, summed per cast. Each application is credited for
    `min(60s, time-to-next-Higanbana)` of ticks — so over-refreshing (clipping the
    DoT) credits less, never double-counts. The DoT snapshots buffs at cast time,
    so the multiplier is read at the application instant (not per tick).

    `optimistic=True` credits every application the FULL 60s DoT regardless of the
    next refresh — an admissible upper bound used only as the beam-search *prune*
    key, so a Higanbana-light / more-Midare line isn't pruned mid-search just
    because the exact (time-to-next) score temporarily favours tighter refreshes.
    The final selection always uses the exact (optimistic=False) score."""
    casts = sorted(t for t, aid in timeline if aid == sd.HIGANBANA)
    if not casts:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + sd.HIGANBANA_DOT_DURATION_S
    total = 0.0
    for i, ct in enumerate(casts):
        nxt = casts[i + 1] if i + 1 < len(casts) else span_end
        covered_s = (sd.HIGANBANA_DOT_DURATION_S if optimistic
                     else min(sd.HIGANBANA_DOT_DURATION_S, max(0.0, nxt - ct)))
        n_ticks = covered_s / sd.HIGANBANA_DOT_TICK_S
        m = 1.0
        if fugetsu_intervals:
            m *= multiplier_at(ct, fugetsu_intervals)
        if buff_intervals:
            m *= multiplier_at(ct, buff_intervals)
        total += n_ticks * sd.HIGANBANA_DOT_TICK_P * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    fugetsu_intervals: list[tuple[float, float, float]] | None = None,
    buff_intervals: list[tuple[float, float, float]] | None = None,
    optimistic_dot: bool = False,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Every cast's table potency is scaled by:
      - the guaranteed crit multiplier when the cast always crits (Setsugekka /
        Namikiri families),
      - the Fugetsu multiplier active at its time (`fugetsu_intervals`, the
        measured coverage; default off — the form the sweep / refinement use
        internally, where Fugetsu is a constant x1.13 and so doesn't change the
        argmax),
      - the raid-buff multiplier (`buff_intervals`, default off).
    Plus the Higanbana DoT (scored separately so over-refresh is overcap-safe).

    There is no GCD/oGCD distinction: Fugetsu and raid buffs amp *all* personal
    damage, so the Kenki oGCDs (Shinten, Senei, Zanshin) and Shoha are scaled too.
    """
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker). Applied before `bi` so the
    # Higanbana DoT snapshot below is also tincture-aware.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    crit = sd.GUARANTEED_CRIT_MULT
    fg = fugetsu_intervals or None
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), sd.JOB_DATA)
        if base <= 0:
            continue
        m = 1.0
        if aid in sd.ALWAYS_CRIT_IDS:
            m *= crit
        if fg:
            m *= multiplier_at(t, fg)
        if bi:
            m *= multiplier_at(t, bi)
        total += base * m
    total += _higanbana_dot_potency(timeline, fg, bi, optimistic=optimistic_dot)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (SAM has no pet scalar);
    `coverage_intervals` is the Fugetsu overlay (full on the idealized side,
    measured on the delivered side). `target_intervals` is the multi-target N(t)
    schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, fugetsu_intervals=coverage_intervals, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=sam_simulator,
    score_timeline=_score_timeline,
    enabler_ids=sd.ENABLER_IDS,
    coverage_intervals=_full_fugetsu_intervals,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- SAMScoringAspect ------------------------------------------------------

class _SamCtx:
    """Per-pull context: measured Fugetsu coverage, Tengentsu Kenki, and the
    entry gauge carried into the pull (a phased fight's P1->P2 leftover)."""
    __slots__ = ("fugetsu_intervals", "bonus_kenki", "fugetsu_pct",
                 "entry_kenki", "entry_meditation")

    def __init__(self, fugetsu_intervals, bonus_kenki, fugetsu_pct,
                 entry_kenki, entry_meditation):
        self.fugetsu_intervals = fugetsu_intervals
        self.bonus_kenki = bonus_kenki
        self.fugetsu_pct = fugetsu_pct
        self.entry_kenki = entry_kenki
        self.entry_meditation = entry_meditation


class SAMScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a SAM run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the MEASURED Fugetsu coverage (delivered
    multiplier) plus the measured Tengentsu Kenki (fed into the ceiling via
    `sim_context` so the lenient / timeline sims spend the same Kenki)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: the inference band centers on the Fuka GCD (2.14), so it
    # measures the player's hasted cadence; min(2.14, inferred) only speeds the ceiling.
    gcd_constant = sam_simulator.SAM_GCD_S

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        from jobs.samurai.buffs import (
            fugetsu_coverage_pct,
            measure_entry_gauge,
            measured_fugetsu_intervals,
            measured_tengentsu_kenki,
        )
        fg = measured_fugetsu_intervals(client, code, report, fight, actor)
        bonus = measured_tengentsu_kenki(client, code, fight, actor)
        entry_k, entry_m = measure_entry_gauge(norm_casts)
        dur = (fight["endTime"] - fight["startTime"]) / 1000.0
        return _SamCtx(fg, bonus, fugetsu_coverage_pct(fg, dur), entry_k, entry_m)

    def sim_context(self, ctx: Any) -> Any:
        # meditate_cap_s is None here (pre-ref); the sidecar refines it down to the
        # ref-observed cap post-fetch (see jobs.samurai.refine_sim_context_from_refs).
        return SamContext(bonus_kenki=ctx.bonus_kenki, entry_kenki=ctx.entry_kenki,
                          entry_meditation=ctx.entry_meditation, meditate_cap_s=None)

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(
            in_fight_casts, fugetsu_intervals=ctx.fugetsu_intervals,
            buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        return {
            "sim_context": self.sim_context(ctx),
            "tengentsuKenki": ctx.bonus_kenki,
            "fugetsuUptimePct": ctx.fugetsu_pct,
            "entryKenki": ctx.entry_kenki,
            "entryMeditation": ctx.entry_meditation,
        }
