"""GNB-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is GNB-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze flow —
comes from `jobs/_core/sim/scoring.py` via `build_scoring` + `ScoringAspectBase`.

GNB's bespoke damage pieces, all scored by a single forward pass that **mirrors
`simulator.apply_cast` exactly** (so the beam-prune key matches the final score and
delivered/idealized stay symmetric — the >100% guard holds by construction):

  * **No Mercy self-buff derived from the timeline casts** — the +20% window
    `[cast_t, +20s)` for each No Mercy cast; a cast is amped by the window granted by an
    EARLIER cast (never its own). Modeled on the timeline (NOT a flat full-coverage
    overlay — No Mercy is only ~1/3 uptime), so a dropped/late No Mercy or GCDs lost
    under it cost efficiency; perfectly used, the x1.20 cancels in the ratio.
  * **Sonic Break + Bow Shock DoTs** — each scored per cast by time-to-next-cast capped
    at the true 15s (NOT SAM's 30s), snapshotting the multiplier (No Mercy + raid +
    tincture) at cast. Over-refreshing credits less (overcap-safe).

Raid buffs (`buff_intervals`) and the in-sim tincture (`merge_tincture_markers`) are
applied multiplicatively on top — external overlays, not derived from the timeline.

GNB needs no per-pull `sim_context` (its self-buff rides the player's own casts and it
has no proc economy) — the ceiling is pure (duration, downtime, buffs) data, with the
per-player GCD threaded via `gcd_constant`. No guaranteed-crit ability ⇒ no crit pricing.
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.entry_gauge import entry_state
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.gunbreaker import data as gd
from jobs.gunbreaker import simulator as gnb_simulator


_TINCTURE_SPEC = spec_for_job(
    gd.JOB_DATA.tincture_main_stat, gd.JOB_DATA.tincture_role_coeff)


def _dot_potency(snaps: list[tuple[float, float]],
                 timeline: list[tuple[float, int]],
                 duration_s: float, tick_s: float, tick_p: int) -> float:
    """DoT potency summed per application: each credited `min(duration, time-to-next)` of
    ticks x the multiplier snapshotted at its cast (`snaps` carries `(cast_t,
    snapshot_mult)`) — so over-refreshing credits less, never double-counts. The trailing
    application is credited up to the fight end (capped at the DoT duration). The shared
    SAM/DRG DoT model."""
    if not snaps:
        return 0.0
    span_end = max((t for t, _a in timeline), default=0.0) + duration_s
    total = 0.0
    for i, (ct, m) in enumerate(snaps):
        nxt = snaps[i + 1][0] if i + 1 < len(snaps) else span_end
        covered_s = min(duration_s, max(0.0, nxt - ct))
        total += (covered_s / tick_s) * tick_p * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly via one forward pass (the exact per-cast math
    `simulator.apply_cast` runs incrementally). Each cast's table potency is scaled by:
      - the No Mercy self-buff, derived from EARLIER No Mercy casts on this timeline,
      - the raid-buff + in-sim-tincture multiplier (`buff_intervals`) at its time.
    Plus the Sonic Break + Bow Shock DoTs (scored separately so over-refresh is
    overcap-safe). Symmetric on delivered + idealized (the same function scores both)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    casts = sorted(timeline, key=lambda x: x[0])

    no_mercy_end = float("-inf")
    sonic_snaps: list[tuple[float, float]] = []
    bow_snaps: list[tuple[float, float]] = []
    total = 0.0
    for t, aid in casts:
        nm = gd.NO_MERCY_MULT if no_mercy_end > t else 1.0
        base = potency_for(aid, n_of(t, aid), gd.JOB_DATA)
        if base > 0:
            m = nm
            if bi:
                m *= multiplier_at(t, bi)
            total += base * m
        # Record the DoT snapshots (No Mercy + raid + tincture) for their tick credit.
        if aid == gd.SONIC_BREAK:
            snap = nm
            if bi:
                snap *= multiplier_at(t, bi)
            sonic_snaps.append((t, snap))
        elif aid == gd.BOW_SHOCK:
            snap = nm
            if bi:
                snap *= multiplier_at(t, bi)
            bow_snaps.append((t, snap))
        # Open the No Mercy window AFTER scoring (a buff never amps its granting cast).
        if aid == gd.NO_MERCY:
            no_mercy_end = t + gd.NO_MERCY_DURATION_S

    total += _dot_potency(sonic_snaps, timeline, gd.SONIC_BREAK_DOT_DURATION_S,
                          gd.SONIC_BREAK_DOT_TICK_S, gd.SONIC_BREAK_DOT_TICK_P)
    total += _dot_potency(bow_snaps, timeline, gd.BOW_SHOCK_DOT_DURATION_S,
                          gd.BOW_SHOCK_DOT_TICK_S, gd.BOW_SHOCK_DOT_TICK_P)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (GNB has no pet scalar);
    `coverage_intervals` is unused (No Mercy rides the timeline, not an overlay).
    `target_intervals` is the free-splash N(t) schedule (None -> single target,
    byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=gnb_simulator,
    score_timeline=_score_timeline,
    enabler_ids=gd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- GNBScoringAspect ------------------------------------------------------

class _GnbCtx:
    """Per-pull context: the phase-continuation entry state (carried cartridges out of
    M12S-P1). GNB opens at t=0 (tank, already on the boss) so there is no opener
    override — only the carried cartridges matter."""
    __slots__ = ("entry",)

    def __init__(self, entry):
        self.entry = entry


class GunbreakerScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a GNB run. Emits the same
    state-key shape as the other scorers so the dashboard headline lights up unchanged.
    Its No Mercy self-buff + DoTs are derived from the player's own casts (symmetric
    with the idealized ceiling); the one per-pull measured axis is the carried-cartridge
    entry state on phase-continuation pulls (M12S-P2), seeded onto the ceiling so a
    loaded opener is matched (preserving the <=100% guard)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: GNB runs the gear-true 2.50 GCD (no haste self-buff);
    # min(2.50, inferred) only speeds the ceiling (monotone-safe).
    gcd_constant = gnb_simulator.GNB_GCD_S
    # GNB has a FLAT GCD (no modeled sub-GCD window), so the demonstrated-cadence anchor is
    # valid: top M12S-P1 parses sustain ~2.46s (162 GCDs) via pure server-tick tightness,
    # tighter than the fixed 2.47 band floor — this bounds them without inflating anything.
    demonstrated_cadence_anchor = True

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any], norm_casts) -> Any:
        # Deepest-cartridge-deficit over the opener = the cartridges carried into the
        # pull. 0 (None) on a cold start -> the ceiling stays pure data, byte-identical.
        return _GnbCtx(entry_state(norm_casts, gd.JOB_DATA.gauges))

    def sim_context(self, ctx: Any) -> Any:
        return ctx.entry or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        e = ctx.entry
        return {
            "sim_context": e or None,
            "entryGauges": dict(e.gauges) if e else {},
        }
