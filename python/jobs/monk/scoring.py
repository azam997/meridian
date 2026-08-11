"""MNK-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is MNK-specific; the scaffolding around it — the LRU-cached
perfect-sim ceiling, the enabler valuation, and the `Scoring` aspect's analyze
flow — comes from `jobs/_core/sim/scoring.py` via `build_scoring` +
`ScoringAspectBase`.

MNK's bespoke damage pieces, all scored by a single forward pass that **mirrors
`simulator.apply_cast` exactly** (so the beam-prune key matches the final score
and delivered/idealized stay symmetric — the >100% guard holds by construction):

  * **Riddle of Fire derived from the timeline casts** — the +15% window
    `[cast_t, +20.7s)` per RoF cast (measured in-game expiry); a cast is amped
    by a window opened by an EARLIER cast, never its own. Brotherhood's +5% is
    the PARTY buff — it rides the job-agnostic raid-buff overlay
    (`buff_intervals`), never this pass (double-count hazard).
  * **The Fury bonuses** — +200 on Leaping Opo / Rising Raptor, +150 on Pouncing
    Coeurl, tracked forward from the generator casts (Dragon Kick / Twin Snakes
    / Demolish), form-eligibility-gated exactly like the game.
  * **The guaranteed crit** — Leaping Opo (and Shadow of the Destroyer) x1.62
    when opo-eligible (form / Perfect Balance / Formless Fist) — measured 100%
    crit on every probed live parse; the SAM Setsugekka pricing.
  * **Forms / Perfect Balance / Formless Fist** — a light state machine (the
    30s form wheel, PB's 3 form-free stacks, the Formless grants from blitzes /
    Fire's Reply / Form Shift) drives the eligibility above. Both passes start
    Formless (the universal pre-pull Form Shift; symmetric with the sim's
    prepull).

Blitzes / replies / The Forbidden Chakra / Six-Sided Star score at their flat
table values (SSS's +80-per-chakra is invisible to the cast stream and unmodeled
on BOTH sides — symmetric). The per-pull measured axis is the chakra budget
(`MonkCtx.tfc_budget` — chakra generation is crit-RNG + party-fed, the DNC
budget pattern).
"""
from __future__ import annotations

from typing import Any

from jobs._core.buff_windows import multiplier_at
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.monk import data as md
from jobs.monk import simulator as mnk_simulator
from jobs.monk.simulator import MonkCtx


_TINCTURE_SPEC = spec_for_job(
    md.JOB_DATA.tincture_main_stat, md.JOB_DATA.tincture_role_coeff)

_NO_FORM, _OPO, _RAPTOR, _COEURL = 0, 1, 2, 3
_FORM_OF = {**{a: _OPO for a in md.OPO_GCD_IDS},
            **{a: _RAPTOR for a in md.RAPTOR_GCD_IDS},
            **{a: _COEURL for a in md.COEURL_GCD_IDS}}
_NEXT_FORM = {_OPO: _RAPTOR, _RAPTOR: _COEURL, _COEURL: _OPO}


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly via one forward pass (the exact per-cast
    math `simulator.apply_cast` runs incrementally). Each cast's table potency
    (plus the state-derived Fury bonuses) is scaled by:
      - the Riddle of Fire +15% self-window, derived from EARLIER RoF casts,
      - x1.62 on the guaranteed-crit ids when opo-eligible,
      - the raid-buff + in-sim-tincture multiplier (`buff_intervals`) at its time.
    Symmetric on delivered + idealized (the same function scores both)."""
    # Fold the sim's in-timeline tincture pot marker into the per-cast multiplier;
    # a no-op for the player's delivered timeline (no marker).
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    bi = buff_intervals or None
    n_of = target_fn or (lambda _t, _a: 1)
    casts = sorted(timeline, key=lambda x: x[0])

    # Both sides open Formless (the universal pre-pull Form Shift — the sim's
    # prepull arms it too, so the assumption cancels in the ratio).
    form = _NO_FORM
    form_end = float("-inf")
    formless_end = md.FORMLESS_DURATION_S
    pb_left = 0
    pb_end = float("-inf")
    opo_fury = raptor_fury = coeurl_fury = 0
    rof_end = float("-inf")
    total = 0.0

    for t, aid in casts:
        in_pb = pb_left > 0 and pb_end > t
        opo_eligible = (in_pb or formless_end > t
                        or (form == _OPO and form_end > t))

        base = potency_for(aid, n_of(t, aid), md.JOB_DATA)
        if aid == md.LEAPING_OPO and opo_fury >= 1:
            base += md.OPO_FURY_BONUS_P
        elif aid == md.RISING_RAPTOR and raptor_fury >= 1:
            base += md.RAPTOR_FURY_BONUS_P
        elif aid == md.POUNCING_COEURL and coeurl_fury >= 1:
            base += md.COEURL_FURY_BONUS_P
        if base > 0:
            m = md.RIDDLE_OF_FIRE_MULT if rof_end > t else 1.0
            if aid in md.ALWAYS_CRIT_IDS and opo_eligible:
                m *= md.GUARANTEED_CRIT_MULT
            if bi:
                m *= multiplier_at(t, bi)
            total += base * m

        # State updates AFTER scoring (a window never amps its granting cast).
        if aid in md.FORM_GCD_IDS:
            fam = _FORM_OF[aid]
            if aid == md.LEAPING_OPO and opo_fury >= 1:
                opo_fury -= 1
            elif aid == md.RISING_RAPTOR and raptor_fury >= 1:
                raptor_fury -= 1
            elif aid == md.POUNCING_COEURL and coeurl_fury >= 1:
                coeurl_fury -= 1
            elif aid == md.DRAGON_KICK and opo_eligible:
                opo_fury = min(1, opo_fury + 1)
            elif aid == md.TWIN_SNAKES:
                raptor_fury = min(1, raptor_fury + 1)
            elif aid == md.DEMOLISH:
                coeurl_fury = min(2, coeurl_fury + 2)
            if in_pb:
                pb_left -= 1
            else:
                if formless_end > t:
                    formless_end = float("-inf")
                form = _NEXT_FORM[fam]
                form_end = t + md.FORM_DURATION_S
        elif aid in md.BLITZ_IDS:
            pb_left = 0
            formless_end = t + md.FORMLESS_DURATION_S
        elif aid in (md.FIRES_REPLY, md.FORM_SHIFT):
            formless_end = t + md.FORMLESS_DURATION_S
        elif aid == md.RIDDLE_OF_FIRE:
            rof_end = t + md.RIDDLE_OF_FIRE_DURATION_S
        elif aid == md.PERFECT_BALANCE:
            pb_left = md.PB_STACKS
            pb_end = t + md.PB_STACK_DURATION_S
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ------------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (MNK has no pet scalar);
    `coverage_intervals` is unused (Riddle of Fire rides the timeline, not an
    overlay). `target_intervals` is the free-splash N(t) schedule (None ->
    single target, byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_FNS = build_scoring(
    sim_module=mnk_simulator,
    score_timeline=_score_timeline,
    enabler_ids=md.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- MNKScoringAspect ----------------------------------------------------------

class _MnkCtx:
    """Per-pull context: the measured chakra budget."""
    __slots__ = ("ctx",)

    def __init__(self, ctx: MonkCtx | None):
        self.ctx = ctx


class MonkScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a MNK run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights up
    unchanged. Riddle of Fire / Fury / forms / the guaranteed crit are all
    derived from the player's own casts (symmetric with the idealized ceiling);
    the per-pull measured axis is the chakra budget (The Forbidden Chakra +
    Enlightenment casts — each one 5 chakra the player provably generated)."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed on the Greased-Lightning 2.00 GCD. The 1s Forbidden
    # Meditation pairs fall BELOW the inference band (0.80 x 2.00 = 1.60s) and
    # self-exclude; Six-Sided Star's 4s pairs fall above it (outliers) — no
    # exclusion hook needed.
    gcd_constant = mnk_simulator.MNK_GCD_S
    # MNK has a FLAT GCD (no modeled sub-GCD/haste window — Riddle of Wind hastes
    # only auto-attacks), so the demonstrated-cadence anchor is valid: a parse
    # sustaining tighter server-tick queuing than the fixed band floor is bounded
    # by its own GCD count (the GNB lesson). Downtime Meditations are counted as
    # GCDs by the anchor (the convention that credits downtime output); that reads
    # the anchor slightly FAST on downtime encounters — a conservative,
    # ceiling-raising direction the max-guard keeps safe.
    demonstrated_cadence_anchor = True

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any], norm_casts) -> Any:
        n = sum(1 for t, aid in norm_casts
                if t >= 0 and aid in (md.THE_FORBIDDEN_CHAKRA, md.ENLIGHTENMENT))
        return _MnkCtx(MonkCtx(tfc_budget=n) if n > 0 else None)

    def sim_context(self, ctx: Any) -> Any:
        return ctx.ctx or None

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        return {
            "sim_context": ctx.ctx or None,
            "tfcBudget": ctx.ctx.tfc_budget if ctx.ctx else 0,
        }
