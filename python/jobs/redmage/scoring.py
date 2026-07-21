"""RDM-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is RDM-specific; everything
around it — the LRU-cached perfect-sim ceiling, the enabler valuation, and the
`Scoring` aspect's analyze flow — comes from `jobs/_core/sim/scoring.py` via
`build_scoring` + `ScoringAspectBase`.

RDM is the simplest job on the damage side so far: no Wildfire payload, no pet,
no maintained personal amp like RPR's Death's Design — every cast just scores its
table potency, scaled only by raid buffs. So `coverage_intervals` is `None`
(no job-wide overlay).

The one RDM-specific wrinkle is the **proc budget**: the player's measured
Verfire+Verstone count is threaded into the idealized ceiling as `sim_context`
so the perfect sim spends the *same* number of procs. Procs are filler-tier
(~20p over the Jolt III they replace), so this barely moves the ceiling — its
purpose is fairness: below-average proc luck never costs efficiency, only proc
*misuse* does (surfaced separately by the Phase 3 ProcsAspect).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.redmage import data as rd
from jobs.redmage import simulator as rdm_simulator


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency, scaled by the
    raid-buff multiplier active at its time (`buff_intervals`). Defaults to off
    (buff-agnostic) — the form the sweep / refinement use internally. RDM has no
    GCD/oGCD distinction in scoring and no job-wide amp, so every entry in
    POTENCIES is scored the same way. `target_fn(t, aid) -> n` supplies the
    per-cast target count (ceiling: N(t) schedule; delivered: measured hits);
    `None` -> single target, byte-identical."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), rd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (RDM has no pet scalar);
    `coverage_intervals` is None (no job-wide overlay). `target_intervals` is the
    multi-target N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    rd.JOB_DATA.tincture_main_stat, rd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=rdm_simulator,
    score_timeline=_score_timeline,
    enabler_ids=rd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- RDMScoringAspect ------------------------------------------------------

def _measure_proc_budget(norm_casts) -> int:
    """The player's in-fight Verfire + Verstone count — each can only be cast by
    consuming a proc, so this is exactly the procs they got and used."""
    return sum(1 for t, aid in norm_casts
               if t >= 0 and aid in (rd.VERFIRE, rd.VERSTONE))


class RDMScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for an RDM run. Emits the
    same state-key shape as MCH/RPR so the dashboard headline lights up
    unchanged. The per-pull context is the player's measured proc budget, fed
    into the idealized ceiling via `sim_context` and stashed on state so the
    sidecar's lenient / timeline sims use the same proc count."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Spell Speed: casters vary far more than ranged, so this is where the
    # inference matters most. min(2.5, inferred) tightens the ceiling for a fast-SpS
    # RDM (SpS scales the cast times too — handled in the model).
    gcd_constant = rdm_simulator.GCD_BASE_S
    # The pre-pull 440 (Verthunder/Veraero III precast during the countdown,
    # resolving at t=0) is real opener damage — credit the one nearest t=0 to
    # delivered, matching the channel the sim emits in prepull.
    prepull_channel_ids = frozenset({rd.VERTHUNDER_III, rd.VERAERO_III})

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        return _measure_proc_budget(norm_casts)

    def sim_context(self, ctx: Any) -> Any:
        return ctx  # the proc budget (int)

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        # `sim_context` is read by the sidecar's `you`-based sim.simulate calls
        # (lenient ceiling, timeline lanes) so they spend the same proc count as
        # the strict ceiling. `proc_budget` is the human-facing alias.
        return {"sim_context": ctx, "proc_budget": ctx}
