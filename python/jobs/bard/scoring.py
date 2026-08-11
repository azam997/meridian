"""BRD-specific delivered-potency scoring + idealized-sim wrapper.

The scoring *math* is BRD-specific; the scaffolding — the LRU-cached perfect-sim
ceiling, the enabler valuation, and the `Scoring` aspect's analyze flow — comes
from `jobs/_core/sim/scoring.py` via `build_scoring` + `ScoringAspectBase`.

BRD's bespoke damage pieces, all derived from the timeline's own casts and
applied symmetrically (delivered + idealized both carry their own casts, so a
perfectly-played piece cancels in the ratio and the ≤100% guard holds):

  * **Raging Strikes** — the job-owned +15% / 20s self-buff window (the PLD
    Fight-or-Flight pattern). Battle Voice / Radiant Finale are PARTY buffs and
    live in the shared raid_buffs.py catalog — modeling them here too would
    double-count in the observed/master lenses.
  * **Barrage** — the first Refulgent Arrow (or Shadowbite) after each Barrage
    lands ×3 (three separate ~280p hits, live-verified); Shadowbite is bumped to
    its 270 Barrage potency instead.
  * **Radiant Encore Coda tiers** — 700/800/1100 by the Coda count the granting
    Radiant Finale consumed, reconstructed from the song/Finale history in the
    same timeline (live-verified: the opener 1-Coda Encore reads ~700p).
  * **The DoTs** (Caustic Bite / Stormbite) — scored per application (the dot
    cast, or an Iron Jaws while it's active) by *time-to-next-refresh* capped at
    the 45s duration, snapshotting the multiplier at the application instant
    (the SAM Higanbana pattern): over-refreshing credits less, never
    double-counts, and a buffed Iron Jaws re-snapshot is worth real potency.

**Budget crediting (PP / Apex).** Pitch Perfect and Apex Arrow have
tier/gauge-scaled potency the cast stream can't see, so both sides score every
spend at the full-tier value (360 / 700) and the ceiling spends the player's
measured COUNT: the tier mix cancels exactly in the efficiency ratio. The cost
is that spending at a low tier (a 1-stack PP, a 60-gauge Apex) is invisible to
efficiency — accepted, like DNC's proc budgets ("below-average luck never costs
efficiency; only misuse does"), since the tier is itself mostly luck-timing.

The per-pull `sim_context` is the set of measured budgets (see data.py); the
GCD constant enables the per-player inference, with the Army's Paeon haste +
Army's Muse windows excluded from it (the ceiling models those in
`gcd_duration` — counting them as gear would double-credit the haste, the BLM
Ley Lines rule). `demonstrated_cadence_anchor` stays False for the same reason:
uptime/GCD-count folds the hasted AP GCDs in.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from jobs._core.buff_windows import BuffWindow, multiplier_at, multiplier_intervals
from jobs._core.sim.aoe_potency import potency_for, schedule_target_fn
from jobs._core.sim.scoring import ScoringAspectBase, build_scoring
from jobs._core.tincture import merge_tincture_markers, spec_for_job
from jobs.bard import data as bd
from jobs.bard import simulator as brd_simulator
from jobs.bard.simulator import BardCtx


def _self_buff_windows(timeline: list[tuple[float, int]]) -> list[BuffWindow]:
    """Raging Strikes windows derived from the timeline's own casts (symmetric
    on delivered + idealized)."""
    return [BuffWindow(t, t + bd.RAGING_STRIKES_DURATION_S,
                       bd.RAGING_STRIKES_MULT, "Raging Strikes")
            for t, aid in timeline if aid == bd.RAGING_STRIKES]


def _barrage_armed_casts(ordered: list[tuple[float, int]]) -> set[int]:
    """Indices (into `ordered`) of the Hawk's Eye spends armed by a Barrage —
    the first Refulgent/Shadowbite within 30s after each Barrage cast."""
    out: set[int] = set()
    armed_until = float("-inf")
    for i, (t, aid) in enumerate(ordered):
        if aid == bd.BARRAGE:
            armed_until = t + bd.BARRAGE_READY_DURATION_S
        elif aid in (bd.REFULGENT_ARROW, bd.SHADOWBITE) and t <= armed_until:
            out.add(i)
            armed_until = float("-inf")
    return out


def _encore_potencies(ordered: list[tuple[float, int]]) -> dict[int, int]:
    """Index → potency for each Radiant Encore, from the Coda count its granting
    Radiant Finale consumed (songs played since the previous Finale, capped 3).
    An Encore with no in-log Finale (a phase-continuation carry) defaults to the
    3-Coda value."""
    out: dict[int, int] = {}
    coda = 0
    pending: int | None = None
    for i, (_t, aid) in enumerate(ordered):
        if aid in bd.SONG_ORDER:
            coda = min(3, coda + 1)
        elif aid == bd.RADIANT_FINALE:
            pending = bd.ENCORE_POTENCY_BY_CODA[max(1, min(3, coda))]
            coda = 0
        elif aid == bd.RADIANT_ENCORE:
            out[i] = pending if pending is not None \
                else bd.ENCORE_POTENCY_BY_CODA[3]
            pending = None
    return out


def _dot_potency(
    ordered: list[tuple[float, int]],
    dot_cast_id: int,
    tick_p: int,
    combined: list[tuple[float, float, float]] | None,
) -> float:
    """One DoT's potency, summed per application. Applications are the dot cast
    itself plus every Iron Jaws while the dot is ACTIVE (the in-game rule — IJ
    does not re-apply a fallen dot). Each application is credited
    `min(45s, time-to-next-application)` of ticks at the multiplier snapshotted
    at the application instant; the trailing application gets the full duration
    (both timelines end at the kill, so the convention is symmetric)."""
    apps: list[float] = []
    last_end = float("-inf")
    for t, aid in ordered:
        if aid == dot_cast_id:
            apps.append(t)
            last_end = t + bd.DOT_DURATION_S
        elif aid == bd.IRON_JAWS and t <= last_end:
            apps.append(t)
            last_end = t + bd.DOT_DURATION_S
    if not apps:
        return 0.0
    total = 0.0
    for i, ct in enumerate(apps):
        covered = (min(bd.DOT_DURATION_S, max(0.0, apps[i + 1] - ct))
                   if i + 1 < len(apps) else bd.DOT_DURATION_S)
        m = multiplier_at(ct, combined) if combined else 1.0
        total += covered / bd.DOT_TICK_S * tick_p * m
    return total


def score_delivered_potency(
    timeline: list[tuple[float, int]],
    buff_intervals: list[tuple[float, float, float]] | None = None,
    target_fn=None,
) -> float:
    """Score a cast timeline uniformly: every cast's table potency — with the
    Barrage ×3, the Encore Coda tier, and the budget-flat PP/Apex values — scaled
    by the product of every buff active at its time (Raging Strikes derived from
    this same timeline, the raid buffs, and the in-sim tincture), plus the two
    DoTs scored per application. There is no GCD/oGCD distinction: the buffs amp
    all personal damage. `target_fn(t, aid) -> n` supplies the per-cast target
    count (None → single target, byte-identical)."""
    buff_intervals = merge_tincture_markers(timeline, buff_intervals, _TINCTURE_SPEC)
    raid = [BuffWindow(s, e, m, "raid") for s, e, m in (buff_intervals or [])]
    combined = multiplier_intervals(raid + _self_buff_windows(timeline)) or None
    n_of = target_fn or (lambda _t, _a: 1)

    ordered = sorted(timeline, key=lambda x: x[0])
    armed = _barrage_armed_casts(ordered)
    encore_p = _encore_potencies(ordered)

    total = 0.0
    for i, (t, aid) in enumerate(ordered):
        base = potency_for(aid, n_of(t, aid), bd.JOB_DATA)
        if aid == bd.RADIANT_ENCORE and i in encore_p:
            # Swap the primary for the Coda tier; splash (if any) stays as keyed.
            base += encore_p[i] - bd.POTENCIES[bd.RADIANT_ENCORE]
        elif i in armed:
            if aid == bd.REFULGENT_ARROW:
                base *= bd.BARRAGE_REFULGENT_HITS
            else:  # Shadowbite: bumped to its Barrage potency on every hit
                base *= bd.BARRAGE_SHADOWBITE_POTENCY / bd.POTENCIES[bd.SHADOWBITE]
        if base <= 0:
            continue
        m = multiplier_at(t, combined) if combined else 1.0
        total += base * m
    total += _dot_potency(ordered, bd.STORMBITE, bd.STORMBITE_DOT_TICK_P, combined)
    total += _dot_potency(ordered, bd.CAUSTIC_BITE, bd.CAUSTIC_DOT_TICK_P, combined)
    return total


# --- Scoring scaffolding (cached ceiling, enabler valuation) ------------------

def _score_timeline(timeline, aux, coverage_intervals, buff_intervals,
                    target_intervals=None) -> float:
    """Uniform engine scoring entry. `aux` is unused (BRD has no pet scalar);
    `coverage_intervals` is None (Raging Strikes is derived inside
    score_delivered_potency, not supplied as a full-coverage overlay).
    `target_intervals` is the multi-target N(t) schedule (None → single target,
    byte-identical)."""
    return score_delivered_potency(
        timeline, buff_intervals=buff_intervals,
        target_fn=schedule_target_fn(target_intervals))


_TINCTURE_SPEC = spec_for_job(
    bd.JOB_DATA.tincture_main_stat, bd.JOB_DATA.tincture_role_coeff)

_FNS = build_scoring(
    sim_module=brd_simulator,
    score_timeline=_score_timeline,
    enabler_ids=bd.ENABLER_IDS,
    coverage_intervals=None,
)

# Re-exported under the names the sidecar / tests / __init__ expect.
_sim_cache_keys = _FNS.sim_cache_keys
_perfect_sim_cached = _FNS.perfect_sim_cached
idealized_at_duration = _FNS.idealized_at_duration
perfect_sim_timeline = _FNS.perfect_sim_timeline
enabler_net_values = _FNS.enabler_net_values


# --- BardScoringAspect ---------------------------------------------------------

def _measure_ctx(norm_casts) -> BardCtx:
    """The player's measured RNG-resource counts — the budgets the idealized
    ceiling spends so below-average Repertoire / Hawk's Eye / Soul Voice luck
    never costs efficiency:
      * refulgent = Refulgent Arrow + Shadowbite casts MINUS the Barrage-armed
        ones (Barrage grants usability itself, not a Hawk's Eye spend),
      * pp / apex / blast = Pitch Perfect / Apex Arrow / Blast Arrow casts,
      * hb = Heartbreak Shot + Rain of Death casts (the Mage's Ballad recast
        economy)."""
    c = Counter(aid for t, aid in norm_casts if t >= 0)
    barrage = c.get(bd.BARRAGE, 0)
    refulgent = max(0, c.get(bd.REFULGENT_ARROW, 0) + c.get(bd.SHADOWBITE, 0)
                    - barrage)
    return BardCtx(
        refulgent_budget=refulgent,
        pp_budget=c.get(bd.PITCH_PERFECT, 0),
        apex_budget=c.get(bd.APEX_ARROW, 0),
        blast_budget=c.get(bd.BLAST_ARROW, 0),
        hb_budget=c.get(bd.HEARTBREAK_SHOT, 0) + c.get(bd.RAIN_OF_DEATH, 0),
    )


class BardScoringAspect(ScoringAspectBase):
    """Computes delivered_potency + idealized_potency for a BRD run. Emits the
    same state-key shape as the other scorers so the dashboard headline lights
    up unchanged. The per-pull context is the player's measured budgets, fed
    into the idealized ceiling via `sim_context` and stashed on state so the
    sidecar's lenient / timeline sims spend the same counts."""

    fns = _FNS
    tincture_spec = _TINCTURE_SPEC
    # Per-player Skill Speed: the inference band centers on the 2.5s global; the
    # Army's Paeon haste windows are excluded below so they can't read as gear.
    gcd_constant = brd_simulator.BRD_GCD_S
    # A haste-window job must NOT use the demonstrated-cadence anchor: the
    # count-based cadence would fold the modeled AP GCDs in and double-credit.
    demonstrated_cadence_anchor = False

    def gcd_inference_exclusions(self, norm_casts):
        """Army's Paeon (+ the 10s Army's Muse tail) windows, from the player's
        own song casts — a self-haste buff, not gear (the BLM Ley Lines rule)."""
        songs = sorted((t, aid) for t, aid in norm_casts
                       if t >= 0 and aid in bd.SONG_ORDER)
        out: list[tuple[float, float]] = []
        for i, (t, aid) in enumerate(songs):
            if aid != bd.ARMYS_PAEON:
                continue
            end = songs[i + 1][0] if i + 1 < len(songs) else t + bd.SONG_DURATION_S
            out.append((t, min(end, t + bd.SONG_DURATION_S) + bd.MUSE_DURATION_S))
        return out

    def prepare(self, client, code: str, fight: dict[str, Any],
                actor: dict[str, Any], report: dict[str, Any],
                norm_casts) -> Any:
        return _measure_ctx(norm_casts)

    def sim_context(self, ctx: Any) -> Any:
        return ctx  # the BardCtx (measured budgets)

    def score_delivered(self, ctx, in_fight_casts, buff_intervals=None) -> float:
        return score_delivered_potency(in_fight_casts, buff_intervals=buff_intervals)

    def extra_state(self, ctx: Any) -> dict:
        # `sim_context` is read by the sidecar's `you`-based sim calls (lenient
        # ceiling, timeline lanes) so they spend the same budgets as the strict
        # ceiling. The scalars are the human-facing aliases.
        return {
            "sim_context": ctx,
            "refulgentBudget": ctx.refulgent_budget,
            "ppBudget": ctx.pp_budget,
            "apexBudget": ctx.apex_budget,
            "blastBudget": ctx.blast_budget,
            "heartbreakBudget": ctx.hb_budget,
        }
