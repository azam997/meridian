"""PCT-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is PCT-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

PCT's damage side is uniform per-cast potency (no DoT, no maintained personal
amp — `coverage_intervals` is `None`, like RDM/BLM) with ONE bespoke family:
the hammer trio is guaranteed crit + direct hit, scored with the tier-measured
`GUARANTEED_CRIT_DH_MULT` (2.26 — see data.py provenance). The multiplier is
applied identically in the delivered scorer, the sim's incremental beam score
(`apply_cast`) and the prune credits — the three-way symmetry that keeps hammer
placement honestly priced on both sides. The Starry Muse +5% party buff is NOT
scored here: it rides the shared `raid_buffs.PROVIDER_BUFFS` overlay (scoring it
in the job would double-count).

PCT is RNG-free, so there is no proc-budget `sim_context`; the per-pull context
is the per-player effective GCD (the shared `gcd_constant` machinery), with the
M12S-P2 entry-state payload wired during live calibration.
"""
from __future__ import annotations

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.pictomancer import data as pd
from jobs.pictomancer import simulator as pct_simulator


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's (AoE-aware) table potency,
    scaled by the raid-buff multiplier active at its time, with the guaranteed
    crit+DH multiplier on the hammer trio. The Star Prism follow-up (34682) and
    all motifs/buttons carry 0 potency and drop out naturally. The same function
    scores both the player's timeline and the idealized ceiling — symmetric.
    `target_fn(t, aid) -> n` supplies the per-cast target count (None -> single
    target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast
    # multiplier; a no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), pd.JOB_DATA)
        if base <= 0:
            continue
        m = multiplier_at(t, bi) if bi else 1.0
        if aid in pd.ALWAYS_CRIT_DH_IDS:
            m *= pd.GUARANTEED_CRIT_DH_MULT
        total += base * m
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) -----------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (PCT has no pet scalar);
    `coverage_intervals` is None (no job-wide overlay). `target_intervals` is
    the multi-target N(t) schedule (None -> single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    pd.JOB_DATA.tincture_main_stat, pd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=pct_simulator,
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


# --- PCTScoringAspect ---------------------------------------------------------

class PCTScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a PCT run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights
    up unchanged. PCT is RNG-free, so the only per-pull context is the
    per-player effective GCD (the shared `gcd_constant` machinery)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Spell Speed (BiS runs ~2.50; the inference tightens the ceiling
    # for a faster build — SpS scales the cast times + per-ability recasts too,
    # handled in the model).
    gcd_constant = pct_simulator.PCT_GCD_S
    # `demonstrated_cadence_anchor` stays False: PCT has a MODELED haste window
    # (Inspiration) AND mixed fixed-ratio slots (3.3 / 4.0 / 6.0) — a count-based
    # demonstrated cadence would fold both in and double-credit them.
    #
    # The pre-pull hardcasts that land at t~0: Rainbow Drip on a cold open
    # (every probed cold pull), Fire in Red on an M12S-P2 loaded continuation.
    prepull_channel_ids = frozenset({pd.RAINBOW_DRIP, pd.FIRE_IN_RED})

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def gcd_inference_exclusions(self, norm_casts):
        """Exclude the Starry Muse burst from the per-player gear-GCD inference.
        Inspiration hastes the CMY spells to ~2.475s — inside the inference band
        — so without this the inference reads the modeled haste as fast GEAR and
        the ceiling double-counts it (the BLM Ley Lines lesson). Unhasted CMY
        (3.3s), motifs (4.0s) and Rainbow Drip (6.0s) fall outside the band and
        self-exclude; hasted RGB (1.875s) falls below it and self-excludes.
        Each Starry cast opens a window [t, t + 30s] (the Inspiration hard cap;
        stacks always run out sooner)."""
        return [(t, t + pd.INSPIRATION_WINDOW_S)
                for t, aid in norm_casts if aid == pd.STARRY_MUSE and t >= 0]
