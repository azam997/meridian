"""DRG-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is DRG-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

DRG's bespoke damage pieces, all scored by a single forward pass that **mirrors
`simulator.apply_cast` exactly** (so the beam-prune key matches the final score and
delivered/idealized stay symmetric — the >100% guard holds by construction):

  * **Damage self-buffs derived from the timeline casts** — Power Surge (+10%, from
    Spiral Blow), Lance Charge (+10%), Life of the Dragon (+15%, from Geirskogul).
    Each is the window `[grant_cast_t, +duration)`; a cast is amped by the buffs
    granted by EARLIER casts (never its own). Modeled on the timeline (NOT a flat
    full-coverage overlay) so the branch fork sees the DoT combo's true Power Surge
    upkeep value — a raw-heavy line that drops Power Surge correctly loses it.
  * **Life Surge** — a guaranteed crit on the next weaponskill, priced x the crit-only
    multiplier (the SAM always-crit / MCH Reassemble pattern).
  * **Chaotic Spring DoT** — scored per cast by time-to-next-cast (capped at 24s) so an
    early refresh credits less (overcap-safe), snapshotting the buff multiplier (self
    + raid + tincture) at cast time.

Raid buffs (`buff_intervals`) and the in-sim tincture (`merge_tincture_markers`) are
applied multiplicatively on top — external overlays, not derived from the timeline.

DRG needs no per-pull `sim_context` (its self-buffs ride the player's own casts and
its Focus entry-gauge is negligible) — the ceiling is pure (duration, downtime,
buffs) data, with the per-player GCD threaded via `gcd_constant`.
"""
from __future__ import annotations

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.dragoon import data as dd
from jobs.dragoon import simulator as drg_simulator


_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)


def _chaotic_spring_dot_potency(
    snaps: list[tuple[float, float]],
    timeline: list[tuple[float, int]],
) -> float:
    """Chaotic Spring DoT potency, summed per application. Each is credited
    `min(24s, time-to-next-Chaotic-Spring)` of ticks x the multiplier snapshotted at
    its cast (`snaps` carries `(cast_t, snapshot_mult)`) — so over-refreshing credits
    less, never double-counts. The trailing application is credited up to the fight
    end (capped at the 24s duration)."""
    if not snaps:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) \
        + dd.CHAOTIC_SPRING_DOT_DURATION_S
    total = 0.0
    for i, (ct, m) in enumerate(snaps):
        nxt = snaps[i + 1][0] if i + 1 < len(snaps) else span_end
        covered_s = min(dd.CHAOTIC_SPRING_DOT_DURATION_S, max(0.0, nxt - ct))
        total += (covered_s / dd.CHAOTIC_SPRING_DOT_TICK_S
                  * dd.CHAOTIC_SPRING_DOT_TICK_P * m)
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly via one forward pass (the exact per-cast math
    `simulator.apply_cast` runs incrementally). Each cast's table potency is scaled by:
      - the active damage self-buffs (Power Surge / Lance Charge / Life of the Dragon),
        derived from EARLIER casts on this timeline,
      - the crit-only multiplier when a Life Surge armed the cast,
      - the raid-buff + in-sim-tincture multiplier (`buff_intervals`) at its time.
    Plus the Chaotic Spring DoT (scored separately so over-refresh is overcap-safe).

    There is no GCD/oGCD distinction: the self-buffs and raid buffs amp *all* personal
    damage. Symmetric on delivered + idealized (the same function scores both)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    casts = sorted(timeline, key=lambda x: x[0])

    power_surge_end = lance_charge_end = lotd_end = float("-inf")
    life_surge_armed = False
    chaotic_snaps: list[tuple[float, float]] = []
    total = 0.0
    for t, aid in casts:
        # Self-buff multiplier from buffs granted by EARLIER casts.
        sb = 1.0
        if power_surge_end > t:
            sb *= dd.POWER_SURGE_MULT
        if lance_charge_end > t:
            sb *= dd.LANCE_CHARGE_MULT
        if lotd_end > t:
            sb *= dd.LOTD_MULT
        base = potency_for(aid, n_of(t, aid), dd.JOB_DATA)
        if base > 0:
            m = sb
            if life_surge_armed and aid in dd.GCD_WEAPONSKILLS:
                m *= dd.GUARANTEED_CRIT_MULT
                life_surge_armed = False
            if bi:
                m *= multiplier_at(t, bi)
            total += base * m
        # Record the Chaotic Spring snapshot (self + raid + tincture) for its DoT.
        if aid == dd.CHAOTIC_SPRING:
            snap = sb
            if bi:
                snap *= multiplier_at(t, bi)
            chaotic_snaps.append((t, snap))
        # Update self-buff windows AFTER scoring (a buff never amps its granting cast).
        if aid in (dd.SPIRAL_BLOW, dd.SONIC_THRUST):
            power_surge_end = t + dd.POWER_SURGE_DURATION_S
        elif aid == dd.LANCE_CHARGE:
            lance_charge_end = t + dd.LANCE_CHARGE_DURATION_S
        elif aid == dd.GEIRSKOGUL:
            lotd_end = t + dd.LOTD_DURATION_S
        elif aid == dd.LIFE_SURGE:
            life_surge_armed = True

    total += _chaotic_spring_dot_potency(chaotic_snaps, timeline)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (DRG has no pet scalar);
    `coverage_intervals` is unused (DRG's self-buffs ride the timeline, not an
    overlay). `target_intervals` is the free-splash N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=drg_simulator,
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


# --- DRGScoringAspect ------------------------------------------------------

class DRGScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a DRG run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up
    unchanged. DRG needs no per-pull measured context — its self-buffs are derived
    from the player's own casts (symmetric with the idealized ceiling), so the
    ceiling is pure (duration, downtime, buffs) data."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: DRG runs the gear-true 2.50 GCD (no haste self-buff);
    # min(2.50, inferred) only speeds the ceiling (monotone-safe).
    gcd_constant = drg_simulator.DRG_GCD_S

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)
