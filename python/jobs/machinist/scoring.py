"""MCH-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* (`score_delivered_potency`) is MCH-specific — it bakes in
Reassemble's crit-DH buff, Wildfire's 6-hit payload, and the linear battery ->
potency pet conversion. Everything around it — the LRU-cached perfect-sim
ceiling, the enabler valuation, and the `Scoring` aspect's analyze flow — comes
from `jobs/_core/sim/scoring.py` via `build_scoring` + `ScoringAspectBase`.

The scoring function applies identical math to idealized, reference, and user
runs so the *deltas* are correct even when absolute values are estimates.
"""
from __future__ import annotations

from typing import Any

from jobs._core import ability_metadata
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.buff_windows import multiplier_at
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.machinist import data as md
from jobs.machinist import simulator as mch_simulator


# Hypercharge / Overheated state: 5 Blazing Shots at 1.5s recast = 7.5s.
# Add a tiny buffer to cover the last Blazing Shot's cast resolution.
HYPERCHARGE_WINDOW_S: float = 8.0
_HYPERCHARGE_ID: int = 17209
_REASSEMBLE_ID: int = 2876
_WILDFIRE_ID: int = 2878
_FMF_ID: int = 36982   # Full Metal Field — innately a guaranteed crit-DH
_FLAMETHROWER_ID: int = 7418   # channeled; only ever the niche downtime-edge tick


def compute_hypercharge_intervals(
    norm_casts: list[tuple[float, int]],
) -> list[tuple[float, float]]:
    """Each in-fight Hypercharge cast spawns a HYPERCHARGE_WINDOW_S interval
    of Blazing Shot uptime. These intervals are MCH-specific: ClippingAspect
    excludes them so the 1.5s Blazing Shot recast doesn't read as a clip.
    """
    return [
        (t, t + HYPERCHARGE_WINDOW_S)
        for t, aid in norm_casts
        if aid == _HYPERCHARGE_ID and t >= 0
    ]


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    queen_battery_spent: int = 0,
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly. Applied identically to idealized,
    reference, and user runs so the *deltas* are correct even when absolute
    values are estimates.

    Components:
      - Direct potency for each cast (from POTENCIES)
      - Reassemble bonus: next weaponskill within 5s gets x GUARANTEED_CRIT_DH_MULT
      - Full Metal Field: innately guaranteed crit-DH, so always x that mult
        (and a Reassemble landing on it is wasted, not double-counted)
      - Wildfire payload: hits x 240 (capped at 6) per Wildfire cast
      - Queen pet potency: battery x 46p

    `buff_intervals` is an optional `(start, end, multiplier)` timeline of
    raid-buff windows. When supplied, every cast is additionally scaled by the
    buff multiplier active at its time — this is what lets the optimizer prefer
    stacking burst into windows. Queen is credited per-summon at the multiplier
    active when she was summoned. When omitted, scoring is buff-agnostic and
    Queen uses the `queen_battery_spent` total (the original behavior).
    """
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier; a
    # no-op for the player's delivered timeline (no marker). Applied before `bi` so it
    # also scales the Wildfire snapshot + per-summon Queen below.
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    crit_dh = md.GUARANTEED_CRIT_DH_MULT
    bi = buff_intervals or None
    # Per-cast target count: ceiling reads the N(t) schedule, delivered the
    # measured hits; `None` -> single target (byte-identical). Applies to the
    # Pass-1 direct potency only; Wildfire payload + Queen stay ST for now.
    n_of = target_fn or (lambda _t, _a: 1)

    def bmult(t: float) -> float:
        return multiplier_at(t, bi) if bi else 1.0

    sorted_timeline = sorted(timeline, key=lambda x: x[0])
    total = 0.0

    # Pass 1: direct potency + guaranteed crit-DH multipliers.
    # `t <= window_end` (not `<`) so a buff applied at t=-5 still covers
    # the t=0 Air Anchor — the buff duration is 5s inclusive.
    reassemble_window_end = -float("inf")
    for t, aid in sorted_timeline:
        if aid == _REASSEMBLE_ID:
            reassemble_window_end = t + 5.0
            continue
        if aid == _WILDFIRE_ID:
            continue
        base = potency_for(aid, n_of(t, aid), md.JOB_DATA)
        if base <= 0:
            continue
        meta = ability_metadata.get_metadata(aid)
        if meta is None:
            continue
        # Only weaponskills (GCDs) consume / benefit from Reassemble; oGCDs
        # like Double Check / Checkmate do not.
        reassemble_active = (not meta.is_ogcd and t <= reassemble_window_end)
        if aid == _FMF_ID:
            # Already a guaranteed crit-DH on its own. Reassemble adds nothing
            # but is still consumed (wasted) if one happened to be up.
            mult = crit_dh
            if reassemble_active:
                reassemble_window_end = -float("inf")
        elif reassemble_active:
            mult = crit_dh
            reassemble_window_end = -float("inf")    # consumed
        else:
            mult = 1.0
        total += base * mult * bmult(t)

    # Pass 2: Wildfire payload — 240p per covered weaponskill (capped at 6),
    # scaled by the raid buff active WHEN WILDFIRE WAS CAST. Damage buffs
    # snapshot at cast: a WF pressed inside a buff window has its WHOLE payload
    # buffed even though it detonates ~10s later. Pressed outside the window,
    # the payload gets nothing, no matter where the hits land.
    wildfire_times = [t for t, aid in sorted_timeline if aid == _WILDFIRE_ID]
    for wf_t in wildfire_times:
        hits = 0
        for t, aid in sorted_timeline:
            if t <= wf_t:
                continue
            if t > wf_t + 10.0:
                break
            if aid == _WILDFIRE_ID or aid == _FLAMETHROWER_ID:
                # Flamethrower's squeezed tick lands during downtime (boss gone),
                # so it never counts as a Wildfire-buffed weaponskill hit.
                continue
            meta = ability_metadata.get_metadata(aid)
            if meta and not meta.is_ogcd:
                hits += 1
        total += min(hits, 6) * 240.0 * bmult(wf_t)

    # Queen pet potency. The buff-aware path credits each summon at the
    # multiplier active when she was summoned, with the per-summon battery
    # SCALED so the total equals `queen_battery_spent` — the deliverability-
    # discounted total every caller passes (the sim's aux on the ceiling side,
    # the player's discounted total on the delivered side). A cut-off Queen can
    # therefore no longer inflate the buff-aware score with battery she never
    # delivered (the old raw-battery phantom). The uniform scale redistributes
    # non-uniform per-summon fractions across the summons' buff multipliers —
    # a second-order error; the battery TOTAL is exact.
    if bi:
        summons = _queen_summons(sorted_timeline)
        raw_total = sum(b for _t, b in summons)
        scale = queen_battery_spent / raw_total if raw_total > 0 else 1.0
        for summon_t, battery in summons:
            total += battery * scale * md.BATTERY_VALUE_P_PER_UNIT * bmult(summon_t)
    else:
        total += queen_battery_spent * md.BATTERY_VALUE_P_PER_UNIT

    return total


def _queen_summons(
    sorted_casts: list[tuple[float, int]],
    fight_duration_s: float | None = None,
    downtime_windows: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """(summon_time_s, battery_at_summon) for each in-fight Queen cast, walked
    from the battery-generator stream (casts must be time-sorted). With
    `fight_duration_s` + `downtime_windows` each summon's battery is scaled by
    `queen_deliverable_fraction` — the same discount the idealized sim applies;
    without them, raw battery."""
    deliverability = fight_duration_s is not None and downtime_windows is not None
    battery = 0
    out: list[tuple[float, float]] = []
    for t, aid in sorted_casts:
        if aid in md.BATTERY_GENERATORS:
            battery = min(md.BATTERY_CAP, battery + md.BATTERY_GENERATORS[aid])
        elif aid == _QUEEN_ABILITY_ID:
            if t >= 0:
                frac = (mch_simulator.queen_deliverable_fraction(
                            t, fight_duration_s, downtime_windows)
                        if deliverability else 1.0)
                out.append((t, battery * frac))
            battery = 0
    return out


# --- Queen battery tracking ------------------------------------------------

_QUEEN_ABILITY_ID: int = 16501


def compute_queen_battery_spent(
    norm_casts: list[tuple[float, int]],
    fight_duration_s: float | None = None,
    downtime_windows: list[tuple[float, float]] | None = None,
) -> float:
    """Battery spent across all in-fight Queen casts. Battery generators feed a
    counter; each Queen cast drains it to zero and contributes its battery-at-cast
    value (when in-fight) to the total. Used by `score_delivered_potency` for
    deterministic pet-damage scoring.

    With `fight_duration_s` + `downtime_windows`, each summon's battery is scaled
    by `queen_deliverable_fraction` — a Queen cut off by a boss-untargetable
    window or the kill is credited only the burst she could actually land, the
    same discount the idealized sim applies. Without them, the raw total (every
    summon at full battery) — the legacy form used for the display-only
    `queen_battery_spent` state key."""
    return sum(b for _t, b in _queen_summons(
        norm_casts, fight_duration_s, downtime_windows))


# --- Scoring scaffolding (cached ceiling, enabler valuation) ---------------

# Enablers whose value is throughput / payload, not standalone direct damage,
# so it can't be read off a potency table — it's the marginal contribution the
# *whole rotation* loses without them. Derived from the sim. Reassemble is
# intentionally excluded: its value is per-cast crit-DH quality, owned by the
# ReassembleAspect, and the picker doesn't honor forbidden_windows for it.
_ENABLER_IDS_FOR_VALUATION: tuple[int, ...] = (
    mch_simulator.WILDFIRE,
    mch_simulator.HYPERCHARGE,
    mch_simulator.BARREL_STABILIZER,
)


def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is Queen battery; MCH has no
    job-wide `coverage_intervals` overlay (that's RPR's Death's Design).
    `target_intervals` is the multi-target N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, aux, buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


# Tincture spec for the idealized placement sweep + the delivered-side credit
# (jobs._core.tincture). Resolved from JobData (None ⇒ no tincture).
_TINCTURE_SPEC = spec_for_job(
    md.JOB_DATA.tincture_main_stat, md.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=mch_simulator,
    score_timeline=_score_timeline,
    enabler_ids=_ENABLER_IDS_FOR_VALUATION,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- MCHScoringAspect ------------------------------------------------------

class MCHScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for an MCH run. Hidden
    from the per-aspect UI — exists so the sidecar can read scalars off
    `mr.aspects['Scoring'].state`. The per-pull context is the total Queen
    battery spent (deterministic pet-damage scoring)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: infer the effective GCD and feed min(constant, inferred)
    # to the ceiling (faster gear only; the analyze flow handles it). 2.50 = the tier
    # constant (true BiS); a genuinely faster-SkS MCH gets a tighter ceiling.
    gcd_constant = mch_simulator.GCD_BASE_S

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        # Score against the DELIVERABLE Queen battery (cut-off summons discounted
        # exactly as the idealized sim discounts them, so the ceiling and the
        # delivered side stay symmetric and efficiency can't exceed 100%). Keep
        # the raw total too — it's the display-only `queen_battery_spent` key.
        from jobs._core.downtime import read_downtime_from_report
        fight_duration_s = (fight["endTime"] - fight["startTime"]) / 1000.0
        downtime_windows, _src = read_downtime_from_report(
            report, norm_casts, fight_duration_s)
        raw = compute_queen_battery_spent(norm_casts)
        deliverable = compute_queen_battery_spent(
            norm_casts, fight_duration_s, downtime_windows)
        return (raw, deliverable)

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        _raw, deliverable = ctx
        return score_delivered_potency(in_fight_casts, deliverable, buff_intervals)

    def extra_state(self, ctx) -> dict:
        raw, _deliverable = ctx
        return {"queen_battery_spent": raw}
