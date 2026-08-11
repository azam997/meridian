"""NIN-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is NIN-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

NIN's bespoke damage pieces, all scored by a single forward pass that **mirrors
`simulator.apply_cast` exactly** (so the beam-prune key matches the final score
and delivered/idealized stay symmetric — the >100% guard holds by construction):

  * **Kunai's Bane derived from the timeline casts** — the +10% NINJA-ONLY window
    `[cast_t, +15s)` per KB cast (the wiki tooltip: "Increases damage YOU deal
    target by 10%"); a cast is amped by a window opened by an EARLIER cast, never
    its own. Dokumori's +5% is the PARTY buff — it rides the job-agnostic
    raid-buff overlay (`buff_intervals`), never this pass (double-count hazard).
  * **Kassatsu** — x1.30 on the ninjutsu that consumes it (Hyosho Ranryu in
    practice; any ninjutsu id qualifies, TCJ steps do not).
  * **Kazematoi** — +100 on Aeolian Edge while stacked (Armor Crush grants 2,
    Aeolian consumes 1), tracked forward from the casts.
  * **Meisui** — +150 on the next Bhavacakra / Zesho Meppo.
  * **Bunshin** — 5 shadow mirrors: +160p per mirrored weaponskill after a
    Bunshin cast (pet damage modeled off the PLAYER's cast timeline — the pet's
    own hits log under separate ids and are never in `norm_casts`, so crediting
    the mirror here is symmetric with the sim).

No DoT (Doton is AoE-only, unmodeled), no guaranteed crits, no RNG proc budget —
the per-pull measured axis is the phase-continuation entry state (carried Ninki /
Kazematoi + opener start on M12S-P2-style continuation logs).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.entry_gauge import (
    EntryState,
    measure_entry_gauge,
    measure_opener_start,
)
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.ninja import data as nd
from jobs.ninja import simulator as nin_simulator


_TINCTURE_SPEC = spec_for_job(
    nd.JOB_DATA.tincture_main_stat, nd.JOB_DATA.tincture_role_coeff)

# The un-modeled Ninki source (the Bunshin shadow's +5 per mirrored hit is pet
# damage the cast stream can't see) would read later spends as phantom carried
# gauge, so the entry measurement is capped to the opening seconds — long enough
# to catch a continuation's loaded spenders, short enough that a cold pull's
# first Bunshin mirrors haven't distorted the balance (the SAM Meditate pattern).
_ENTRY_WINDOW_S = 15.0
# Cold pulls open with the pre-pull-mudra Suiton at ~0.45-0.7s; a continuation
# (already mid-combat, no countdown) opens earlier. Anything at/after this is a
# normal cold opener -> no override, byte-identical.
_ENTRY_ENGAGE_S = 0.4


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly via one forward pass (the exact per-cast
    math `simulator.apply_cast` runs incrementally). Each cast's table potency
    (plus the state-derived Kazematoi / Meisui bonuses) is scaled by:
      - the Kunai's Bane +10% self-window, derived from EARLIER KB casts,
      - x1.30 Kassatsu on the ninjutsu that consumes it,
      - the raid-buff + in-sim-tincture multiplier (`buff_intervals`) at its time;
    plus the Bunshin shadow mirror (+160p per mirrored weaponskill). Symmetric on
    delivered + idealized (the same function scores both)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier;
    # a no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    casts = sorted(timeline, key=lambda x: x[0])

    kb_end = float("-inf")
    kassatsu = False
    meisui = False
    kazematoi = 0
    bunshin = 0
    total = 0.0
    for t, aid in casts:
        base = potency_for(aid, n_of(t, aid), nd.JOB_DATA)
        if aid == nd.AEOLIAN_EDGE and kazematoi >= 1:
            base += nd.AEOLIAN_KAZEMATOI_BONUS_P
        elif aid in (nd.BHAVACAKRA, nd.ZESHO_MEPPO) and meisui:
            base += nd.MEISUI_BONUS_P
        if base > 0:
            m = nd.KUNAIS_BANE_MULT if kb_end > t else 1.0
            if aid in nd.NINJUTSU_IDS and kassatsu:
                m *= nd.KASSATSU_MULT
            if bi:
                m *= multiplier_at(t, bi)
            total += base * m
        # Bunshin shadow mirror.
        if bunshin > 0 and aid in nd.BUNSHIN_MIRRORED_IDS:
            m = nd.KUNAIS_BANE_MULT if kb_end > t else 1.0
            if bi:
                m *= multiplier_at(t, bi)
            total += nd.BUNSHIN_MIRROR_P * m
            bunshin -= 1
        # State updates AFTER scoring (a window never amps its granting cast).
        if aid in nd.NINJUTSU_IDS and kassatsu:
            kassatsu = False
        if aid == nd.AEOLIAN_EDGE:
            kazematoi = max(0, kazematoi - 1) if kazematoi >= 1 else 0
        elif aid == nd.ARMOR_CRUSH:
            kazematoi = min(nd.KAZEMATOI_CAP, kazematoi + 2)
        elif aid == nd.KUNAIS_BANE:
            kb_end = t + nd.KUNAIS_BANE_DURATION_S
        elif aid == nd.KASSATSU:
            kassatsu = True
        elif aid == nd.MEISUI:
            meisui = True
        elif aid in (nd.BHAVACAKRA, nd.ZESHO_MEPPO):
            meisui = False
        elif aid == nd.BUNSHIN:
            bunshin = nd.BUNSHIN_STACKS
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ------------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (NIN has no pet scalar);
    `coverage_intervals` is unused (Kunai's Bane rides the timeline, not an
    overlay). `target_intervals` is the multi-target N(t) schedule (None ->
    single target; NIN's kit declares no splash, so it's inert either way)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=nin_simulator,
    score_timeline=_score_timeline,
    enabler_ids=nd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- NINScoringAspect ----------------------------------------------------------

class _NinCtx:
    """Per-pull context: the phase-continuation entry state (carried Ninki /
    Kazematoi + an earlier opener start on continuation logs)."""
    __slots__ = ("entry",)

    def __init__(self, entry):
        self.entry = entry


class NinjaScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a NIN run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. Kunai's Bane / Kassatsu / Kazematoi / Meisui / Bunshin are all
    derived from the player's own casts (symmetric with the idealized ceiling);
    the one per-pull measured axis is the phase-continuation entry state."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed on the Huton-hasted weaponskill GCD. The fixed-rate
    # mudra (0.5s) / ninjutsu (1.5s) / TCJ (~1.0s) pairs fall BELOW the inference
    # band (0.80 x 2.125 = 1.70s), so they self-exclude — no exclusion hook needed.
    gcd_constant = nin_simulator.NIN_GCD_S
    # NOT a flat-GCD job (fixed-rate mudra/ninjutsu GCDs interleave everywhere):
    # a count-based cadence would fold the 0.5s/1.5s slots in and over-credit the
    # ceiling, so the demonstrated-cadence anchor stays off (the VPR reasoning).
    demonstrated_cadence_anchor = False

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any], norm_casts) -> Any:
        # Assembled from the shared primitives (not `entry_state`) so the gauge
        # measurement can be window-capped — the Bunshin-shadow Ninki guard.
        gauges = {k: v for k, v in measure_entry_gauge(
            norm_casts, nd.JOB_DATA.gauges, window_s=_ENTRY_WINDOW_S).items() if v}
        opener = measure_opener_start(norm_casts, frozenset(), _ENTRY_ENGAGE_S)
        entry = None
        if gauges or opener is not None:
            entry = EntryState(gauges=tuple(sorted(gauges.items())),
                               opener_start_s=opener)
        return _NinCtx(entry)

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
