"""RPR-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is RPR-specific; everything
around it — the LRU-cached perfect-sim ceiling, the enabler valuation, and the
`Scoring` aspect's analyze flow — comes from `jobs/_core/sim/scoring.py` via
`build_scoring` + `ScoringAspectBase`.

RPR is simpler than MCH on the damage side — no Wildfire payload, no pet — but
adds **Death's Design**: a 10% personal-damage amp the player maintains on the
boss. It's modeled as a multiplier-interval set (`dd_intervals`), exactly like
raid buffs: the idealized side assumes full coverage (the sim keeps Shadow of
Death up), while the delivered side is scaled by the *measured* coverage from
the boss's debuff events (see jobs/reaper/death_design.py). So a dropped/late
Death's Design costs efficiency; at 100% uptime the x1.10 cancels in the ratio.
That full-coverage overlay is the shared scaffolding's `coverage_intervals` hook.
"""
from __future__ import annotations

from typing import Any

from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.buff_windows import multiplier_at
from jobs._core.entry_gauge import entry_state
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.reaper import data as rd
from jobs.reaper import simulator as rpr_simulator


# Death's Design covers the whole fight on the idealized side. Padded start so
# a t~0 opener cast is inside the window.
def _full_dd_intervals(duration_s: float) -> list[tuple[float, float, float]]:
    return [(-10.0, duration_s + 1.0, rd.DEATHS_DESIGN_MULT)]


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    dd_intervals: list[tuple[float, float, float]] | None = None,
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Every cast's table potency is scaled by
    the Death's Design multiplier active at its time (`dd_intervals`) and the
    raid-buff multiplier (`buff_intervals`). Both default to off (buff-agnostic,
    DD-agnostic) — the form the sweep / refinement use internally, where DD is a
    constant x1.10 on every candidate and so doesn't change the argmax.

    Unlike MCH there is no GCD/oGCD distinction here: Death's Design and raid
    buffs amp *all* personal damage, so oGCDs (Blood Stalk, Gluttony, Lemure's
    Slice, Sacrificium) are scaled too.
    """
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    dd = dd_intervals or None
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), rd.JOB_DATA)
        if base <= 0:
            continue
        m = 1.0
        if dd:
            m *= multiplier_at(t, dd)
        if bi:
            m *= multiplier_at(t, bi)
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (RPR has no pet scalar);
    `coverage_intervals` is the Death's Design overlay. `target_intervals` is the
    multi-target N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, dd_intervals=coverage_intervals, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    rd.JOB_DATA.tincture_main_stat, rd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=rpr_simulator,
    score_timeline=_score_timeline,
    enabler_ids=rd.ENABLER_IDS,
    coverage_intervals=_full_dd_intervals,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- Phase-continuation entry state (P1->P2 carryover) ---------------------

def _shroud_spend_hook(name: str, aid: int, scratch: dict):
    """Shroud-spend override for the shared `measure_entry_gauge`: Plentiful Harvest
    arms a FREE (Ideal Host) Enshroud, so the next Enshroud spends 0 shroud, not 50.
    (Soul has no conditional spends -> the hook returns None there = flat spender.)"""
    if name != "shroud":
        return None
    if aid == rd.PLENTIFUL_HARVEST:
        scratch["ideal_host"] = True
        return None
    if aid == rd.ENSHROUD and scratch.get("ideal_host"):
        scratch["ideal_host"] = False
        return 0
    return None


# --- RPRScoringAspect ------------------------------------------------------

class _RprCtx:
    """Per-pull context: measured Death's Design coverage (delivered multiplier) plus
    the phase-continuation entry state (carried soul/shroud + opener start)."""
    __slots__ = ("dd_intervals", "entry")

    def __init__(self, dd_intervals, entry):
        self.dd_intervals = dd_intervals
        self.entry = entry


class RPRScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for an RPR run. Emits the
    same state-key shape as MCH's scorer so the dashboard headline lights up
    unchanged. Per-pull context = the MEASURED Death's Design coverage (delivered
    multiplier) plus the phase-continuation entry state (carried gauge + opener
    start, fed into the ceiling via sim_context so the lenient / timeline sims open
    as loaded as the player did)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    gcd_constant = rpr_simulator.GCD_BASE_S
    # Pre-pull ranged Harpe (precast during the run-in, resolving at t~0) is real
    # opener damage — credit the one nearest t=0 to delivered, matching the Harpe
    # the sim emits in prepull when the sweep takes the pre-Harpe line.
    prepull_channel_ids = frozenset({rd.HARPE})

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        from jobs.reaper.death_design import measured_dd_intervals
        dd = measured_dd_intervals(client, code, report, fight, actor)
        entry = entry_state(
            norm_casts, rd.JOB_DATA.gauges, spend_hook=_shroud_spend_hook,
            prepull_channel_ids=self.prepull_channel_ids,
            default_engage_s=rd.JOB_DATA.role_policy.engage_delay_s)
        return _RprCtx(dd, entry)

    def sim_context(self, ctx: Any) -> Any:
        # Phase-continuation entry state threaded into the ceiling. None (cold start)
        # keeps the ceiling pure (duration, downtime, buffs) data, byte-identical.
        return ctx.entry or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(
            in_fight_casts, dd_intervals=ctx.dd_intervals, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        e = ctx.entry
        return {
            "sim_context": e or None,
            "entryGauges": dict(e.gauges) if e else {},
            "openerStartS": e.opener_start_s if e else None,
        }
