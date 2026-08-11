"""Idealized Viper rotation — the VPR `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep and local-search
refinement all live in `jobs/_core/sim/engine.py`. This module supplies only the
VPR-specific rotation: the three-gauge state (Serpent Offering / Rattling Coil /
Anguine Tribute), the priority pickers, the per-cast transitions, and the oGCD
weave queue. The four `simulate_*` shims at the bottom bind this model to the
engine (names kept so the sidecar / scorer / tests call them unchanged).

Viper is a fast, deterministic instant-melee job with NO RNG procs and no real
GCD forks, so — like RPR — its ceiling is `engine.perfect` (greedy + oGCD-burst
refinement); no beam / exact-DP seam is wired (there is nothing to fork on). The
two job-specific touches vs the RPR template:

- **Swiftscaled** (-15% recast self-buff, ~100% uptime) is baked into the hasted
  GCD constant `VPR_GCD_S`; every GCD's true recast — the fast ~1.7s Reawakened
  Generations, the slow 3.0s Coils / Ouroboros — comes from the per-ability
  `gcd_duration` (`data.GCD_RECAST_MULT`).
- **Hunter's Instinct** (+10% damage self-buff) is NOT here — it's a
  `coverage_intervals` overlay in scoring.py (the idealized side assumes full
  coverage), measured on the player's own aura (jobs/viper/buffs.py).

RPR-style gauge loop encoded:
- ST combo (starter -> Sting -> positional finisher, each finisher +10 Serpent
  Offering and a Death Rattle oGCD).
- Vicewinder (2 charges / 40s, +1 Rattling Coil) -> Hunter's Coil -> Swiftskin's
  Coil (each +5 offering, each double-weaving Twinfang + Twinblood Bite).
- Uncoiled Fury (ranged GCD) spends a Rattling Coil -> Uncoiled Twinfang/Twinblood.
- Reawaken (50 Serpent Offering, or free under Ready to Reawaken from Serpent's
  Ire) -> 5 Anguine Tribute -> 4 Generations (each a Legacy oGCD) -> Ouroboros.
- Serpent's Ire (120s) -> +1 coil + Ready to Reawaken (a free Reawaken).

Out of scope for v1 (documented, intentionally not modeled):
- AoE buttons (Maws / Bites / Vicepit / Dens / Threshes) — single-target +
  free-splash cleave ceiling (the current-tier finding; see data.SPLASH_POTENCIES).
- Positional hit/miss (idealized always hits); exact buff timers (Swiftscaled in
  the GCD constant, Hunter's Instinct a full-coverage overlay).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.viper import data as vd


# --- Ability IDs (aliased from data for readability) ----------------------
STEEL_FANGS        = vd.STEEL_FANGS
REAVING_FANGS      = vd.REAVING_FANGS
HUNTERS_STING      = vd.HUNTERS_STING
SWIFTSKINS_STING   = vd.SWIFTSKINS_STING
FLANKSTING_STRIKE  = vd.FLANKSTING_STRIKE
FLANKSBANE_FANG    = vd.FLANKSBANE_FANG
HINDSTING_STRIKE   = vd.HINDSTING_STRIKE
HINDSBANE_FANG     = vd.HINDSBANE_FANG
DEATH_RATTLE       = vd.DEATH_RATTLE
VICEWINDER         = vd.VICEWINDER
HUNTERS_COIL       = vd.HUNTERS_COIL
SWIFTSKINS_COIL    = vd.SWIFTSKINS_COIL
TWINFANG_BITE      = vd.TWINFANG_BITE
TWINBLOOD_BITE     = vd.TWINBLOOD_BITE
UNCOILED_FURY      = vd.UNCOILED_FURY
UNCOILED_TWINFANG  = vd.UNCOILED_TWINFANG
UNCOILED_TWINBLOOD = vd.UNCOILED_TWINBLOOD
REAWAKEN           = vd.REAWAKEN
FIRST_GENERATION   = vd.FIRST_GENERATION
SECOND_GENERATION  = vd.SECOND_GENERATION
THIRD_GENERATION   = vd.THIRD_GENERATION
FOURTH_GENERATION  = vd.FOURTH_GENERATION
OUROBOROS          = vd.OUROBOROS
FIRST_LEGACY       = vd.FIRST_LEGACY
SECOND_LEGACY      = vd.SECOND_LEGACY
THIRD_LEGACY       = vd.THIRD_LEGACY
FOURTH_LEGACY      = vd.FOURTH_LEGACY
SERPENTS_IRE       = vd.SERPENTS_IRE

# The Reawakened combo, in order, with the Legacy oGCD each Generation grants.
_REAWAKEN_GCDS: tuple[int, ...] = (
    FIRST_GENERATION, SECOND_GENERATION, THIRD_GENERATION, FOURTH_GENERATION,
    OUROBOROS,
)
_LEGACY_FOR: dict[int, int] = {
    FIRST_GENERATION: FIRST_LEGACY, SECOND_GENERATION: SECOND_LEGACY,
    THIRD_GENERATION: THIRD_LEGACY, FOURTH_GENERATION: FOURTH_LEGACY,
}
# The reduced-recast Reawakened GCDs (for gcd_duration + the clip/inference skip).
REAWAKEN_FAST_IDS: frozenset[int] = frozenset(_REAWAKEN_GCDS)


# --- Rotation tuning ------------------------------------------------------
# Viper's achievable SUSTAINED normal-GCD cadence. NOT the bare Swiftscaled recast
# (2.50 x 0.85 = 2.125): VPR double-weaves almost every GCD (Death Rattle, the two
# Twin Bites per Coil, two Uncoiled Twins, a Legacy per Generation), and an oGCD's
# animation lock leaves a real fight running materially slower than the single-weave
# floor. Top parses' single-weave GCDs sit at ~2.13 (the gear floor) but their
# *sustained* normal cadence is ~2.30 (the double-weave mean) — and the rotation
# can't avoid the double-weaves, so 2.13 is an unreachable ghost. Calibrated 2.28
# against the live top-10 x whole tier (0 over 100.5%, top parses ~97-99%). Because
# the cadence is weave-bound, not gear-bound, the per-player Skill-Speed inference is
# DISABLED for VPR (scoring.gcd_constant = None) — it would measure the tight floor
# and over-credit. ⚠️ re-tune per tier vs observed top-VPR GCD counts.
VPR_GCD_S = 2.125
# The Reawakened combo runs at PER-ABILITY recasts (the Generations at a reduced
# ~1.7s — 2.0s base x Swiftscaled, measured from the live opener — and Ouroboros at
# the slow 3.0s Vicewinder-line recast), all from `data.GCD_RECAST_MULT` in
# `gcd_duration` below; there is no single Reawaken recast constant.


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """VPR picker tunables — only the shared knobs (max_weaves / forbidden_windows)
    in v1. The rotation is deterministic, so there is no job-local sweep axis."""
    pass


@dataclass
class SimState(SimStateBase):
    offering: int = 0            # Serpent Offering (0-100) -> Reawaken
    rattling: int = 0            # Rattling Coil (0-3) -> Uncoiled Fury
    anguine: int = 0             # Anguine Tribute (0-5) — spent in the Reawaken combo
    reawaken_step: int = -1      # -1 idle; 0-4 -> next _REAWAKEN_GCDS index
    ready_to_reawaken: bool = False   # free Reawaken (from Serpent's Ire)
    combo_step: int = 0          # ST combo: 0 starter, 1 Sting, 2 finisher
    vice_step: int = 0           # Vicewinder combo: 0 idle, 1 -> Hunter's Coil, 2 -> Swiftskin's Coil
    pending_ogcds: tuple[int, ...] = ()   # oGCD weave queue (Death Rattle / Twins / Legacies)
    # Maintained self-buffs (set by the granting GCDs; never expire in-sim — the
    # rotation keeps them up, and scoring models them as always-on). They exist only
    # to gate the OPENER's first Reawaken behind the buff setup, so the Reawaken
    # burst lands buffed; in steady state both are always up (a no-op).
    swiftscaled: bool = False        # haste (Swiftskin's Sting / Coil)
    hunters_instinct: bool = False   # +10% damage (Hunter's Sting / Coil)
    # Cosmetic alternations (potency-neutral; keep the timeline realistic).
    starter_steel: bool = True
    sting_hunters: bool = True
    finisher_flank: bool = True


# --- Refinement anchors ---------------------------------------------------
# The greedy picker fires Serpent's Ire / Reawaken as soon as they're available;
# the refinement nudges the 2-min burst (Serpent's Ire + the Reawaken it frees)
# into raid-buff windows.
_PERFECT_ANCHORS: tuple[int, ...] = (SERPENTS_IRE, REAWAKEN)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (SERPENTS_IRE, REAWAKEN)

# Sweep axis (kept job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)


# --- The VPR rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    vd.JOB_DATA.tincture_main_stat, vd.JOB_DATA.tincture_role_coeff)


class ViperRotationModel(engine.BaseRotationModel):
    cooldowns = vd.COOLDOWNS
    timing = InstantGCD(base_s=VPR_GCD_S)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Multi-target N(t) schedule (the cleave-aware ceiling): free-splash is
        # credited by the scorer's per-target valuation; the ST rotation itself is
        # unchanged at every N (Viper has no dedicated AoE buttons modeled yet), so
        # this only flows into `_make_score`. Empty () -> single target.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed (threaded only when faster than the constant):
        # scales the base GCD and the Reawakened GCD by the same haste factor. None
        # keeps the tier constants, byte-identical.
        self.gcd_base_s = VPR_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != VPR_GCD_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {VICEWINDER: 2.0}
        state.cd_ready = {SERPENTS_IRE: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Melee engage delay: the in-fight loop starts after the run-in to the boss
        # (Slither dash in the opener); no ranged precast (Uncoiled Fury needs a
        # Rattling Coil the pull hasn't generated yet).
        state.t = vd.JOB_DATA.role_policy.engage_delay_s

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Per-ability recast. VPR mixes GCD speeds (2.0s Generations, 2.2s Reawaken,
        # 2.5s ST combo, 3.0s Coils/Vicewinder/Ouroboros, 3.5s Uncoiled Fury). A
        # single blended constant over-fits the SLOW GCDs (it runs Uncoiled Fury /
        # Ouroboros far too fast → the ceiling packs extra GCDs no player can), so the
        # cadence must be per-ability: gcd_base_s (the standard 2.5s GCD) x the recast
        # multiple. Scales with gcd_base_s for per-player Skill Speed.
        return self.gcd_base_s * vd.GCD_RECAST_MULT.get(gcd_id, 1.0)

    # --- GCD pick ---------------------------------------------------------
    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t

        # 1. Inside the Reawaken combo: the forced Generation -> ... -> Ouroboros
        #    sequence (each spends an Anguine Tribute, at the reduced recast).
        if state.reawaken_step >= 0:
            return _REAWAKEN_GCDS[state.reawaken_step]

        # 2. Vicewinder coil combo continuation (Hunter's Coil -> Swiftskin's Coil).
        #    Finish the coils before anything else — they grant the self-buffs and
        #    are the highest-value GCDs, so Reawaken must never interrupt them.
        if state.vice_step == 1:
            return HUNTERS_COIL
        if state.vice_step == 2:
            return SWIFTSKINS_COIL

        # 3. Reawaken — the top-value burst. Free under Ready to Reawaken (Serpent's
        #    Ire), else 50 Serpent Offering. HELD until BOTH self-buffs are up so the
        #    burst (the single biggest chunk of potency) always lands buffed: in the
        #    opener this defers the free Reawaken behind the Swiftscaled / Hunter's
        #    Instinct setup combo, matching real play; in steady state both are always
        #    maintained, so the gate is a no-op. Then fire ASAP so offering never overcaps.
        if (state.ready_to_reawaken or state.offering >= 50) \
                and state.swiftscaled and state.hunters_instinct \
                and not is_forbidden(REAWAKEN, t, params.forbidden_windows):
            return REAWAKEN

        # 4. Dump a Rattling Coil before it would overcap (so the next Vicewinder /
        #    Serpent's Ire coil isn't wasted).
        if state.rattling >= vd.RATTLING_CAP:
            return UNCOILED_FURY

        # 5. Vicewinder when a charge is up and the +1 coil won't overcap — the coil
        #    pair (680 each) + offering is the highest-throughput GCD line.
        if state.charges.get(VICEWINDER, 0) >= 1 and state.rattling < vd.RATTLING_CAP:
            return VICEWINDER

        # 6. Uncoiled Fury as high-value filler (680 + two 170 oGCDs) whenever a coil
        #    is banked — beats the 300 ST combo filler.
        if state.rattling >= 1:
            return UNCOILED_FURY

        # 7. ST combo — the Serpent Offering engine (finisher +10).
        return self._combo_gcd(state)

    def _combo_gcd(self, state: SimState) -> int:
        if state.combo_step == 0:
            return STEEL_FANGS if state.starter_steel else REAVING_FANGS
        if state.combo_step == 1:
            return HUNTERS_STING if state.sting_hunters else SWIFTSKINS_STING
        # Finisher: route by the Sting just used, alternating flank/rear for venom.
        if state.sting_hunters:
            return FLANKSTING_STRIKE if state.finisher_flank else HINDSTING_STRIKE
        return FLANKSBANE_FANG if state.finisher_flank else HINDSBANE_FANG

    # --- oGCD pick --------------------------------------------------------
    def pick_ogcd(self, state: SimState, params):
        t = state.t
        # Serpent's Ire — 120s burst (+1 coil + a free Reawaken). Fire on cooldown;
        # the refinement holds it into raid-buff windows via the anchors.
        if state.cd_ready.get(SERPENTS_IRE, 0) <= t \
                and not is_forbidden(SERPENTS_IRE, t, params.forbidden_windows):
            return SERPENTS_IRE
        # The weave queue: Death Rattle / Twin Bites / Uncoiled Twins / Legacies,
        # in the order their GCDs granted them.
        if state.pending_ogcds:
            return state.pending_ogcds[0]
        return None

    # --- transitions ------------------------------------------------------
    def clone(self, state: SimState) -> SimState:
        new = copy.copy(state)
        new.charges = dict(state.charges)
        new.cd_ready = dict(state.cd_ready)
        new.timeline = list(state.timeline)
        return new

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Serpent Offering (generators only here; Reawaken's spend is handled in its
        # branch so a FREE Reawaken under Ready to Reawaken costs nothing).
        gain = vd.OFFERING_GENERATORS.get(ability_id)
        if gain:
            state.offering = min(vd.OFFERING_CAP, state.offering + gain)
        # Rattling Coil.
        rg = vd.RATTLING_GENERATORS.get(ability_id)
        if rg:
            state.rattling = min(vd.RATTLING_CAP, state.rattling + rg)
        rs = vd.RATTLING_SPENDERS.get(ability_id)
        if rs:
            state.rattling = max(0, state.rattling - rs)

        # Cooldown / charges (generic): Vicewinder charges, Serpent's Ire.
        apply_cooldown(state, self.cooldowns, ability_id)

        # Per-ability effects + the oGCD weave queue.
        if ability_id == SERPENTS_IRE:
            state.ready_to_reawaken = True

        elif ability_id == VICEWINDER:
            state.vice_step = 1
        elif ability_id == HUNTERS_COIL:
            state.vice_step = 2
            state.hunters_instinct = True   # grants Hunter's Instinct (+10% dmg)
            state.pending_ogcds = state.pending_ogcds + (TWINFANG_BITE, TWINBLOOD_BITE)
        elif ability_id == SWIFTSKINS_COIL:
            state.vice_step = 0
            state.swiftscaled = True         # grants Swiftscaled (haste)
            state.pending_ogcds = state.pending_ogcds + (TWINFANG_BITE, TWINBLOOD_BITE)

        elif ability_id == UNCOILED_FURY:
            state.pending_ogcds = state.pending_ogcds + (UNCOILED_TWINFANG,
                                                         UNCOILED_TWINBLOOD)

        elif ability_id == REAWAKEN:
            if state.ready_to_reawaken:
                state.ready_to_reawaken = False        # free cast
            else:
                state.offering = max(0, state.offering - 50)
            state.anguine = vd.ANGUINE_ON_REAWAKEN
            state.reawaken_step = 0
        elif ability_id in _LEGACY_FOR:                # a Generation
            state.anguine = max(0, state.anguine - 1)
            state.pending_ogcds = state.pending_ogcds + (_LEGACY_FOR[ability_id],)
            state.reawaken_step = state.reawaken_step + 1
        elif ability_id == OUROBOROS:
            state.anguine = max(0, state.anguine - 1)
            state.reawaken_step = -1                    # exit the Reawaken combo

        # ST combo progression + the finisher's Death Rattle oGCD.
        elif ability_id in (STEEL_FANGS, REAVING_FANGS):
            state.combo_step = 1
        elif ability_id in (HUNTERS_STING, SWIFTSKINS_STING):
            state.combo_step = 2
            state.sting_hunters = (ability_id == HUNTERS_STING)
            # The Sting also grants its self-buff (the ST-combo path to coverage).
            if ability_id == HUNTERS_STING:
                state.hunters_instinct = True
            else:
                state.swiftscaled = True
        elif ability_id in (FLANKSTING_STRIKE, FLANKSBANE_FANG,
                            HINDSTING_STRIKE, HINDSBANE_FANG):
            state.combo_step = 0
            state.pending_ogcds = state.pending_ogcds + (DEATH_RATTLE,)
            # Advance the cosmetic alternations for the next combo.
            state.starter_steel = not state.starter_steel
            state.sting_hunters = not state.sting_hunters
            state.finisher_flank = not state.finisher_flank

        # Pop the weave queue when an oGCD from it resolves.
        elif state.pending_ogcds and ability_id == state.pending_ogcds[0]:
            state.pending_ogcds = state.pending_ogcds[1:]

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


_MODEL = ViperRotationModel()


def _model_for(sim_context) -> ViperRotationModel:
    """The model bound to this run's per-pull context — a per-player effective GCD
    (CeilingContext, faster-than-constant Skill Speed) and/or a `MultiTargetContext`
    (the cleave N(t) schedule). `None`/none -> the default model, byte-identical."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    return ViperRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    cleave N(t) `schedule` (each cast valued per-target via `aoe_potency.potency_for`,
    which credits the SPLASH_POTENCIES set). Hunter's-Instinct-agnostic (a constant
    x1.10 on every candidate, so it doesn't change the argmax) and buff-aware when
    given. Empty schedule -> single target, byte-identical."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.viper.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests / canonical helpers call `_score`).
_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — VPR has no pet/
    payload scalar, so aux is always 0."""
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
    """Sweep SimParams, return the highest-scoring (timeline, 0)."""
    model = _model_for(sim_context)
    timeline, aux, _params, _score_v = engine.sweep_best(
        model, _make_score(model.mt_schedule), fight_duration_s,
        downtime_windows or [], buff_intervals=buff_intervals)
    return timeline, aux


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: sweep + local-search refinement of the oGCD-burst timing,
    buff-aware when `buff_intervals` is given. (No beam/DP — VPR's loop is
    deterministic; there is no GCD fork to search, so width-1 perfect IS optimal.)"""
    model = _model_for(sim_context)
    return engine.perfect(model, _make_score(model.mt_schedule), fight_duration_s,
                          downtime_windows or [], buff_intervals)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows.
    Falls back to the throughput optimum when there are no party buffs."""
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
