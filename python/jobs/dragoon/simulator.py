"""Idealized DRG rotation — the Dragoon `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the DRG-specific rotation: the branching combo, the
Firstminds' Focus gauge, the Life-of-the-Dragon burst chain, the damage
self-buffs, and the Chaotic Spring DoT. The four `simulate_*` shims at the bottom
bind this model to the engine (names kept so the sidecar / scorer / tests call
them unchanged).

DRG-specific rotation encoded:
- **A branching 5-GCD combo** (why DRG needs the beam). After the starter (True
  Thrust, or Raiden Thrust once Draconian Fire is up) the 2nd GCD forks: **Lance
  Barrage** -> the raw *Heavens' Thrust* combo, or **Spiral Blow** -> the *Chaotic
  Spring* DoT combo. Both converge on the 4th-GCD positional then Drakesbane. The
  fork is exposed in `gcd_candidates`; the beam picks the cadence (refresh the DoT /
  Power Surge now via the DoT combo, or take the higher raw potency now).
- **Self-buff damage multipliers folded INTO the incremental score** (Power Surge
  +10% from Spiral Blow, Lance Charge +10%, Life of the Dragon +15% from Geirskogul):
  because the fork directly controls Power Surge uptime, these ride the timeline (not
  a flat overlay) so the beam-prune key sees the DoT combo's true upkeep value — the
  exact same per-cast math `score_delivered_potency` runs, so the prune key matches
  the final score (modulo raid buffs / tincture, applied at final selection).
- **Chaotic Spring DoT** — scored by time-to-next-refresh, capped at 24s (the SAM
  Higanbana model); refreshed by running the DoT combo, so the refresh IS the fork.
- **Life Surge** — a 2-charge/40s buff placed on the next 460-potency finisher
  (Heavens' Thrust / Drakesbane); the buffed GCD is priced x the crit-only multiplier.
- **Firstminds' Focus** — +1 per Raiden Thrust, spent 2-at-a-time by Wyrmwind Thrust.
- **Battle Litany** (+10% crit) is cast for timeline realism but adds NO potency
  (crit-neutral).
- **The dedicated AoE combo** (Doom Spike / Draconian Fury -> Sonic Thrust ->
  Coerthan Torment, full-to-all) — its own 3-GCD combo track (`aoe_combo_step`, the
  GNB pattern), forked as a WHOLE-COMBO decision at the combo boundary when the
  `MultiTargetContext` schedule says `N >= _AOE_MIN_TARGETS` (a mid-combo bail would
  drop combo bonuses the in-game player would keep — the RPR whole-combo lesson).
  Sonic Thrust maintains Power Surge like Spiral Blow; Draconian Fury feeds Focus
  like Raiden Thrust (a slightly faster cadence — every 3 GCDs vs 5); the line
  forfeits the Chaotic Spring DoT while it runs, which is exactly the value tradeoff
  the beam weighs. Empty schedule -> the fork never opens, byte-identical.

Out of scope for v1 (documented, intentionally not modeled):
- Mid-ST-combo entry into the AoE combo (the fork opens at combo boundaries only —
  worst case ~4 GCDs of delayed AoE entry, an under-credit = <=100%-safe direction).
- Exact Draconian Fire / proc timers (Draconian Fire is a steady-state toggle: every
  ender re-grants it, so the starter is Raiden Thrust after the opener's True Thrust).
- The exact-DP solver seam (RPR ships beam-only; the diverse beam holds the ceiling).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.dragoon import data as dd


# --- Ability IDs (aliased for readability) --------------------------------
TRUE_THRUST        = dd.TRUE_THRUST
RAIDEN_THRUST      = dd.RAIDEN_THRUST
LANCE_BARRAGE      = dd.LANCE_BARRAGE
HEAVENS_THRUST     = dd.HEAVENS_THRUST
FANG_AND_CLAW      = dd.FANG_AND_CLAW
SPIRAL_BLOW        = dd.SPIRAL_BLOW
CHAOTIC_SPRING     = dd.CHAOTIC_SPRING
WHEELING_THRUST    = dd.WHEELING_THRUST
DRAKESBANE         = dd.DRAKESBANE
LANCE_CHARGE       = dd.LANCE_CHARGE
BATTLE_LITANY      = dd.BATTLE_LITANY
LIFE_SURGE         = dd.LIFE_SURGE
GEIRSKOGUL         = dd.GEIRSKOGUL
NASTROND           = dd.NASTROND
STARDIVER          = dd.STARDIVER
STARCROSS          = dd.STARCROSS
DRAGONFIRE_DIVE    = dd.DRAGONFIRE_DIVE
RISE_OF_THE_DRAGON = dd.RISE_OF_THE_DRAGON
HIGH_JUMP          = dd.HIGH_JUMP
MIRAGE_DIVE        = dd.MIRAGE_DIVE
WYRMWIND_THRUST    = dd.WYRMWIND_THRUST
DOOM_SPIKE         = dd.DOOM_SPIKE
DRACONIAN_FURY     = dd.DRACONIAN_FURY
SONIC_THRUST       = dd.SONIC_THRUST
COERTHAN_TORMENT   = dd.COERTHAN_TORMENT


# --- Rotation tuning ------------------------------------------------------
# DRG has NO haste self-buff -> the GCD is the gear-true 2.50 base (per-player Skill
# Speed threads in via `gcd_base_s`, only ever faster -> monotone-safe). ⚠️ confirm
# the BiS GCD on the live calibration (it should land at ~2.50).
DRG_GCD_S = 2.50
STARDIVER_RECAST_S = 30.0
WYRMWIND_RECAST_S = 10.0

# Combo branches.
_RAW = 1   # Lance Barrage -> Heavens' Thrust -> Fang and Claw
_DOT = 2   # Spiral Blow -> Chaotic Spring -> Wheeling Thrust

# The dedicated AoE combo opens at this many targets. Value math (per GCD, both
# lines under the same self-buffs/oGCDs): the AoE cycle deals ~133×N (DF 130 +
# ST 120 + CT 150 over 3 GCDs, full-to-all) + a faster Focus feed (+1/3 GCDs vs
# +1/5) vs the ST line's ~368 + the Chaotic Spring DoT (~36/GCD amortized) ≈
# 400-430. N=3 → ~400×N-side + Focus ≈ break-even-to-ahead; N=2 → ~267, clearly
# behind. The fork is offered to the BEAM from 3 (it still weighs both lines);
# the empirical N-sweep in tests/test_dragoon_aoe.py pins swap-never-worse.
_AOE_MIN_TARGETS = 3

# One full Chaotic Spring DoT in potency (the trailing application is credited this
# much in the incremental beam-prune key, matching score_delivered_potency).
_CHAOTIC_FULL_DOT_P = (dd.CHAOTIC_SPRING_DOT_DURATION_S / dd.CHAOTIC_SPRING_DOT_TICK_S
                       * dd.CHAOTIC_SPRING_DOT_TICK_P)
# Admissible per-Focus credit in the beam-prune key: each banked Focus becomes half a
# Wyrmwind Thrust. Keeps a Focus-banking line alive until the WWT lands.
_FOCUS_PRUNE_VALUE = dd.POTENCIES[WYRMWIND_THRUST] / 2.0

# Hold the burst enablers into raid-buff windows; the payoff oGCDs follow them.
_ANCHORS: tuple[int, ...] = (GEIRSKOGUL, LANCE_CHARGE, DRAGONFIRE_DIVE)
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """DRG picker tunables — no axis beyond the shared knobs (max_weaves /
    forbidden_windows)."""
    pass


@dataclass
class SimState(SimStateBase):
    # Combo state machine.
    combo_step: int = 0          # 0 starter, 1 branch, 2 third, 3 fourth, 4 finisher
    combo_branch: int = 0        # _RAW / _DOT once the 2nd GCD is chosen
    aoe_combo_step: int = 0      # 0 boundary, 1 expect Sonic Thrust, 2 expect Coerthan Torment
    draconian_fire: bool = False  # enders grant it -> the starter becomes Raiden Thrust
    # Self-buff window ends + the DoT.
    dot_end: float = 0.0
    power_surge_end: float = 0.0
    lance_charge_end: float = 0.0
    lotd_end: float = 0.0
    life_surge_armed: bool = False
    # Life-of-the-Dragon burst chain + jump follow-ups.
    nastrond_ready: int = 0      # 0-3 (granted by Geirskogul)
    starcross_ready: bool = False
    dragons_flight: bool = False  # Rise of the Dragon ready
    dive_ready: bool = False     # Mirage Dive ready
    # Firstminds' Focus gauge (0-2).
    focus: int = 0
    # Incremental (buff-agnostic, self-buff-AWARE) running score for the O(1)
    # beam-prune key — the exact per-cast math `score_delivered_potency` runs with
    # raid buffs / tincture off. `_score_flat` = sum of cast potencies x guaranteed
    # crit x the active self-buffs; `_score_fin_dot` = finalized Chaotic Spring DoT of
    # superseded applications; `_score_last_chaotic` = (cast_t, snapshot_mult) of the
    # trailing DoT (credited the full 24s in the prune key, matching the scorer).
    _score_flat: float = 0.0
    _score_fin_dot: float = 0.0
    _score_last_chaotic: tuple[float, float] | None = None


def _self_buff_mult(state: SimState, t: float) -> float:
    """Product of the damage self-buffs active at `t` (set by EARLIER casts)."""
    m = 1.0
    if state.power_surge_end > t:
        m *= dd.POWER_SURGE_MULT
    if state.lance_charge_end > t:
        m *= dd.LANCE_CHARGE_MULT
    if state.lotd_end > t:
        m *= dd.LOTD_MULT
    return m


class DragoonRotationModel(engine.BaseRotationModel):
    cooldowns = dd.COOLDOWNS
    timing = InstantGCD(base_s=DRG_GCD_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Multi-target N(t) schedule (free-splash on the burst oGCDs the ST sim
        # already casts). Empty () -> single target, byte-identical. DRG does NOT
        # fork its GCDs on AoE (no dedicated AoE rotation in v1); the schedule only
        # scales the cleaving oGCDs' splash via `aoe_potency.potency_for`.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed: `gcd_base_s` is the player's measured GCD when
        # faster than the 2.50 constant. None keeps the constant, byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    # --- Lifecycle ---------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {LIFE_SURGE: 2.0}
        state.cd_ready = {LANCE_CHARGE: 0.0, BATTLE_LITANY: 0.0, GEIRSKOGUL: 0.0,
                          DRAGONFIRE_DIVE: 0.0, HIGH_JUMP: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Melee run-in: the first in-fight GCD (True Thrust) lands after the engage
        # delay. DRG has no instant pre-pull cast.
        state.t = dd.JOB_DATA.role_policy.engage_delay_s

    # --- GCD selection -----------------------------------------------------

    def _starter(self, state: SimState) -> int:
        return RAIDEN_THRUST if state.draconian_fire else TRUE_THRUST

    def _branch_greedy(self, state: SimState) -> int:
        """Greedy default at the fork: run the DoT combo when the Chaotic Spring DoT
        (or Power Surge) is about to fall, else the higher-potency raw combo. The
        beam explores both via `gcd_candidates`, so this only needs to be a sane
        baseline that maintains the DoT + Power Surge in steady state."""
        if (state.dot_end - state.t <= dd.CHAOTIC_SPRING_REFRESH_AT_S
                or state.power_surge_end - state.t <= 0.0):
            return SPIRAL_BLOW
        return LANCE_BARRAGE

    def _aoe_starter(self, state: SimState) -> int:
        return DRACONIAN_FURY if state.draconian_fire else DOOM_SPIKE

    def _at_boundary(self, state: SimState) -> bool:
        """True at a clean combo boundary — the only place the ST-vs-AoE line is
        chosen (whole-combo decision; no mid-combo bail)."""
        return state.combo_step == 0 and state.aoe_combo_step == 0

    def pick_gcd(self, state: SimState, params) -> int:
        # Dedicated AoE combo: forced completion once started, entered greedily at
        # a boundary when the live target count clears the crossover.
        if state.aoe_combo_step == 1:
            return SONIC_THRUST
        if state.aoe_combo_step == 2:
            return COERTHAN_TORMENT
        if self._at_boundary(state) and self._n(state.t) >= _AOE_MIN_TARGETS:
            return self._aoe_starter(state)
        step = state.combo_step
        if step == 0:
            return self._starter(state)
        if step == 1:
            return self._branch_greedy(state)
        if step == 2:
            return HEAVENS_THRUST if state.combo_branch == _RAW else CHAOTIC_SPRING
        if step == 3:
            return FANG_AND_CLAW if state.combo_branch == _RAW else WHEELING_THRUST
        return DRAKESBANE  # step 4

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The dense GCD move set. Two forks: the 2nd-GCD ST branch (Lance Barrage
        vs Spiral Blow), and — when the schedule affords `_AOE_MIN_TARGETS` — the
        whole-combo ST-vs-AoE choice at a combo boundary. Every other step is a
        single forced move. Paired with `beam_signature` dedup the beam holds the
        distinct lines and the search reaches the DoT-refresh / burst-alignment /
        AoE-entry cadence a top parse plays."""
        if state.aoe_combo_step:
            return [self.pick_gcd(state, params)]   # forced AoE-combo completion
        if self._at_boundary(state) and self._n(state.t) >= _AOE_MIN_TARGETS:
            return [self._starter(state), self._aoe_starter(state)]
        if state.combo_step == 1:
            return [LANCE_BARRAGE, SPIRAL_BLOW]
        return [self.pick_gcd(state, params)]

    # --- oGCD weaves -------------------------------------------------------

    def _next_is_finisher(self, state: SimState) -> bool:
        """True iff the NEXT GCD is a 460-potency finisher (Heavens' Thrust or
        Drakesbane) — where Life Surge's guaranteed crit converts to the most damage."""
        return ((state.combo_step == 2 and state.combo_branch == _RAW)
                or state.combo_step == 4)

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Lance Charge — the +10% self-buff window. Fired FIRST (top priority) so it
        # covers the burst it shares a 60s cadence with: Geirskogul/LotD and the huge
        # Nastrond/Stardiver/Starcross hits. Ranking it below those (the obvious
        # cooldown order) makes the sim cast them BEFORE Lance Charge → they miss the
        # +10% and the strict ceiling reads ~1.5-2% under a real aligned parse (the
        # refine pass can't fix it: strict has no raid window to align into). Lance
        # Charge is only ever ready at the 60s burst, so firing it ASAP == at burst.
        if state.cd_ready.get(LANCE_CHARGE, 0.0) <= t \
                and not is_forbidden(LANCE_CHARGE, t, fw):
            return LANCE_CHARGE
        # Life of the Dragon chain — time-sensitive (inside the 20s window).
        if state.nastrond_ready > 0:
            return NASTROND
        if state.starcross_ready:
            return STARCROSS
        if state.lotd_end > t and state.cd_ready.get(STARDIVER, 0.0) <= t:
            return STARDIVER
        # Geirskogul — the burst enabler (LotD +15% + 1 Nastrond).
        if state.cd_ready.get(GEIRSKOGUL, 0.0) <= t \
                and not is_forbidden(GEIRSKOGUL, t, fw):
            return GEIRSKOGUL
        # Dragonfire Dive + Rise of the Dragon follow-up.
        if state.dragons_flight:
            return RISE_OF_THE_DRAGON
        if state.cd_ready.get(DRAGONFIRE_DIVE, 0.0) <= t \
                and not is_forbidden(DRAGONFIRE_DIVE, t, fw):
            return DRAGONFIRE_DIVE
        # High Jump + Mirage Dive follow-up.
        if state.dive_ready:
            return MIRAGE_DIVE
        if state.cd_ready.get(HIGH_JUMP, 0.0) <= t:
            return HIGH_JUMP
        # Battle Litany — party crit (0 potency; cast for timeline realism).
        if state.cd_ready.get(BATTLE_LITANY, 0.0) <= t \
                and not is_forbidden(BATTLE_LITANY, t, fw):
            return BATTLE_LITANY
        # Life Surge — armed before a 460 finisher so the guaranteed crit lands there.
        if state.charges.get(LIFE_SURGE, 0.0) >= 1.0 and not state.life_surge_armed \
                and self._next_is_finisher(state) \
                and not is_forbidden(LIFE_SURGE, t, fw):
            return LIFE_SURGE
        # Wyrmwind Thrust — Focus dump (2 stacks).
        if state.focus >= dd.FOCUS_CAP \
                and state.cd_ready.get(WYRMWIND_THRUST, 0.0) <= t:
            return WYRMWIND_THRUST
        return None

    # --- Cast transitions --------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental (buff-agnostic, self-buff-aware) running score — the per-cast
        # math `score_delivered_potency` runs with raid buffs / tincture off. The
        # per-target potency (`potency_for`) scales cleaving oGCDs by the live target
        # count; at N==1 it equals POTENCIES.get, byte-identical.
        base_p = potency_for(ability_id, self._n(t), dd.JOB_DATA)
        if base_p > 0:
            m = _self_buff_mult(state, t)
            if state.life_surge_armed and ability_id in dd.GCD_WEAPONSKILLS:
                m *= dd.GUARANTEED_CRIT_MULT
                state.life_surge_armed = False
            state._score_flat += base_p * m

        # Chaotic Spring DoT (time-to-next; snapshot self-buffs at cast time).
        if ability_id == CHAOTIC_SPRING:
            if state._score_last_chaotic is not None:
                last_t, last_m = state._score_last_chaotic
                gap = min(dd.CHAOTIC_SPRING_DOT_DURATION_S, max(0.0, t - last_t))
                state._score_fin_dot += (gap / dd.CHAOTIC_SPRING_DOT_TICK_S
                                         * dd.CHAOTIC_SPRING_DOT_TICK_P * last_m)
            state._score_last_chaotic = (t, _self_buff_mult(state, t))
            state.dot_end = t + dd.CHAOTIC_SPRING_DOT_DURATION_S

        # Self-buff window updates (AFTER scoring this cast, so a buff never amps the
        # cast that grants it).
        if ability_id in (SPIRAL_BLOW, dd.SONIC_THRUST):
            state.power_surge_end = t + dd.POWER_SURGE_DURATION_S
        elif ability_id == LANCE_CHARGE:
            state.lance_charge_end = t + dd.LANCE_CHARGE_DURATION_S
        elif ability_id == GEIRSKOGUL:
            state.lotd_end = t + dd.LOTD_DURATION_S
            state.nastrond_ready = dd.NASTROND_PER_LOTD
        elif ability_id == LIFE_SURGE:
            state.life_surge_armed = True

        # Combo progression.
        self._advance_combo(state, ability_id)

        # Firstminds' Focus gauge.
        if ability_id in dd.FOCUS_GENERATORS:
            state.focus = min(dd.FOCUS_CAP, state.focus + dd.FOCUS_GENERATORS[ability_id])
        if ability_id in dd.FOCUS_SPENDERS:
            state.focus = max(0, state.focus - dd.FOCUS_SPENDERS[ability_id])

        # Burst-chain follow-up state.
        if ability_id == NASTROND:
            state.nastrond_ready = max(0, state.nastrond_ready - 1)
        elif ability_id == STARDIVER:
            state.starcross_ready = True
            state.cd_ready[STARDIVER] = t + STARDIVER_RECAST_S
        elif ability_id == STARCROSS:
            state.starcross_ready = False
        elif ability_id == DRAGONFIRE_DIVE:
            state.dragons_flight = True
        elif ability_id == RISE_OF_THE_DRAGON:
            state.dragons_flight = False
        elif ability_id == HIGH_JUMP:
            state.dive_ready = True
        elif ability_id == MIRAGE_DIVE:
            state.dive_ready = False
        elif ability_id == WYRMWIND_THRUST:
            state.cd_ready[WYRMWIND_THRUST] = t + WYRMWIND_RECAST_S

        # Generic cooldown / charges (COOLDOWNS only — Lance Charge / Battle Litany /
        # Life Surge / Geirskogul / Dragonfire Dive / High Jump).
        apply_cooldown(state, self.cooldowns, ability_id)

    def _advance_combo(self, state: SimState, ability_id: int) -> None:
        # The dedicated AoE combo runs its OWN 3-GCD track (the GNB pattern); the
        # in-game combo chain is one timer, so starting either line resets the other.
        if ability_id in (DOOM_SPIKE, DRACONIAN_FURY):
            state.aoe_combo_step = 1
            state.combo_step = 0
            state.combo_branch = 0
            if ability_id == DRACONIAN_FURY:
                state.draconian_fire = False   # the proc is consumed
            return
        if ability_id == SONIC_THRUST:
            state.aoe_combo_step = 2
            return
        if ability_id == COERTHAN_TORMENT:
            state.aoe_combo_step = 0
            state.draconian_fire = True        # the AoE ender grants Draconian Fire
            return
        if ability_id in (TRUE_THRUST, RAIDEN_THRUST):
            state.combo_step = 1
            state.combo_branch = 0
            state.aoe_combo_step = 0
            if ability_id == RAIDEN_THRUST:
                state.draconian_fire = False   # the proc is consumed
        elif ability_id == LANCE_BARRAGE:
            state.combo_step = 2
            state.combo_branch = _RAW
        elif ability_id == SPIRAL_BLOW:
            state.combo_step = 2
            state.combo_branch = _DOT
        elif ability_id in (HEAVENS_THRUST, CHAOTIC_SPRING):
            state.combo_step = 3
        elif ability_id in (FANG_AND_CLAW, WHEELING_THRUST):
            state.combo_step = 4
            state.draconian_fire = True        # enders grant Draconian Fire
        elif ability_id == DRAKESBANE:
            state.combo_step = 0
            state.draconian_fire = True

    # --- Beam search seam --------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental self-buff-aware running score (no
        re-scan). Equals `score_delivered_potency(... raid buffs/tincture off)` plus an
        admissible trailing-DoT credit (the open Chaotic Spring credited the full 24s,
        so a DoT-combo line isn't pruned before its DoT pays off) and a banked-Focus
        credit. Buff-agnostic by design — the engine re-scores survivors under
        `buff_intervals`, so ranking on raw potency only steers survival."""
        base = state._score_flat + state._score_fin_dot
        if state._score_last_chaotic is not None:
            base += _CHAOTIC_FULL_DOT_P * state._score_last_chaotic[1]
        return base + state.focus * _FOCUS_PRUNE_VALUE

    def beam_signature(self, state: SimState):
        """Lossless diversity-dedup key (engine.beam_search): the full future-relevant
        state, so two beams that reach an identical state collapse to the
        higher-scoring one, freeing the width for genuinely distinct lines."""
        return (
            round(state.t, 2), state.combo_step, state.combo_branch,
            state.aoe_combo_step, state.draconian_fire,
            round(max(0.0, state.dot_end - state.t), 2),
            round(max(0.0, state.power_surge_end - state.t), 2),
            round(max(0.0, state.lance_charge_end - state.t), 2),
            round(max(0.0, state.lotd_end - state.t), 2),
            state.nastrond_ready, state.starcross_ready, state.dragons_flight,
            state.dive_ready, state.life_surge_armed, state.focus,
            round(state.charges.get(LIFE_SURGE, 0.0), 2),
            round(max(0.0, state.cd_ready.get(GEIRSKOGUL, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(LANCE_CHARGE, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(BATTLE_LITANY, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(DRAGONFIRE_DIVE, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(HIGH_JUMP, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(STARDIVER, 0.0) - state.t), 2),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding -----------------------------------

def _model_for(duration_s: float, sim_context) -> DragoonRotationModel:
    """Build a model bound to this run's per-pull context. After unwrapping any
    per-player effective GCD (CeilingContext) then any `MultiTargetContext` (the
    free-splash N(t) schedule), DRG has no further per-pull scalar payload (its Focus
    entry-gauge is negligible), so the remaining payload is ignored."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
    return DragoonRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    free-splash N(t) `schedule` (each cleaving oGCD valued per-target). Buff-aware when
    given. Empty schedule -> single target, byte-identical. Lazy import to avoid a
    scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.dragoon.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


_score = _make_score()


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(duration_key, sim_context)
    score = _make_score(model.mt_schedule)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, score, duration_key, list(downtime_tuple), buff_intervals,
        width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The DRG ceiling: the diverse beam over the combo-branch fork on top of the
    burst-timing refinement. (RPR ships beam-only; the exact DP seam is deferred —
    the diverse beam holds the ceiling.)"""
    tl, aux = _perfect_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        tuple((round(s, 3), round(e, 3), round(m, 4))
              for s, e, m in buff_intervals) if buff_intervals else None,
        sim_context)
    return list(tl), aux


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once (greedy baseline). Returns (timeline, 0) — DRG
    has no pet/payload scalar, so aux is always 0."""
    if params is None:
        params = SimParams()
    model = _model_for(fight_duration_s, sim_context)
    return engine.run_rotation(model, fight_duration_s, downtime_windows or [], params)


def simulate_idealized_optimal(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling (buff-aware when given)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Alias for the optimal ceiling — the beam over the GCD forks on top of the
    refined burst timing (the real upper bound; no greedy floor)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows.
    Falls back to the throughput optimum when there are no party buffs."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
