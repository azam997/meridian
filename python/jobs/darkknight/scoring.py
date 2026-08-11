"""DRK-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is DRK-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze flow —
comes from `jobs/_core/sim/scoring.py` via `build_scoring` + `ScoringAspectBase`.

DRK's one bespoke damage piece, scored by a single forward pass that **mirrors
`simulator.apply_cast` exactly** (so the beam-prune key matches the final score and
delivered/idealized stay symmetric — the >100% guard holds by construction):

  * **Darkside derived from the timeline casts** — +10% while active; each Edge/Flood
    of Shadow cast extends it +30s capped at 60s, from zero at the pull. A cast is
    amped by the window state set by EARLIER casts (never its own grant). FFLogs does
    not record Darkside at all (no status event, no aura token, not in the
    `multiplier` field — probe-verified), so reconstruction from the cast times is
    the ONLY honest model — and it is exactly symmetric, because the idealized
    timeline is scored by this same function.

Everything else is a pure table lookup: the Living Shadow pet fold and the Salted
Earth tick fold are CONSTANTS on their cast ids (data.py), so there are no DoT
snapshots and no bespoke families — Esteem's own ids never appear in a cast stream
and score 0.0 by construction.

Raid buffs (`buff_intervals`) and the in-sim tincture (`merge_tincture_markers`) are
applied multiplicatively on top — external overlays, not derived from the timeline.

DRK needs no per-pull `sim_context` beyond the (measured-cold, safety-net) entry
Blood: TBN/Dark Arts is MP-net-neutral (data.py header) and Darkside rides the
player's own casts. No guaranteed-crit ability ⇒ no crit pricing.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.entry_gauge import entry_state
from jobs._core.improvements import Improvement
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.darkknight import data as dd
from jobs.darkknight import simulator as drk_simulator


_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)

_DARKSIDE_GRANTS = (dd.EDGE_OF_SHADOW, dd.FLOOD_OF_SHADOW)


def _extend_darkside(darkside_end: float, t: float) -> float:
    """One Edge/Flood cast at `t`: +30s onto the live window, capped at t+60s."""
    return min(t + dd.DARKSIDE_CAP_S,
               max(darkside_end, t) + dd.DARKSIDE_EXTEND_S)


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly via one forward pass (the exact per-cast math
    `simulator.apply_cast` runs incrementally). Each cast's table potency is scaled by:
      - Darkside, derived from EARLIER Edge/Flood casts on this timeline,
      - the raid-buff + in-sim-tincture multiplier (`buff_intervals`) at its time.
    Symmetric on delivered + idealized (the same function scores both)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    casts = sorted(timeline, key=lambda x: x[0])

    darkside_end = float("-inf")
    total = 0.0
    for t, aid in casts:
        base = potency_for(aid, n_of(t, aid), dd.JOB_DATA)
        if base > 0:
            m = dd.DARKSIDE_MULT if darkside_end > t else 1.0
            if bi:
                m *= multiplier_at(t, bi)
            total += base * m
        # Extend Darkside AFTER scoring (a grant never amps its own cast).
        if aid in _DARKSIDE_GRANTS:
            darkside_end = _extend_darkside(darkside_end, t)
    return total


def darkside_stats(timeline: list[tuple[float, int]],
                   duration_s: float) -> dict[str, Any]:
    """Delivered-side Darkside diagnostics: uptime % of the fight span after the
    first grant, the uncovered windows, and the potency lost to casts made while
    it was down. The universal pre-first-Edge opener GCD is excluded (the ceiling
    pays the same structural cost, so it isn't an improvement)."""
    casts = sorted(timeline, key=lambda x: x[0])
    darkside_end = float("-inf")
    first_grant: float | None = None
    lost = 0.0
    # Per-window buckets keyed by the down-window's start: when a cast is
    # counted lost, the current `darkside_end` IS the start of the uncovered
    # window it falls in (a re-grant closes the window at its own cast time;
    # the never-re-granted tail keeps the final `darkside_end` as its start) —
    # so the bucket keys line up with `uncovered` exactly, tail included, and
    # the buckets sum to `lost` by construction.
    lost_by_start: dict[float, float] = {}
    uncovered: list[tuple[float, float]] = []
    for t, aid in casts:
        if first_grant is not None and darkside_end <= t:
            base = potency_for(aid, 1, dd.JOB_DATA)
            if base > 0:
                amp = base * (dd.DARKSIDE_MULT - 1.0)
                lost += amp
                lost_by_start[darkside_end] = (
                    lost_by_start.get(darkside_end, 0.0) + amp)
        if aid in _DARKSIDE_GRANTS:
            if first_grant is None:
                first_grant = t
            elif darkside_end <= t:
                uncovered.append((darkside_end, t))
            darkside_end = _extend_darkside(darkside_end, t)
    if first_grant is None:
        return {"darkside_uptime_pct": 0.0, "darkside_uncovered": [],
                "darkside_uncovered_lost": [],
                "darkside_lost_potency": 0.0}
    span = max(1e-9, duration_s - first_grant)
    down = sum(max(0.0, min(e, duration_s) - s) for s, e in uncovered)
    if darkside_end < duration_s:
        down += duration_s - darkside_end
        uncovered = uncovered + [(darkside_end, duration_s)]
    uncovered_lost = [lost_by_start.get(s, 0.0) for s, _e in uncovered]
    return {
        "darkside_uptime_pct": round(100.0 * max(0.0, span - down) / span, 2),
        "darkside_uncovered": [(round(s, 2), round(e, 2)) for s, e in uncovered],
        "darkside_uncovered_lost": [round(x, 1) for x in uncovered_lost],
        "darkside_lost_potency": round(lost, 1),
    }


def _mmss(s: float) -> str:
    n = int(round(s))
    return f"{n // 60}:{n % 60:02d}"


def improvements_from_darkside(state: dict) -> list[Improvement]:
    """A priced card for the damage lost to Darkside downtime — the 10% amp missed
    on everything cast while it wasn't up (mid-fight drops only; the universal
    opener GCD is excluded in `darkside_stats`). With 2+ down-windows the card
    carries one located child per window (a single window keeps the card a
    directly-jumpable leaf). Zero-priced (no card) at full uptime."""
    lost = float(state.get("darkside_lost_potency", 0.0) or 0.0)
    if lost <= 0.0:
        return []
    uncovered = state.get("darkside_uncovered") or []
    t0 = uncovered[0][0] if uncovered else 0.0
    cov = float(state.get("darkside_uptime_pct", 100.0))
    # Old-shape states (no `darkside_uncovered_lost`) degrade to childless.
    per_window = state.get("darkside_uncovered_lost") or []
    children: list[Improvement] = []
    if len(uncovered) >= 2 and len(per_window) == len(uncovered):
        for (s, e), wl in zip(uncovered, per_window):
            children.append(Improvement(
                kind="darkside", ability_id=dd.EDGE_OF_SHADOW,
                ability_name="Darkside", time_s=float(s),
                lost_potency=float(wl),
                summary=f"{_mmss(s)}–{_mmss(e)}: Darkside down {e - s:.0f}s — "
                        f"spend Edge of Shadow sooner to keep the 10% amp up"))
    return [Improvement(
        kind="darkside", ability_id=dd.EDGE_OF_SHADOW,
        ability_name="Darkside", time_s=t0, lost_potency=lost,
        summary=f"Darkside dropped to {cov:.1f}% uptime — "
                f"the 10% amp was missing from {_mmss(t0)}",
        children=children)]


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (the pet fold rides the
    Living Shadow cast id); `coverage_intervals` is unused (Darkside rides the
    timeline, not an overlay). `target_intervals` is the free-splash N(t)
    schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=drk_simulator,
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


# --- DarkKnightScoringAspect ------------------------------------------------

class _DrkCtx:
    """Per-pull context: the phase-continuation entry state (carried Blood — a
    safety net; measured M12S-P2 pulls open cold) + the delivered-side Darkside
    diagnostics for the improvement card."""
    __slots__ = ("entry", "darkside")

    def __init__(self, entry, darkside):
        self.entry = entry
        self.darkside = darkside


class DarkKnightScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a DRK run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. Darkside is derived from the player's own casts (symmetric with the
    idealized ceiling); TBN/Dark Arts is MP-net-neutral and un-modeled (data.py);
    the one per-pull measured axis is the carried-Blood entry state on
    phase-continuation pulls, seeded onto the ceiling so a loaded opener would be
    matched (preserving the <=100% guard)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: DRK runs the gear-true 2.50 GCD (no haste self-buff);
    # min(2.50, inferred) only speeds the ceiling (monotone-safe).
    gcd_constant = drk_simulator.DRK_GCD_S
    # DRK has a FLAT GCD (no modeled sub-GCD window; probe: 2.496-2.498s flat in
    # and out of burst), so the demonstrated-cadence anchor is valid: a top parse
    # that out-queues the fixed sub-GCD band floor bounds the ceiling's GCD budget
    # at its own demonstrated cadence.
    demonstrated_cadence_anchor = True

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any], norm_casts) -> Any:
        duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        return _DrkCtx(entry_state(norm_casts, dd.JOB_DATA.gauges),
                       darkside_stats(norm_casts, duration_s))

    def sim_context(self, ctx: Any) -> Any:
        return ctx.entry or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        e = ctx.entry
        return {
            "sim_context": e or None,
            "entryGauges": dict(e.gauges) if e else {},
            **ctx.darkside,
        }
