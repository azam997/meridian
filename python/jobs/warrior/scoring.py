"""WAR-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is WAR-specific; everything around
it — the LRU-cached perfect-sim ceiling, the enabler valuation, and the `Scoring`
aspect's analyze flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring`
+ `ScoringAspectBase`.

WAR's two bespoke damage pieces:

  * **Surging Tempest** — a maintained 10% personal-damage self-buff (the WAR
    analog of RPR's Death's Design). Modeled as a `coverage_intervals` overlay:
    the idealized side assumes full coverage; the delivered side is scaled by the
    *measured* coverage from the player's own damage events (surging_tempest.py).
    A dropped/late Surging Tempest costs efficiency; at 100% uptime the x1.10
    cancels in the ratio (preserving the efficiency <= 100% guard).
  * **Guaranteed crit-DH** — Inner Chaos and Primal Rend always land a guaranteed
    critical direct hit, and the free Fell Cleaves cast inside an Inner Release
    window do too. These are priced with a flat crit-DH multiplier (like MCH
    Reassemble / Full Metal Field). The Inner Release windows are derived from the
    timeline's own Inner Release casts, so the treatment is symmetric on delivered
    + idealized.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.entry_gauge import entry_state
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.warrior import data as wd
from jobs.warrior import simulator as war_simulator


# Surging Tempest covers the whole fight on the idealized side. Padded start so
# a t~0 opener cast is inside the window.
def _full_st_intervals(duration_s: float) -> list[tuple[float, float, float]]:
    return [(-10.0, duration_s + 1.0, wd.SURGING_TEMPEST_MULT)]


def _inner_release_windows(
    timeline: list[tuple[float, int]],
) -> list[tuple[float, float]]:
    """Inner Release windows derived from the IR casts in the timeline:
    `[(t, t + INNER_RELEASE_WINDOW_S)]`. A free Fell Cleave inside one is a
    guaranteed crit-DH (the 3 free weaponskills); used symmetrically on delivered
    + idealized. Inner Chaos / Primal Rend are crit-DH regardless of the window."""
    return [(t, t + wd.INNER_RELEASE_WINDOW_S)
            for t, aid in timeline if aid == wd.INNER_RELEASE]


def _in_windows(t: float, windows: list[tuple[float, float]]) -> bool:
    return any(s <= t < e for s, e in windows)


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    st_intervals: list[tuple[float, float, float]] | None = None,
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Every cast's table potency is scaled by:
      - a guaranteed crit-DH multiplier when the cast is Inner Chaos / Primal Rend
        (always) or a free Fell Cleave inside an Inner Release window (derived
        from this same timeline's IR casts),
      - the Surging Tempest multiplier active at its time (`st_intervals`, the
        measured coverage; default off — the form the sweep / refinement use
        internally, where Surging Tempest is a constant x1.10 and so doesn't
        change the argmax),
      - the raid-buff multiplier (`buff_intervals`, default off).

    There is no GCD/oGCD distinction: Surging Tempest and raid buffs amp *all*
    personal damage, so the damage oGCDs (Upheaval, Onslaught, Primal Wrath) are
    scaled too.
    """
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    ir_windows = _inner_release_windows(timeline)
    crit_dh = wd.GUARANTEED_CRIT_DH_MULT
    st = st_intervals or None
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), wd.JOB_DATA)
        if base <= 0:
            continue
        m = 1.0
        if aid in wd.ALWAYS_CRIT_DH_IDS:
            m *= crit_dh
        elif aid in (wd.FELL_CLEAVE, wd.DECIMATE) and _in_windows(t, ir_windows):
            # Decimate is the AoE Fell Cleave — also a guaranteed crit-DH when
            # cast as a free Inner Release weaponskill.
            m *= crit_dh
        if st:
            m *= multiplier_at(t, st)
        if bi:
            m *= multiplier_at(t, bi)
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (WAR has no pet scalar);
    `coverage_intervals` is the Surging Tempest overlay (full on the idealized
    side, measured on the delivered side). `target_intervals` is the multi-target
    N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, st_intervals=coverage_intervals, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    wd.JOB_DATA.tincture_main_stat, wd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=war_simulator,
    score_timeline=_score_timeline,
    enabler_ids=wd.ENABLER_IDS,
    coverage_intervals=_full_st_intervals,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- WarriorScoringAspect --------------------------------------------------

class _WarCtx:
    """Per-pull context: measured Surging Tempest coverage (delivered multiplier)
    plus the phase-continuation entry state (carried Beast gauge)."""
    __slots__ = ("st_intervals", "entry")

    def __init__(self, st_intervals, entry):
        self.st_intervals = st_intervals
        self.entry = entry


class WarriorScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a WAR run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the MEASURED Surging Tempest coverage plus
    the phase-continuation entry state (carried Beast gauge; a tank opens at t=0
    so there is no opener override)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    gcd_constant = war_simulator.GCD_BASE_S

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        from jobs.warrior.surging_tempest import measured_st_intervals
        st = measured_st_intervals(client, code, report, fight, actor)
        entry = entry_state(norm_casts, wd.JOB_DATA.gauges)
        return _WarCtx(st, entry)

    def sim_context(self, ctx: Any) -> Any:
        # Carried-Beast entry state threaded into the ceiling. None (cold start)
        # keeps the ceiling pure (duration, downtime, buffs) data, byte-identical.
        return ctx.entry or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(
            in_fight_casts, st_intervals=ctx.st_intervals, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        e = ctx.entry
        return {
            "sim_context": e or None,
            "entryGauges": dict(e.gauges) if e else {},
        }
