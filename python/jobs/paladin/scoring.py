"""PLD-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is PLD-specific; everything around
it — the LRU-cached perfect-sim ceiling, the enabler valuation, and the `Scoring`
aspect's analyze flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring`
+ `ScoringAspectBase`.

PLD's one genuinely-bespoke damage piece is **Fight or Flight**: a 20s personal
damage-up the player places (PLD's analog of RPR's Death's Design, but a burst
window rather than a maintained debuff). It's modeled by deriving the FoF windows
from the **FoF casts in the timeline itself** and multiplying every cast within
them. Because both the delivered and idealized timelines carry their own FoF cast,
the treatment is symmetric — a late/dropped FoF, or GCDs lost under it, costs
efficiency, while a perfectly-used FoF cancels in the ratio (preserving the
efficiency <= 100% guard). Unlike RPR there is no full-coverage overlay, so
`coverage_intervals` is None.
"""
from __future__ import annotations

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.paladin import data as pd
from jobs.paladin import simulator as pld_simulator


def _fof_windows(timeline: list[tuple[float, int]]
                 ) -> list[tuple[float, float, float]]:
    """Fight or Flight windows derived from the FoF casts in the timeline:
    `[(t, t + duration, multiplier)]`. Used symmetrically on delivered + idealized."""
    return [(t, t + pd.FIGHT_OR_FLIGHT_DURATION_S, pd.FIGHT_OR_FLIGHT_MULT)
            for t, aid in timeline if aid == pd.FIGHT_OR_FLIGHT]


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Every cast's table potency is scaled by
    the Fight or Flight multiplier active at its time (derived from this same
    timeline's FoF casts) and the raid-buff multiplier (`buff_intervals`, default
    off — the form the sweep / refinement use internally).

    There is no GCD/oGCD distinction: FoF and raid buffs amp *all* personal
    damage, so the damage oGCDs (Imperator, Circle of Scorn, Expiacion, Blade of
    Honor, Intervene) are scaled too. `target_fn(t, aid) -> n` supplies the
    per-cast target count (None -> single target, byte-identical).
    """
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier (the
    # in-sim analog of FoF); a no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    fof = _fof_windows(timeline) or None
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), pd.JOB_DATA)
        if base <= 0:
            continue
        m = 1.0
        if fof:
            m *= multiplier_at(t, fof)
        if bi:
            m *= multiplier_at(t, bi)
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (PLD has no pet scalar);
    `coverage_intervals` is unused (FoF is derived inside score_delivered_potency,
    not supplied as a full-coverage overlay). `target_intervals` is the
    multi-target N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    pd.JOB_DATA.tincture_main_stat, pd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=pld_simulator,
    score_timeline=_score_timeline,
    enabler_ids=pd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- PaladinScoringAspect --------------------------------------------------

class PaladinScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a PLD run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. Fight or Flight is derived from the cast timeline, so the only per-pull
    context is the phase-continuation entry combo/proc state (carried Divine Might /
    Atonement chain / mid main-combo), fed into the ceiling via `sim_context` so the
    lenient / timeline sims open as loaded as the player did. PLD has no offensive
    gauge, so there is no gauge to carry — only the combo machine."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    gcd_constant = pld_simulator.GCD_BASE_S
    # Pre-pull ranged Holy Spirit (precast during the run-in, resolving at t~0) is
    # real opener damage — credit the one nearest t=0 to delivered, matching the
    # channel the sim emits in prepull when the sweep takes the pre-cast line.
    prepull_channel_ids = frozenset({pd.HOLY_SPIRIT})

    def prepare(self, client, code: str, fight, actor, report, norm_casts):
        from jobs.paladin.simulator import measure_pld_context
        return measure_pld_context(norm_casts)

    def sim_context(self, ctx):
        # Per-pull effective GCD + carried combo/proc state threaded into the ceiling.
        # None (a flat-2.5 cold start) keeps the ceiling pure data, byte-identical.
        return ctx or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx) -> dict:
        return {
            "sim_context": ctx or None,
            "entryDivineMight": bool(ctx.divine_might) if ctx else False,
            "entryComboStep": ctx.combo_step if ctx else 0,
        }
