"""Idealized PLD rotation — the Paladin `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the PLD-specific rotation: the combo/proc state, the
priority pickers, and the per-cast transitions. The four `simulate_*` functions
at the bottom are thin shims that bind this model to the engine (kept under their
original names so the sidecar, the scorer and the tests call them unchanged).

PLD-specific rotation encoded (NO gauges — Oath is defensive-only):
- Main combo: Fast Blade -> Riot Blade -> Royal Authority. Royal Authority grants
  the Atonement chain (Atonement -> Supplication -> Sepulchre) + Divine Might
  (one free instant Holy Spirit). Steady-state filler is therefore
  FB,RB,RA,AT,SU,SE,HS repeating.
- Burst (every 60s): Fight or Flight (the 20s self damage-up window) + Imperator
  (damage + opens the magical combo) + Goring Blade. The magical combo
  Confiteor -> Blade of Faith -> Blade of Truth -> Blade of Valor (all instant,
  ranged) then Blade of Honor (oGCD). Circle of Scorn / Expiacion / Intervene
  weave on cooldown.
- Ranged opener: a pre-pull Holy Spirit channel (begincast-anchored, like RPR's
  pre-Harpe), swept on/off.

Out of scope for v1 (documented, intentionally not modeled):
- Hardcast Holy Spirit as steady-state filler (only Divine Might / Requiescat
  instant casts + the pre-pull channel are modeled).
- MP economy / Oath gauge (neither binds the optimized rotation).
- DoTs (Circle of Scorn folded into its direct potency).
- Frame-perfect animation timing.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache

from jobs._core.sim import engine, optimal
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.paladin import data as pd


# --- Ability IDs (aliased from data for readability) -----------------------
FAST_BLADE       = pd.FAST_BLADE
RIOT_BLADE       = pd.RIOT_BLADE
ROYAL_AUTHORITY  = pd.ROYAL_AUTHORITY
ATONEMENT        = pd.ATONEMENT
SUPPLICATION     = pd.SUPPLICATION
SEPULCHRE        = pd.SEPULCHRE
GORING_BLADE     = pd.GORING_BLADE
HOLY_SPIRIT      = pd.HOLY_SPIRIT
CONFITEOR        = pd.CONFITEOR
BLADE_OF_FAITH   = pd.BLADE_OF_FAITH
BLADE_OF_TRUTH   = pd.BLADE_OF_TRUTH
BLADE_OF_VALOR   = pd.BLADE_OF_VALOR
BLADE_OF_HONOR   = pd.BLADE_OF_HONOR
FIGHT_OR_FLIGHT  = pd.FIGHT_OR_FLIGHT
IMPERATOR        = pd.IMPERATOR
CIRCLE_OF_SCORN  = pd.CIRCLE_OF_SCORN
EXPIACION        = pd.EXPIACION
INTERVENE        = pd.INTERVENE
HOLY_CIRCLE      = pd.HOLY_CIRCLE   # AoE Holy Spirit (the Divine Might free instant)


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S        = 2.50     # PLD true BiS GCD (no headroom — the FoF burst-packing
                             # beam closes the search residual the old 2.45 hid)
# Where the pre-pull Holy Spirit channel marker lands: its begincast time
# (= -cast_time), matching the player's begincast-anchored precast. The hardcast
# resolves at the pull; scoring sums the whole timeline (buff multiplier 1.0
# pre-pull), so its potency is still credited.
PREPULL_CHANNEL_T = -pd.HOLY_SPIRIT_CAST_S


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """PLD picker tunables, on top of the shared knobs (max_weaves_per_gcd /
    triple_weave_clip_s / forbidden_windows):
      * `prepull_holyspirit` — pre-channel ranged Holy Spirit during the run-in
        (True) for a free opener cast that rolls the first melee GCD, or open
        straight into melee (False). Both are valid; the sweep picks the higher-
        scoring per duration."""
    prepull_holyspirit: bool = True


@dataclass
class SimState(SimStateBase):
    # Main combo: 0 expects Fast, 1 Riot, 2 Royal.
    combo_step: int = 0
    # Atonement chain procs (granted by Royal Authority).
    atonement_ready: bool = False
    supplication_ready: bool = False
    sepulchre_ready: bool = False
    divine_might: bool = False        # one free instant Holy Spirit
    # Magical combo: 0 inactive, 1 Confiteor, 2 Blade of Faith, 3 Truth, 4 Valor.
    magic_combo: int = 0
    blade_of_honor_ready: bool = False
    goring_ready: bool = False        # Goring Blade Ready (granted by Fight or Flight)


@dataclass(frozen=True)
class PldContext:
    """Combo/proc state carried INTO a phase continuation (P1->P2), threaded as the
    per-pull `sim_context`. PLD has no offensive gauge; what it carries is the combo
    machine + procs — Divine Might's free instant Holy Spirit, the Atonement chain, a
    mid main-combo step. Seeding these lets the ceiling open with the same high-potency
    GCDs the player did (a carried Divine Might Holy Spirit is 500 potency vs a
    cold-start Fast Blade 220). `None`/falsy on a fresh pull -> the sim cold-starts,
    byte-identical.

    Safe by construction: seeding a proc only *adds* an opener option the sweep may
    take while still keeping the pre-cast line, so the ceiling = max(...) can only rise
    (efficiency only falls) — it can never push a pull >100%. (Per-pull Skill Speed is
    a separate axis: the inferred GCD rides the `CeilingContext` wrapper and only
    threads when meaningfully faster than the tier constant — see `gcd_speed.py`.)"""
    divine_might: bool = False
    atonement_ready: bool = False
    supplication_ready: bool = False
    sepulchre_ready: bool = False
    combo_step: int = 0

    def __bool__(self) -> bool:
        return (self.divine_might or self.atonement_ready or self.supplication_ready
                or self.sepulchre_ready or bool(self.combo_step))


def measure_pld_context(norm_casts) -> "PldContext | None":
    """Detect combo/proc state carried into a phase continuation, inferred from the
    casts BEFORE the first Royal Authority (the in-fight granter of Divine Might + the
    Atonement chain). On a fresh pull every proc is *earned* after a Royal Authority, so
    nothing is detected -> `None` (the sim cold-starts, byte-identical). A continuation
    opens with a carried proc (Divine Might Holy Spirit / an Atonement step) before its
    first combo'd Royal Authority -> that proc is seeded."""
    divine_might = atonement = supplication = sepulchre = False
    combo_step = 0
    saw_fast = False
    for _t, aid in sorted((t, a) for t, a in norm_casts if t >= 0.0):
        if aid == ROYAL_AUTHORITY:
            break    # from here procs are earned in-fight, not carried from P1
        if aid == HOLY_SPIRIT:
            divine_might = True
        elif aid == ATONEMENT:
            atonement = True
        elif aid == SUPPLICATION:
            supplication = True
        elif aid == SEPULCHRE:
            sepulchre = True
        elif aid == FAST_BLADE:
            saw_fast = True
        elif aid == RIOT_BLADE and not saw_fast:
            combo_step = 1    # opened mid-combo (Riot with no preceding Fast Blade)
    ctx = PldContext(divine_might=divine_might, atonement_ready=atonement,
                     supplication_ready=supplication, sepulchre_ready=sepulchre,
                     combo_step=combo_step)
    return ctx if ctx else None


# --- Refinement / canonical anchors ---------------------------------------
# The greedy picker fires the burst (Fight or Flight / Imperator) as soon as it's
# available; the refinement nudges these into raid-buff windows.
_ANCHORS: tuple[int, ...] = (FIGHT_OR_FLIGHT, IMPERATOR)

# Sweep axes (kept here so the shape stays job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_SWEEP_PREPULL_HS: tuple[bool, ...] = (True, False)


# --- The PLD rotation model -----------------------------------------------

# The tincture the sim places in-rotation (jobs._core.tincture). The model carries it
# so the shared engine pots inside the sim (`_maybe_pot`); the scorer credits the pot
# at cast time. Derived from JobData, same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    pd.JOB_DATA.tincture_main_stat, pd.JOB_DATA.tincture_role_coeff)


class PaladinRotationModel(engine.BaseRotationModel):
    cooldowns = pd.COOLDOWNS
    timing = InstantGCD(base_s=GCD_BASE_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, ctx: "PldContext | None" = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()) -> None:
        self.ctx = ctx
        # Per-player Skill Speed (threaded only when faster than the constant): speeds
        # the whole rotation. None keeps the tier constant (2.50), byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)
        # Multi-target N(t) schedule: the cleaving magical combo (Confiteor / Blades),
        # Imperator, Circle of Scorn, Expiacion scale per-target in scoring (via
        # AOE_POTENCIES); the Divine Might spend swaps to Holy Circle at high N.
        # Empty () -> single target, byte-identical. (The ST-vs-AoE filler-combo fork
        # is a deferred refinement — left ST here, so a very-high-N pull disclaims
        # rather than ever showing >100%.)
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _divine_might_spell(self, state: "SimState") -> int:
        """Holy Spirit, or Holy Circle (its AoE form) when that out-potencies at the
        target count — gauge-free (both consume Divine Might), so closed-form."""
        n = self._n(state.t)
        if n >= 2 and potency_for(HOLY_CIRCLE, n, pd.JOB_DATA) > potency_for(
                HOLY_SPIRIT, n, pd.JOB_DATA):
            return HOLY_CIRCLE
        return HOLY_SPIRIT

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {
            FIGHT_OR_FLIGHT: 0.0, IMPERATOR: 0.0, GORING_BLADE: 0.0,
            CIRCLE_OF_SCORN: 0.0, EXPIACION: 0.0,
        }
        state.charges = {INTERVENE: 2.0}
        # Phase-continuation: seed combo/proc state carried out of the prior phase.
        if self.ctx is not None:
            state.divine_might = self.ctx.divine_might
            state.atonement_ready = self.ctx.atonement_ready
            state.supplication_ready = self.ctx.supplication_ready
            state.sepulchre_ready = self.ctx.sepulchre_ready
            state.combo_step = self.ctx.combo_step
        return state

    def prepull(self, state: SimState, params) -> None:
        # Melee engage delay: the in-fight loop starts after the run-in to the
        # boss. With the ranged precast option, channel Holy Spirit (25y) during
        # the run-in for a free opener cast that resolves at the pull.
        engage = pd.JOB_DATA.role_policy.engage_delay_s
        if params.prepull_holyspirit:
            state.timeline.append((PREPULL_CHANNEL_T, HOLY_SPIRIT))
            state.t = max(engage, GCD_BASE_S)
        else:
            state.t = engage

    def pick_gcd(self, state: SimState, params) -> int:
        # 1. Finish the magical combo once Imperator opened it (highest potency).
        if state.magic_combo == 1:
            return CONFITEOR
        if state.magic_combo == 2:
            return BLADE_OF_FAITH
        if state.magic_combo == 3:
            return BLADE_OF_TRUTH
        if state.magic_combo == 4:
            return BLADE_OF_VALOR

        # 2. Goring Blade — the Fight or Flight proc (Goring Blade Ready). It's
        # granted by FoF, so it's always cast inside the buff window and always
        # amplified, which keeps the ceiling honest (a real in-buff Goring can't
        # beat the idealized one).
        if state.goring_ready:
            return GORING_BLADE

        # 3. Spend the Atonement chain, then the Divine Might Holy Spirit.
        if state.sepulchre_ready:
            return SEPULCHRE
        if state.supplication_ready:
            return SUPPLICATION
        if state.atonement_ready:
            return ATONEMENT
        if state.divine_might:
            return self._divine_might_spell(state)

        # 4. Main combo filler.
        if state.combo_step == 1:
            return RIOT_BLADE
        if state.combo_step == 2:
            return ROYAL_AUTHORITY
        return FAST_BLADE

    # --- Beam search (GCD-perfect burst packing) ---------------------------
    # PLD's one ceiling-relevant GCD choice is *when* to spend the flexible filler
    # procs — the Atonement chain (Atonement/Supplication/Sepulchre) and the Divine
    # Might Holy Spirit. The greedy spends them ASAP, so they bleed before Fight or
    # Flight; a top parse HOLDS them to land under the 20s FoF window (the burst
    # packing measured as the whole true-gear residual — see the diag script). The
    # beam forks 'spend the proc now vs advance the main combo (hold it)' and the
    # FoF-aware score picks the packing. The fork set is sparse (only at a filler
    # slot), so the search stays ~0.3s even at width 64.

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """Spend-now vs hold-for-FoF, at a filler slot only. Forced single move for
        everything else (magic combo / Goring / a granted finisher). Holding is kept
        LOSSLESS: never advance into a Royal Authority while a chain step is pending
        (Royal restarts the chain, which would drop the held step)."""
        greedy = self.pick_gcd(state, params)
        if state.magic_combo != 0 or state.goring_ready:
            return [greedy]
        if greedy not in (ATONEMENT, SUPPLICATION, SEPULCHRE, HOLY_SPIRIT):
            return [greedy]
        if state.combo_step == 1:
            alt = RIOT_BLADE
        elif state.combo_step == 2:
            alt = ROYAL_AUTHORITY
        else:
            alt = FAST_BLADE
        chain_pending = (state.atonement_ready or state.supplication_ready
                         or state.sepulchre_ready)
        if alt == ROYAL_AUTHORITY and chain_pending:
            return [greedy]
        return [greedy, alt]

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """Top-K key: the exact (Fight-or-Flight-aware) partial score plus an
        admissible credit for any held filler proc (it will be spent, ideally under a
        FoF window) so a holding line isn't pruned before the window arrives. The
        final selection re-scores exactly, so the credit only steers survival. The
        fork set is sparse, so the O(timeline) re-scan here is cheap."""
        base = score_fn(state.timeline, self.final_aux(state), buff_intervals)
        held = 0.0
        if state.sepulchre_ready:
            held += pd.POTENCIES[SEPULCHRE]
        elif state.supplication_ready:
            held += pd.POTENCIES[SUPPLICATION]
        elif state.atonement_ready:
            held += pd.POTENCIES[ATONEMENT]
        if state.divine_might:
            held += pd.POTENCIES[HOLY_SPIRIT]
        return base + held * pd.FIGHT_OR_FLIGHT_MULT

    def beam_signature(self, state: SimState):
        """Diversity-dedup key (engine.beam_search): the future-relevant combo/proc
        state + the FoF/Imperator phase, so equivalent lines collapse to the better
        one and the width holds genuinely distinct packings."""
        return (
            round(state.t, 2), state.combo_step, state.atonement_ready,
            state.supplication_ready, state.sepulchre_ready, state.divine_might,
            state.magic_combo, state.goring_ready, state.blade_of_honor_ready,
            round(max(0.0, state.cd_ready.get(FIGHT_OR_FLIGHT, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(IMPERATOR, 0.0) - state.t), 2),
        )

    # --- Exact-solver seam (optimal.solve_optimal) --------------------------
    # PLD opts in almost for free: `legal_gcds` defaults to the dense `gcd_candidates`
    # (the spend-vs-hold FoF-packing fork), `dominance_key` to `beam_signature`, and
    # the default re-scan `exact_g`/`terminal_g` is already FoF-aware (FoF is derived
    # from the timeline inside `score_delivered_potency`, not an overlay). The
    # dominance vector moves the burst/filler cooldown timers off the categorical key
    # so states that differ only in how ready a cooldown is collapse by Pareto.

    def clone(self, state: SimState) -> SimState:
        new = copy.copy(state)
        new.charges = dict(state.charges)
        new.cd_ready = dict(state.cd_ready)
        new.timeline = list(state.timeline)
        return new

    def dominance_key(self, state: SimState):
        return (
            round(state.t, 2), state.combo_step, state.atonement_ready,
            state.supplication_ready, state.sepulchre_ready, state.divine_might,
            state.magic_combo, state.goring_ready, state.blade_of_honor_ready,
        )

    def dominance_vector(self, state: SimState) -> tuple:
        # Burst + filler cooldowns negated so readier = larger (monotone-good for the
        # buff-agnostic throughput ceiling: more FoF/Imperator/filler uptime is more
        # damage and never forces a GCD — alignment holds ride the beam guard).
        return (
            -round(max(0.0, state.cd_ready.get(FIGHT_OR_FLIGHT, 0.0) - state.t), 2),
            -round(max(0.0, state.cd_ready.get(IMPERATOR, 0.0) - state.t), 2),
            -round(max(0.0, state.cd_ready.get(CIRCLE_OF_SCORN, 0.0) - state.t), 2),
            -round(max(0.0, state.cd_ready.get(EXPIACION, 0.0) - state.t), 2),
            round(state.charges.get(INTERVENE, 0.0), 3),
        )

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Blade of Honor — the granted finisher, fire ASAP.
        if state.blade_of_honor_ready:
            return BLADE_OF_HONOR

        # Fight or Flight — open the burst window (the alignment anchor).
        if state.cd_ready.get(FIGHT_OR_FLIGHT, 0) <= t \
                and not is_forbidden(FIGHT_OR_FLIGHT, t, fw):
            return FIGHT_OR_FLIGHT

        # Imperator — damage + opens the magical combo (alignment anchor).
        if state.magic_combo == 0 and state.cd_ready.get(IMPERATOR, 0) <= t \
                and not is_forbidden(IMPERATOR, t, fw):
            return IMPERATOR

        # Filler oGCDs on cooldown.
        if state.cd_ready.get(CIRCLE_OF_SCORN, 0) <= t:
            return CIRCLE_OF_SCORN
        if state.cd_ready.get(EXPIACION, 0) <= t:
            return EXPIACION
        if state.charges.get(INTERVENE, 0) >= 1:
            return INTERVENE

        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        state.timeline.append((state.t, ability_id))

        # Cooldown / charges (generic).
        apply_cooldown(state, self.cooldowns, ability_id)

        # Main combo
        if ability_id == FAST_BLADE:
            state.combo_step = 1
        elif ability_id == RIOT_BLADE:
            state.combo_step = 2 if state.combo_step == 1 else 0
        elif ability_id == ROYAL_AUTHORITY:
            state.combo_step = 0
            # Grant the Atonement chain + Divine Might (sim always combos right). A
            # fresh Royal RESTARTS the chain at Atonement, so clear any later step:
            # the greedy always spends the chain before re-comboing (so this is a
            # no-op there), but it keeps a beam hold-line from banking chain steps
            # across a Royal — an illegal accumulation that would inflate the ceiling.
            state.atonement_ready = True
            state.supplication_ready = False
            state.sepulchre_ready = False
            state.divine_might = True
        # Atonement chain (each step arms the next)
        elif ability_id == ATONEMENT:
            state.atonement_ready = False
            state.supplication_ready = True
        elif ability_id == SUPPLICATION:
            state.supplication_ready = False
            state.sepulchre_ready = True
        elif ability_id == SEPULCHRE:
            state.sepulchre_ready = False
        elif ability_id in (HOLY_SPIRIT, HOLY_CIRCLE):
            state.divine_might = False
        elif ability_id == FIGHT_OR_FLIGHT:
            state.goring_ready = True     # grants Goring Blade Ready
        elif ability_id == GORING_BLADE:
            state.goring_ready = False
        # Magical combo
        elif ability_id == IMPERATOR:
            state.magic_combo = 1
        elif ability_id == CONFITEOR:
            state.magic_combo = 2
        elif ability_id == BLADE_OF_FAITH:
            state.magic_combo = 3
        elif ability_id == BLADE_OF_TRUTH:
            state.magic_combo = 4
        elif ability_id == BLADE_OF_VALOR:
            state.magic_combo = 0
            state.blade_of_honor_ready = True
        elif ability_id == BLADE_OF_HONOR:
            state.blade_of_honor_ready = False

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            for ph in _SWEEP_PREPULL_HS:
                yield SimParams(max_weaves_per_gcd=mw,
                                prepull_holyspirit=ph,
                                forbidden_windows=extra_forbidden)


_MODEL = PaladinRotationModel()


def _model_for(sim_context):
    """Model bound to this pull's context: a per-player effective GCD (CeilingContext,
    faster-than-constant Skill Speed) and/or carried combo/proc state (PldContext).
    The shared cold-start singleton when neither (byte-identical)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    ctx = payload if isinstance(payload, PldContext) else None
    if gcd is None and ctx is None and not mt_schedule:
        return _MODEL
    return PaladinRotationModel(ctx=ctx, gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn bound to a multi-target N(t) `schedule`
    (each cast valued per-target via `aoe_potency.potency_for` — the cleaving magic
    combo / Imperator / Circle of Scorn / Expiacion scale automatically). Fight or
    Flight is derived from the timeline's FoF casts; buff-aware when given.
    `beam_prune` re-scans via this fn, so it's target-aware too. Empty schedule ->
    single target, byte-identical."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.paladin.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


_score = _make_score()


# Beam width for the GCD-perfect FoF burst-packing search. PLD's fork set is sparse
# (only at filler slots), so the search converges by ~width 32 and runs in ~0.3s; 64
# is safe headroom. `engine.beam_perfect` at width 1 == `engine.perfect`, and it is
# guarded never to fall below the refined greedy ceiling.
_BEAM_WIDTH = 64


def _beam_best(model, score, fight_duration_s, downtime, buff_intervals):
    """The PLD beam ceiling: burst-timing refinement + a beam over the spend-vs-hold
    filler fork (guarded never to fall below the refined greedy ceiling). Kept as the
    exact solver's incumbent seed + the buff-alignment / fallback half of
    `_optimal_best`."""
    return engine.beam_perfect(model, score, fight_duration_s, downtime,
                               buff_intervals, width=_BEAM_WIDTH)


# PLD's fork set is sparse, so the exact solve converges in well under a second; the
# box only guards a pathological pull (then the `_optimal_best` beam guard holds).
_DP_TIME_BOX_S = 30.0


@lru_cache(maxsize=64)
def _dp_throughput_cached(duration_key: float,
                          downtime_tuple: tuple[tuple[float, float], ...],
                          sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    """The provable buff-agnostic throughput optimum (the rank/strict ceiling), cached
    (buff-independent). Sweeps the same params the beam does (max_weaves x prepull Holy
    Spirit), seeds the B&B incumbent from the beam, keeps the best, and guards >= the
    seed so a time-out never falls below it. FoF is scored from the timeline inside
    `score_delivered_potency`, so the exact objective is already FoF-aware."""
    downtime = list(downtime_tuple)
    model = _model_for(sim_context)
    score = _make_score(model.mt_schedule)
    seed_tl, seed_aux = _beam_best(model, score, duration_key, downtime, None)
    best = (score(seed_tl, seed_aux, None), tuple(seed_tl), seed_aux)
    for params in model.sweep_params(()):
        tl, aux, _proven = optimal.solve_optimal(
            model, score, duration_key, downtime, params,
            buff_intervals=None, incumbent=best[0], time_box=_DP_TIME_BOX_S)
        s = score(tl, aux, None)
        if s > best[0]:
            best = (s, tuple(tl), aux)
    return best[1], best[2]


def _dp_throughput(fight_duration_s, downtime, sim_context):
    tl, aux = _dp_throughput_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        sim_context)
    return list(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The PLD ceiling: the exact buff-agnostic throughput optimum (a provable upper
    bound, cached) max'd against the buff-aware beam (raid-burst alignment). For the
    buff-agnostic rank path the DP is >= the beam by construction, so the ceiling is
    provably optimal there — and the outer beam is skipped (the DP path computes the
    identical agnostic beam as its incumbent seed, so re-running it would just double
    the search); neither axis ever regresses."""
    dp_tl, dp_aux = _dp_throughput(fight_duration_s, downtime, sim_context)
    if not buff_intervals:
        return dp_tl, dp_aux
    model = _model_for(sim_context)
    score = _make_score(model.mt_schedule)
    beam_tl, beam_aux = _beam_best(model, score, fight_duration_s, downtime, buff_intervals)
    if score(dp_tl, dp_aux, buff_intervals) >= score(beam_tl, beam_aux, buff_intervals):
        return dp_tl, dp_aux
    return beam_tl, beam_aux


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — the tuple shape
    mirrors the other sims (whose 2nd element is an opaque scalar); PLD has none."""
    if params is None:
        params = SimParams()
    return engine.run_rotation(_model_for(sim_context), fight_duration_s,
                               downtime_windows or [], params)


def simulate_idealized_optimal(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The provably-optimal rotation (buff-aware when given)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: the exact DP+B&B optimum guarded against the FoF
    spend-vs-hold beam (the real upper bound — no greedy floor). Buff-aware when
    `buff_intervals` is given."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the burst forced into the raid-buff windows (the
    comparison lane). Falls back to the throughput optimum when there are no
    party buffs."""
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
