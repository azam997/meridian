"""SMN-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is SMN-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

SMN's damage side is uniform per-cast potency with NO bespoke families at all:
the pet contributions are folded into the player's own cast ids in the data
table (demi autos at the summon cast, Enkindle payoffs on the Enkindle id,
primal bursts on the primal-summon id — see data.py), so the delivered scorer,
the sim's incremental beam score (`apply_cast`) and the prune credits price the
identical constants on the identical ids — the three-way symmetry is trivial by
construction. Slipstream's windstorm DoT is likewise folded into its cast
potency (the PLD Circle-of-Scorn pattern). No guaranteed-crit family, no
maintained personal amp — `coverage_intervals` is `None`, like RDM/BLM/PCT.
The Searing Light +5% party buff is NOT scored here: it rides the shared
`raid_buffs.PROVIDER_BUFFS` overlay (scoring it in the job would double-count).

SMN is RNG-free (Further Ruin / Ruby's Glimmer / favors are deterministic
grants), so there is no proc-budget `sim_context`; the per-pull context is the
per-player effective GCD (the shared `gcd_constant` machinery) plus a dormant
phase-continuation entry gauge (aetherflow — probes show M12S-P2 opens COLD, so
the measurement returns empty and the ceiling stays byte-identical).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.entry_gauge import EntryState, measure_entry_gauge
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.summoner import data as sd
from jobs.summoner import simulator as smn_simulator


_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's (AoE-aware) table potency,
    scaled by the raid-buff multiplier active at its time. The pet folds ride
    the player cast ids, and the pet's own damage ids never appear in a cast
    stream, so the same function scores both the player's timeline and the
    idealized ceiling — symmetric. `target_fn(t, aid) -> n` supplies the
    per-cast target count (None -> single target, byte-identical)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast
    # multiplier; a no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    total = 0.0
    for t, aid in timeline:
        base = potency_for(aid, n_of(t, aid), sd.JOB_DATA)
        if base <= 0:
            continue
        total += base * (multiplier_at(t, bi) if bi else 1.0)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) -----------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (the pet folds ride the
    cast ids); `coverage_intervals` is None (no job-wide overlay).
    `target_intervals` is the multi-target N(t) schedule (None -> single
    target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=smn_simulator,
    score_timeline=_score_timeline,
    enabler_ids=sd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- SMNScoringAspect ---------------------------------------------------------

class _SmnCtx:
    """Per-pull context: the phase-continuation entry state (carried
    aetherflow on a hypothetical mid-cycle continuation log)."""
    __slots__ = ("entry",)

    def __init__(self, entry):
        self.entry = entry


class SMNScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for an SMN run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights
    up unchanged. SMN is RNG-free, so the per-pull context is the per-player
    effective GCD (the shared `gcd_constant` machinery) plus the dormant entry
    gauge below."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Spell Speed (BiS runs ~2.48; the inference tightens the ceiling
    # for a faster build — SpS scales the cast times + per-ability recasts too,
    # handled in the model). The impulses / Topaz Rite / Ruin III run the 1.0x
    # recast and are valid inference pairs; Emerald (0.6x = 1.5s) falls BELOW
    # the inference band and Ruby (1.2x = 3.0s) / Slipstream (1.4x) fall above,
    # so they self-exclude — no exclusion hook needed (the NIN mudra situation).
    gcd_constant = smn_simulator.SMN_GCD_S
    # NOT a flat-GCD job (mixed per-ability fixed-ratio slots: 1.5s Emerald /
    # 3.0s Ruby / 3.5s Slipstream interleave everywhere): a count-based
    # demonstrated cadence would fold the fast Emerald slots in and over-credit
    # the ceiling, so the anchor stays off (the NIN/VPR reasoning).
    demonstrated_cadence_anchor = False
    # The pre-pull hardcast that lands at t~0 on every probed open (cold M11S,
    # M12S-P1 AND the M12S-P2 continuations): Ruin III.
    prepull_channel_ids = frozenset({sd.RUIN_III})

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any], norm_casts) -> Any:
        # Aetherflow is a closed cast-visible economy (Energy Drain/Siphon is
        # the only generator and both are in the cast stream), so the
        # deepest-deficit measurement needs no window cap (unlike NIN's
        # Bunshin-shadow Ninki). Probes show M12S-P2 opens COLD — this returns
        # empty there and the ceiling stays byte-identical.
        gauges = {k: v for k, v in measure_entry_gauge(
            norm_casts, sd.JOB_DATA.gauges).items() if v}
        entry = EntryState(gauges=tuple(sorted(gauges.items()))) if gauges else None
        return _SmnCtx(entry)

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
