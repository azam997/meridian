"""DNC-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is DNC-specific; everything around
it — the LRU-cached perfect-sim ceiling, the enabler valuation, and the `Scoring`
aspect's analyze flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring`
+ `ScoringAspectBase`.

DNC's bespoke damage piece is its **three self-buffs**, all derived from the
timeline's own casts and applied symmetrically (the PLD Fight-or-Flight pattern),
so a dropped/late buff costs efficiency while a perfectly-used one cancels in the
ratio (preserving the efficiency ≤ 100% guard):

  * **Standard Finish** — a maintained ~5% self-buff (refreshed every Standard
    Step). Modeled per-cast as a 60s window; back-to-back casts overlap into ~full
    coverage on the ceiling, real coverage on the delivered side. (Unlike WAR's
    Surging Tempest this needs no measured-coverage fetch: deriving it from the
    STANDARD_FINISH casts the sim/player both emit is already symmetric.)
  * **Technical Finish** — a ~5% 20s party window.
  * **Devilment** — a crit/DH 20s burst window, priced as a calibrated effective
    multiplier (the MCH Reassemble / WAR guaranteed-crit-DH pattern).

The three stack multiplicatively (Standard Finish is always up; Technical Finish
+ Devilment land together in the 2-min burst), so they're folded through
`multiplier_intervals` (which collapses overlapping windows into product
segments) alongside the raid buffs + the in-sim tincture. `coverage_intervals` is
`None`. The per-pull `sim_context` is the player's measured proc/feather/esprit
budgets, threaded into the ceiling so below-average luck never costs efficiency.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import BuffWindow, multiplier_at, multiplier_intervals
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.dancer import data as dd
from jobs.dancer import simulator as dnc_simulator
from jobs.dancer.simulator import DancerCtx


def _self_buff_windows(timeline: list[tuple[float, int]]) -> list[BuffWindow]:
    """DNC's three self-buffs, derived from their casts in the timeline. Used
    symmetrically on delivered + idealized (both carry their own casts)."""
    out: list[BuffWindow] = []
    for t, aid in timeline:
        if aid in (dd.STANDARD_FINISH, dd.FINISHING_MOVE):
            # Finishing Move grants the same Standard Finish self-buff as a finished
            # Standard Step dance.
            out.append(BuffWindow(t, t + dd.STANDARD_FINISH_DURATION_S,
                                  dd.STANDARD_FINISH_MULT, "Standard Finish"))
        elif aid == dd.TECHNICAL_FINISH:
            out.append(BuffWindow(t, t + dd.TECHNICAL_FINISH_DURATION_S,
                                  dd.TECHNICAL_FINISH_MULT, "Technical Finish"))
        elif aid == dd.DEVILMENT:
            out.append(BuffWindow(t, t + dd.DEVILMENT_DURATION_S,
                                  dd.DEVILMENT_MULT, "Devilment"))
    return out


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency, scaled by the
    product of every buff active at its time — DNC's three self-buffs (derived from
    this same timeline's casts), the raid buffs (`buff_intervals`, default off — the
    form the sweep / refinement use internally) and the in-sim tincture. There is no
    GCD/oGCD distinction: the self-buffs amp all personal damage, so the oGCD Fan
    Dances are scaled too. `target_fn(t, aid) -> n` supplies the per-cast target
    count (None -> single target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast overlay; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    raid = [BuffWindow(s, e, m, "raid") for s, e, m in (buff_intervals or [])]
    combined = multiplier_intervals(raid + _self_buff_windows(timeline)) or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), dd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, combined) if combined else 1.0
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (DNC has no pet scalar);
    `coverage_intervals` is None (the self-buffs are derived inside
    score_delivered_potency, not supplied as a full-coverage overlay).
    `target_intervals` is the multi-target N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=dnc_simulator,
    score_timeline=_score_timeline,
    enabler_ids=dd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- DancerScoringAspect ---------------------------------------------------

def _measure_ctx(norm_casts) -> DancerCtx:
    """The player's measured RNG/external-resource counts — the budgets the
    idealized ceiling spends so below-average luck never costs efficiency:
      * procs  = Reverse Cascade + Fountainfall (each needs a Silken proc),
      * feathers = Fan Dance (each spends a Fourfold Feather),
      * sabers = Saber Dance + Dance of the Dawn (each spends 50 Esprit)."""
    proc = sum(1 for t, aid in norm_casts
               if t >= 0 and aid in (dd.REVERSE_CASCADE, dd.FOUNTAINFALL))
    feather = sum(1 for t, aid in norm_casts
                  if t >= 0 and aid == dd.FAN_DANCE)
    saber = sum(1 for t, aid in norm_casts
                if t >= 0 and aid in (dd.SABER_DANCE, dd.DANCE_OF_THE_DAWN))
    return DancerCtx(proc_budget=proc, feather_budget=feather, saber_budget=saber)


class DancerScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a DNC run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. The per-pull context is the player's measured proc/feather/esprit
    budgets, fed into the idealized ceiling via `sim_context` and stashed on state
    so the sidecar's lenient / timeline sims spend the same counts."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    gcd_constant = dnc_simulator.GCD_BASE_S

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        return _measure_ctx(norm_casts)

    def sim_context(self, ctx: Any) -> Any:
        return ctx  # the DancerCtx (measured budgets)

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        # `sim_context` is read by the sidecar's `you`-based sim calls (lenient
        # ceiling, timeline lanes) so they spend the same budgets as the strict
        # ceiling. The scalars are the human-facing aliases.
        return {
            "sim_context": ctx,
            "procBudget": ctx.proc_budget,
            "featherBudget": ctx.feather_budget,
            "saberBudget": ctx.saber_budget,
        }
