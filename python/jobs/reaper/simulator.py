"""Idealized RPR rotation — the Reaper `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the RPR-specific rotation: the gauge state, the
priority pickers, the per-cast state transitions, and the Soulsow downtime
re-arm. The four `simulate_*` functions at the bottom are thin shims that bind
this model to the engine (kept under their original names so the sidecar, the
scorer and the tests call them unchanged).

RPR-specific rotation encoded:
- Two gauges: Soul (combo / Soul Slice build it; Blood Stalk / Gluttony spend
  50) and Shroud (Soul Reaver GCDs build it 10 at a time; Enshroud spends 50).
- Soul Reaver loop: Blood Stalk (or Unveiled, when Enhanced) -> Soul Reaver ->
  Gibbet/Gallows (positional GCD, +10 shroud), alternating for the Enhanced bonus.
- Gluttony -> 2 Executioner GCDs (Exec Gibbet/Gallows, +10 shroud each).
- Enshroud (shroud 50, or FREE via Plentiful Harvest's Ideal Host) -> 5 Lemure
  Shroud -> 4x Void/Cross Reaping (1.5s recast) + Communio, Lemure's Slice
  oGCDs draining Void Shroud, then Communio -> Perfectio (after Occulta).
- Death's Design upkeep: Shadow of Death cast on cadence (occupies a GCD, +10
  soul). The 10% amp itself is applied in scoring, not here.
- Soulsow -> Harvest Moon downtime re-arm (the engine's on_downtime_window hook).

Out of scope for v1 (documented, intentionally not modeled):
- Exact Enhanced-buff timers (treated as immediate alternation state).
- AoE actions (Soul Scythe / Guillotine / Grim Swathe) — single-target ceiling.
- Frame-perfect animation timing; positional hit/miss (idealized always hits).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from jobs._core.entry_gauge import EntryState, seed_entry_gauge
from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.reaper import data as rd


# --- Ability IDs (aliased from data for readability) ----------------------
SLICE             = rd.SLICE
WAXING_SLICE      = rd.WAXING_SLICE
INFERNAL_SLICE    = rd.INFERNAL_SLICE
SHADOW_OF_DEATH   = rd.SHADOW_OF_DEATH
SOUL_SLICE        = rd.SOUL_SLICE
BLOOD_STALK       = rd.BLOOD_STALK
UNVEILED_GIBBET   = rd.UNVEILED_GIBBET
UNVEILED_GALLOWS  = rd.UNVEILED_GALLOWS
GLUTTONY          = rd.GLUTTONY
GIBBET            = rd.GIBBET
GALLOWS           = rd.GALLOWS
EXEC_GIBBET       = rd.EXEC_GIBBET
EXEC_GALLOWS      = rd.EXEC_GALLOWS
ENSHROUD          = rd.ENSHROUD
VOID_REAPING      = rd.VOID_REAPING
CROSS_REAPING     = rd.CROSS_REAPING
LEMURES_SLICE     = rd.LEMURES_SLICE
SACRIFICIUM       = rd.SACRIFICIUM
COMMUNIO          = rd.COMMUNIO
PERFECTIO         = rd.PERFECTIO
ARCANE_CIRCLE     = rd.ARCANE_CIRCLE
PLENTIFUL_HARVEST = rd.PLENTIFUL_HARVEST
SOULSOW           = rd.SOULSOW
HARVEST_MOON      = rd.HARVEST_MOON
HARPE             = rd.HARPE
# AoE line (cast in multi-target windows; gauge-equivalent to the ST counterparts).
SPINNING_SCYTHE   = rd.SPINNING_SCYTHE
NIGHTMARE_SCYTHE  = rd.NIGHTMARE_SCYTHE
WHORL_OF_DEATH    = rd.WHORL_OF_DEATH
GRIM_SWATHE       = rd.GRIM_SWATHE
GUILLOTINE        = rd.GUILLOTINE
GRIM_REAPING      = rd.GRIM_REAPING
LEMURES_SCYTHE    = rd.LEMURES_SCYTHE
EXEC_GUILLOTINE   = rd.EXEC_GUILLOTINE
SOUL_SCYTHE       = rd.SOUL_SCYTHE

# Single-target -> AoE-counterpart substitutions. RPR's AoE buttons feed the SAME
# soul/shroud economy as their ST counterparts (gauge-equivalent), so the optimal
# choice at a slot is closed-form: take whichever out-potencies at the live target
# count. The main combo is handled separately (3-step ST vs 2-step AoE, decided by
# per-GCD average). Applied only when N>=2; N==1 leaves the ST pick untouched.
_AOE_GCD_SWAP: dict[int, int] = {
    SHADOW_OF_DEATH: WHORL_OF_DEATH,
    SOUL_SLICE:      SOUL_SCYTHE,
    GIBBET:          GUILLOTINE,
    GALLOWS:         GUILLOTINE,
    EXEC_GIBBET:     EXEC_GUILLOTINE,
    EXEC_GALLOWS:    EXEC_GUILLOTINE,
    VOID_REAPING:    GRIM_REAPING,
    CROSS_REAPING:   GRIM_REAPING,
}
_AOE_OGCD_SWAP: dict[int, int] = {
    BLOOD_STALK:      GRIM_SWATHE,
    UNVEILED_GIBBET:  GRIM_SWATHE,
    UNVEILED_GALLOWS: GRIM_SWATHE,
    LEMURES_SLICE:    LEMURES_SCYTHE,
}


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S         = 2.5     # RPR base GCD (no SkS gear-aware adjustment in v1)
GCD_ENSHROUD_S     = 1.5     # Void / Cross Reaping recast inside Enshroud
# Death's Design refresh: refresh when remaining <= this. The debuff is 30s,
# bankable to 60s — refreshing around the 30s mark holds ~100% uptime without
# over-spending GCDs on Shadow of Death.
DD_REFRESH_AT_S    = rd.DEATHS_DESIGN_DURATION_S
# A downtime window must exceed this for the sim to spend it re-arming Soulsow.
SOULSOW_MIN_WINDOW_S = GCD_BASE_S
# Where the pre-pull Harpe marker lands on the timeline: its begincast time
# (= -cast_time), matching the player's begincast-anchored precast. The 1.3s
# Harpe is channelled while running in and resolves at the pull; scoring sums the
# whole timeline (buff multiplier 1.0 pre-pull), so its 300p is still credited.
PREPULL_CHANNEL_T = -rd.HARPE_CAST_S


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """RPR picker tunables. Adds to the shared knobs (max_weaves_per_gcd /
    triple_weave_clip_s / forbidden_windows):
      * `harvest_moon_priority_high` — fire an armed Harvest Moon ASAP (True) or
        hold it as low-priority filler (False).
      * `prepull_harpe` — pre-channel ranged Harpe during the run-in (True) for a
        free 300p that rolls the first GCD, or open straight into melee (False).
    Both are valid lines; the sweep picks the higher-scoring per duration."""
    harvest_moon_priority_high: bool = True
    prepull_harpe: bool = True


@dataclass
class SimState(SimStateBase):
    soul: int = 0
    shroud: int = 0
    combo_step: int = 0          # 0 expects Slice, 1 Waxing, 2 Infernal
    aoe_combo_step: int = 0      # 0 expects Spinning Scythe, 1 Nightmare Scythe
    soul_reaver: int = 0         # 0/1 — granted by Blood Stalk/Unveiled, spent by Gibbet/Gallows
    enhanced_gibbet: bool = False
    enhanced_gallows: bool = False
    executioner: int = 0         # 0-2 — granted by Gluttony, spent by Exec Gibbet/Gallows
    # Enshroud state
    enshrouded: bool = False
    lemure: int = 0              # Lemure Shroud (0-5)
    void_shroud: int = 0         # Void Shroud (0-2), spent by Lemure's Slice
    oblatio: bool = False        # Sacrificium available during Enshroud
    # Burst pipeline
    plentiful_ready: bool = False
    ideal_host: bool = False     # free next Enshroud (from Plentiful Harvest)
    perfectio_occulta: bool = False  # next Communio grants Parata
    perfectio_parata: bool = False   # Perfectio available
    # Death's Design upkeep
    death_design_end: float = 0.0
    # Soulsow / Harvest Moon
    soulsow: bool = True         # fight starts with Soulsow active (per brief)


# --- GCD-pick helpers -----------------------------------------------------

def _reaver_gcd(state: SimState) -> int:
    """Which Soul Reaver GCD to fire: honor the Enhanced alternation, else
    default to Gibbet."""
    if state.enhanced_gibbet:
        return GIBBET
    if state.enhanced_gallows:
        return GALLOWS
    return GIBBET


def _exec_gcd(state: SimState) -> int:
    if state.enhanced_gibbet:
        return EXEC_GIBBET
    if state.enhanced_gallows:
        return EXEC_GALLOWS
    return EXEC_GIBBET


def _reaping_gcd(state: SimState) -> int:
    """Void vs Cross Reaping inside Enshroud (alternate via Enhanced)."""
    if state.enhanced_gibbet:
        return VOID_REAPING
    if state.enhanced_gallows:
        return CROSS_REAPING
    return VOID_REAPING


def _soul_spender(state: SimState) -> int:
    """Blood Stalk, or its Unveiled replacement when an Enhanced buff is up."""
    if state.enhanced_gibbet:
        return UNVEILED_GIBBET
    if state.enhanced_gallows:
        return UNVEILED_GALLOWS
    return BLOOD_STALK


# --- Refinement / canonical anchors ---------------------------------------
# The greedy picker fires burst (Arcane Circle / Enshroud / Gluttony /
# Plentiful Harvest) as soon as it's available; the refinement nudges these.
_PERFECT_ANCHORS: tuple[int, ...] = (ARCANE_CIRCLE, ENSHROUD, GLUTTONY,
                                     PLENTIFUL_HARVEST)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (ARCANE_CIRCLE, ENSHROUD,
                                             GLUTTONY, PLENTIFUL_HARVEST)
_CANONICAL_HOLD_S: float = engine._CANONICAL_HOLD_S

# Sweep axes (kept here so the shape stays job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_SWEEP_HARVEST_HIGH: tuple[bool, ...] = (True, False)
_SWEEP_PREPULL_HARPE: tuple[bool, ...] = (True, False)


# --- The RPR rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    rd.JOB_DATA.tincture_main_stat, rd.JOB_DATA.tincture_role_coeff)


class ReaperRotationModel(engine.BaseRotationModel):
    cooldowns = rd.COOLDOWNS
    timing = InstantGCD(base_s=GCD_BASE_S)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, entry: EntryState | None = None,
                 gcd_base_s: float | None = None,
                 ranged_windows: tuple[tuple[float, float], ...] = (),
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Phase-continuation entry state (carried gauge + opener start); None for a
        # cold start (non-phased) -> byte-identical to the pre-entry-state model.
        self.entry = entry
        # Consensus ranged-filler windows (LENIENT ceiling only): stretches the
        # refs agree melee range was unavailable, bridged with Harpe. Empty
        # (strict / delivered / every other caller) -> byte-identical.
        self.ranged_windows = ranged_windows
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=2 the picker
        # swaps in AoE buttons (gauge-equivalent, closed-form per slot — see
        # `_maybe_aoe`). Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed (threaded only when faster than the constant): scales
        # the base GCD and the Enshroud (Void/Cross Reaping) GCD by the same haste
        # factor. None keeps the tier constants, byte-identical.
        self.gcd_base_s = GCD_BASE_S if gcd_base_s is None else gcd_base_s
        self.gcd_enshroud_s = GCD_ENSHROUD_S * (self.gcd_base_s / GCD_BASE_S)
        if self.gcd_base_s != GCD_BASE_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {SOUL_SLICE: 2.0}
        state.cd_ready = {GLUTTONY: 0.0, ARCANE_CIRCLE: 0.0}
        if self.entry is not None:
            seed_entry_gauge(state, self.entry.gauge_map, rd.JOB_DATA.gauges)
        return state

    def prepull(self, state: SimState, params) -> None:
        # Death's Design assumed applied pre-pull (Shadow of Death ~ -1.3s).
        state.death_design_end = rd.DEATHS_DESIGN_DURATION_S
        # Phase continuation: already on the boss with no countdown to pre-channel
        # Harpe in, so open at the carried-in start (matches the player) rather than
        # the fresh-pull run-in/precast.
        if self.entry is not None and self.entry.opener_start_s is not None:
            state.t = self.entry.opener_start_s
            return
        # Melee engage delay: the in-fight loop starts after the run-in to the
        # boss (ranged classes act at t=0, so this is 0 for them).
        engage = rd.JOB_DATA.role_policy.engage_delay_s
        if params.prepull_harpe:
            # Pre-channel ranged Harpe (25y) while running in: a free 300p that
            # fills otherwise-dead run-in time. But Harpe rolls the GCD, so the
            # first melee GCD lands ~one GCD in rather than at the bare run-in —
            # the sweep decides whether that trade beats opening straight into
            # melee (it can push a GCD off the end of the fight).
            state.timeline.append((PREPULL_CHANNEL_T, HARPE))
            state.t = max(engage, GCD_BASE_S)
        else:
            state.t = engage

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Grim Reaping is the AoE replacement for Void/Cross Reaping INSIDE Enshroud
        # and shares their 1.5s recast — it must get the same fast GCD, or the AoE
        # Enshroud window runs ~4s long (4 reaping at 2.5s instead of 1.5s), drifting
        # every later Enshroud back until the last one can't finish before the kill
        # (the lost 5th Communio the N=3 AoE audit flagged).
        in_enshroud = state.enshrouded and gcd_id in (
            VOID_REAPING, CROSS_REAPING, GRIM_REAPING)
        return self.gcd_enshroud_s if in_enshroud else self.gcd_base_s

    def pick_gcd(self, state: SimState, params) -> int:
        """The greedy GCD pick, target-aware: the single-target choice with its AoE
        counterpart substituted in where the live target count makes it win (see
        `_maybe_aoe`). N==1 / no schedule -> the ST pick, byte-identical."""
        return self._maybe_aoe(state, self._pick_gcd_st(state, params))

    def _maybe_aoe(self, state: SimState, gcd: int) -> int:
        """Substitute the AoE counterpart of `gcd` when the target count makes it
        out-potency the ST button. RPR's AoE line is gauge-equivalent, so the
        per-slot choice is closed-form. The main combo is a whole-combo decision
        (3-step ST vs 2-step AoE) by per-GCD average so a single slot can't switch
        on the ST finisher still winning. N<2 -> unchanged (byte-identical)."""
        n = n_at(state.t, self.mt_schedule)
        if n < 2:
            return gcd
        jd = rd.JOB_DATA
        if gcd in (SLICE, WAXING_SLICE, INFERNAL_SLICE):
            st_avg = (jd.potencies[SLICE] + jd.potencies[WAXING_SLICE]
                      + jd.potencies[INFERNAL_SLICE]) / 3.0
            aoe_avg = (potency_for(SPINNING_SCYTHE, n, jd)
                       + potency_for(NIGHTMARE_SCYTHE, n, jd)) / 2.0
            if aoe_avg > st_avg:
                return (SPINNING_SCYTHE if state.aoe_combo_step == 0
                        else NIGHTMARE_SCYTHE)
            return gcd
        alt = _AOE_GCD_SWAP.get(gcd)
        if alt is not None and potency_for(alt, n, jd) > potency_for(gcd, n, jd):
            return alt
        return gcd

    def _pick_gcd_st(self, state: SimState, params) -> int:
        t = state.t

        # Expire Enshroud if the window elapsed without finishing (safety).
        if state.enshrouded and state.lemure <= 0:
            state.enshrouded = False

        # 1. Inside Enshroud: spend Lemure Shroud, finish on Communio.
        if state.enshrouded:
            if state.lemure > 1:
                return _reaping_gcd(state)
            return COMMUNIO  # last Lemure -> Communio ends Enshroud

        # 1b. Forced-disconnect window (LENIENT ceiling only — empty otherwise):
        # the ref consensus says melee range is unavailable here, so bridge it
        # the way the refs do — with the best ranged-legal GCD by priority:
        # Perfectio (25y) > an armed Harvest Moon (25y) > Harpe (300p filler).
        # The Enshroud line above is left to run (Communio/Reaping reach), which
        # keeps MORE potency on the ceiling — the conservative direction for a
        # lenient pardon. Melee-only flags (Soul Reaver / Executioner / combo)
        # simply wait out the window, as they do in-game (30s grace).
        if self.ranged_windows and any(s <= t < e
                                       for s, e in self.ranged_windows):
            if state.perfectio_parata:
                return PERFECTIO
            if state.soulsow:
                return HARVEST_MOON
            return HARPE

        # 2. Perfectio follow-up (from a Communio that consumed Occulta).
        if state.perfectio_parata:
            return PERFECTIO

        # 3. Plentiful Harvest (raid burst).
        if state.plentiful_ready and not is_forbidden(PLENTIFUL_HARVEST, t,
                                                      params.forbidden_windows):
            return PLENTIFUL_HARVEST

        # 4. Executioner GCDs (Gluttony-granted, high potency).
        if state.executioner > 0:
            return _exec_gcd(state)

        # 5. Soul Reaver GCDs (Blood Stalk-granted; build shroud).
        if state.soul_reaver > 0:
            return _reaver_gcd(state)

        # 6. Soul Slice when it won't overcap soul (build for the next spender).
        if state.charges.get(SOUL_SLICE, 0) >= 1 and state.soul + 50 <= rd.SOUL_CAP:
            return SOUL_SLICE

        # 7. Maintain Death's Design.
        if state.death_design_end - t <= DD_REFRESH_AT_S:
            return SHADOW_OF_DEATH

        # 8. Armed Harvest Moon (free value from a downtime-cast Soulsow).
        if state.soulsow and params.harvest_moon_priority_high:
            return HARVEST_MOON

        # 9. Main combo filler.
        if state.soulsow:  # low-priority Harvest Moon line
            return HARVEST_MOON
        if state.combo_step == 0:
            return SLICE
        if state.combo_step == 1:
            return WAXING_SLICE
        return INFERNAL_SLICE

    def pick_ogcd(self, state: SimState, params):
        """Greedy oGCD pick, target-aware: the AoE soul spender (Grim Swathe) /
        Lemure's drain (Lemure's Scythe) substituted where they out-potency."""
        return self._maybe_aoe_ogcd(state, self._pick_ogcd_st(state, params))

    def _maybe_aoe_ogcd(self, state: SimState, ogcd):
        if ogcd is None:
            return None
        n = n_at(state.t, self.mt_schedule)
        if n < 2:
            return ogcd
        alt = _AOE_OGCD_SWAP.get(ogcd)
        if alt is not None and potency_for(alt, n, rd.JOB_DATA) > potency_for(
                ogcd, n, rd.JOB_DATA):
            return alt
        return ogcd

    def _pick_ogcd_st(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Arcane Circle — raid buff + Plentiful Harvest enable.
        if state.cd_ready.get(ARCANE_CIRCLE, 0) <= t \
                and not is_forbidden(ARCANE_CIRCLE, t, fw):
            return ARCANE_CIRCLE

        # Sacrificium — only inside Enshroud (Oblatio).
        if state.enshrouded and state.oblatio:
            return SACRIFICIUM

        # Lemure's Slice — drain Void Shroud inside Enshroud.
        if state.enshrouded and state.void_shroud >= 2:
            return LEMURES_SLICE

        # Gluttony — 60s, spend 50 soul for 2 Executioner GCDs.
        if not state.enshrouded and state.cd_ready.get(GLUTTONY, 0) <= t \
                and state.soul >= 50 and not is_forbidden(GLUTTONY, t, fw):
            return GLUTTONY

        # Enshroud — enter the Lemure sub-rotation (shroud 50, or free via Ideal Host).
        if not state.enshrouded and (state.shroud >= 50 or state.ideal_host) \
                and not is_forbidden(ENSHROUD, t, fw):
            return ENSHROUD

        # Blood Stalk / Unveiled — spend 50 soul for a Soul Reaver (feeds shroud).
        if not state.enshrouded and state.soul_reaver == 0 and state.soul >= 50:
            return _soul_spender(state)

        return None

    # --- Exact-solver seam (optimal.solve_optimal) --------------------------
    # NOT wired into RPR's production ceiling: `simulate_idealized_perfect` stays on
    # `engine.perfect`, and that is the CORRECT resting state — the full
    # action-perfect solver (these GCD filler forks + the Blood-Stalk-vs-bank oGCD
    # fork below) finds only ~+0.2% over greedy, so the search axis is closed and
    # wiring it would only buy runtime. (A historical "2.2% fidelity gap" note here
    # was stale: it quoted a pre-entry-gauge M12S-P2 parse; the 2026-06-10 live tier
    # sweep is 0/50 over 100.5%, max 99.34%.) RPR's GCD line is mostly FORCED
    # (Enshroud reaping / Perfectio / Plentiful / Executioner / Soul Reaver); its
    # real decisions are oGCD-economy (Blood Stalk vs Gluttony soul contention).
    # These overrides are kept + validated for losslessness/exactness in
    # tests/test_optimal_solver.py (the engine's oGCD FORK path).

    def clone(self, state: SimState) -> SimState:
        new = copy.copy(state)
        new.charges = dict(state.charges)
        new.cd_ready = dict(state.cd_ready)
        new.timeline = list(state.timeline)
        return new

    def legal_gcds(self, state: SimState, params) -> list[int]:
        # Always include the greedy pick (so the DP can never fall below greedy), then
        # at a FILLER slot also expose the other legal fillers so the search can
        # reorder Soul Slice / Shadow of Death (DD upkeep) / Harvest Moon / the combo.
        # Forced contexts (Enshroud / Perfectio / Plentiful / Executioner / Soul
        # Reaver) collapse to the single greedy move.
        greedy = self.pick_gcd(state, params)
        moves = [greedy]
        _FILLERS = (SLICE, WAXING_SLICE, INFERNAL_SLICE, SOUL_SLICE,
                    SHADOW_OF_DEATH, HARVEST_MOON)
        if greedy in _FILLERS:
            combo = (SLICE if state.combo_step == 0
                     else WAXING_SLICE if state.combo_step == 1 else INFERNAL_SLICE)
            alts: list[int] = []
            if state.charges.get(SOUL_SLICE, 0) >= 1 and state.soul + 50 <= rd.SOUL_CAP:
                alts.append(SOUL_SLICE)
            if state.death_design_end - state.t <= DD_REFRESH_AT_S:
                alts.append(SHADOW_OF_DEATH)
            if state.soulsow:
                alts.append(HARVEST_MOON)
            alts.append(combo)
            for a in alts:
                if a not in moves:
                    moves.append(a)
        return moves

    def dominance_key(self, state: SimState):
        return (
            round(state.t, 2), state.combo_step, state.soul_reaver, state.executioner,
            state.enshrouded, state.lemure, state.void_shroud, state.oblatio,
            state.enhanced_gibbet, state.enhanced_gallows, state.plentiful_ready,
            state.ideal_host, state.perfectio_occulta, state.perfectio_parata,
            state.soulsow, round(state.death_design_end - state.t, 1),
        )

    def dominance_vector(self, state: SimState) -> tuple:
        return (
            state.soul, state.shroud, round(state.charges.get(SOUL_SLICE, 0.0), 3),
            -round(max(0.0, state.cd_ready.get(GLUTTONY, 0.0) - state.t), 2),
            -round(max(0.0, state.cd_ready.get(ARCANE_CIRCLE, 0.0) - state.t), 2),
        )

    def ogcd_candidates(self, state: SimState, params) -> list:
        greedy = self.pick_ogcd(state, params)
        # The RPR decision the GCD level can't see: holding Blood Stalk to bank 50
        # soul for an imminent Gluttony (2 Executioner GCDs — more potency per soul
        # than Blood Stalk's 1 Soul Reaver) can beat spending now. Expose [spend,
        # hold] so the solver optimizes the soul schedule instead of greedy-dumping.
        if greedy in (BLOOD_STALK, UNVEILED_GIBBET, UNVEILED_GALLOWS):
            return [greedy, None]
        return [greedy]

    # exact_g / terminal_g use the BaseRotationModel default (re-scan score_fn): RPR
    # scores a flat potency sum (no DoT), and re-scanning correctly includes the
    # pre-pull Harpe that `prepull` appends outside apply_cast.

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Soul gauge
        if ability_id in rd.SOUL_GENERATORS:
            state.soul = min(rd.SOUL_CAP, state.soul + rd.SOUL_GENERATORS[ability_id])
        if ability_id in rd.SOUL_SPENDERS:
            state.soul = max(0, state.soul - rd.SOUL_SPENDERS[ability_id])

        # Shroud gauge
        if ability_id in rd.SHROUD_GENERATORS:
            state.shroud = min(rd.SHROUD_CAP,
                               state.shroud + rd.SHROUD_GENERATORS[ability_id])
        if ability_id in rd.SHROUD_SPENDERS and not state.ideal_host:
            state.shroud = max(0, state.shroud - rd.SHROUD_SPENDERS[ability_id])

        # Cooldown / charges (generic).
        apply_cooldown(state, self.cooldowns, ability_id)

        # Per-ability effects
        if ability_id in (BLOOD_STALK, UNVEILED_GIBBET, UNVEILED_GALLOWS, GRIM_SWATHE):
            state.soul_reaver = 1
        elif ability_id == GLUTTONY:
            state.executioner = 2
        elif ability_id == GUILLOTINE:
            # AoE Soul Reaver GCD — spends a reaver stack, no positional/Enhanced.
            state.soul_reaver = max(0, state.soul_reaver - 1)
        elif ability_id == EXEC_GUILLOTINE:
            state.executioner = max(0, state.executioner - 1)
        elif ability_id == GRIM_REAPING:
            # AoE Void/Cross Reaping inside Enshroud.
            state.lemure = max(0, state.lemure - 1)
            state.void_shroud = min(2, state.void_shroud + 1)
        elif ability_id == SPINNING_SCYTHE:
            state.aoe_combo_step = 1
            state.combo_step = 0
        elif ability_id == NIGHTMARE_SCYTHE:
            state.aoe_combo_step = 0
            state.combo_step = 0
        elif ability_id == SOUL_SCYTHE:
            # Soul Scythe shares Soul Slice's charge pool (the pick gates on it).
            state.charges[SOUL_SLICE] = max(0.0, state.charges.get(SOUL_SLICE, 2.0) - 1)
        elif ability_id in (GIBBET, EXEC_GIBBET):
            # Gibbet consumes a reaver/executioner stack and grants Enhanced Gallows.
            if ability_id == GIBBET:
                state.soul_reaver = max(0, state.soul_reaver - 1)
            else:
                state.executioner = max(0, state.executioner - 1)
            state.enhanced_gibbet = False
            state.enhanced_gallows = True
        elif ability_id in (GALLOWS, EXEC_GALLOWS):
            if ability_id == GALLOWS:
                state.soul_reaver = max(0, state.soul_reaver - 1)
            else:
                state.executioner = max(0, state.executioner - 1)
            state.enhanced_gallows = False
            state.enhanced_gibbet = True
        elif ability_id == ENSHROUD:
            state.enshrouded = True
            state.lemure = rd.LEMURE_SHROUD_ON_ENSHROUD
            state.void_shroud = 0
            state.oblatio = True
            state.ideal_host = False
            # Enshroud resets the Enhanced alternation into the Reaping pair.
            state.enhanced_gibbet = True
            state.enhanced_gallows = False
        elif ability_id in (VOID_REAPING, CROSS_REAPING):
            state.lemure = max(0, state.lemure - 1)
            state.void_shroud = min(2, state.void_shroud + 1)
            # Alternate the Reaping pair.
            if ability_id == VOID_REAPING:
                state.enhanced_gibbet = False
                state.enhanced_gallows = True
            else:
                state.enhanced_gallows = False
                state.enhanced_gibbet = True
        elif ability_id in (LEMURES_SLICE, LEMURES_SCYTHE):
            state.void_shroud = max(0, state.void_shroud - 2)
        elif ability_id == SACRIFICIUM:
            state.oblatio = False
        elif ability_id == COMMUNIO:
            state.enshrouded = False
            state.lemure = 0
            state.void_shroud = 0
            state.oblatio = False
            if state.perfectio_occulta:
                state.perfectio_occulta = False
                state.perfectio_parata = True
            # Reset Enhanced state after the Enshroud window.
            state.enhanced_gibbet = False
            state.enhanced_gallows = False
        elif ability_id == PERFECTIO:
            state.perfectio_parata = False
        elif ability_id == ARCANE_CIRCLE:
            state.plentiful_ready = True
        elif ability_id == PLENTIFUL_HARVEST:
            state.plentiful_ready = False
            state.ideal_host = True          # free next Enshroud
            state.perfectio_occulta = True   # next Communio -> Parata
        elif ability_id in (SHADOW_OF_DEATH, WHORL_OF_DEATH):
            # Refresh Death's Design (Whorl applies it to ALL targets; same upkeep).
            base = max(t, state.death_design_end)
            state.death_design_end = min(t + rd.DEATHS_DESIGN_MAX_S,
                                         base + rd.DEATHS_DESIGN_DURATION_S)
        elif ability_id == HARVEST_MOON:
            state.soulsow = False

        # Main-combo tracking (an ST combo button resets the AoE combo and vice
        # versa — switching combos restarts the other).
        if ability_id == SLICE:
            state.combo_step = 1
            state.aoe_combo_step = 0
        elif ability_id == WAXING_SLICE:
            state.combo_step = 2 if state.combo_step == 1 else 0
            state.aoe_combo_step = 0
        elif ability_id == INFERNAL_SLICE:
            state.combo_step = 0
            state.aoe_combo_step = 0

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # Re-arm Soulsow if it's down and the window is long enough, so an armed
        # Harvest Moon fires at the next uptime edge. Soulsow is a GCD but carries
        # no potency (0 in the table), so it's purely a flag set here, not scored.
        s, e = win_start, win_end
        if not state.soulsow and (e - s) > SOULSOW_MIN_WINDOW_S:
            press_t = max(s, state.last_gcd_t + GCD_BASE_S)
            if press_t < e:
                state.timeline.append((press_t, SOULSOW))
                state.soulsow = True

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            for hm in _SWEEP_HARVEST_HIGH:
                for ph in _SWEEP_PREPULL_HARPE:
                    yield SimParams(max_weaves_per_gcd=mw,
                                    harvest_moon_priority_high=hm,
                                    prepull_harpe=ph,
                                    forbidden_windows=extra_forbidden)


_MODEL = ReaperRotationModel()


def _model_for(sim_context) -> ReaperRotationModel:
    """The model bound to this run's per-pull context — a per-player effective GCD
    (CeilingContext, faster-than-constant Skill Speed), an `EntryState`
    (phase-continuation gauge + opener start), and/or a `RangedFillerContext`
    (consensus disconnect windows, LENIENT ceiling only), and/or a
    `MultiTargetContext` (the AoE N(t) schedule). `None`/none of them -> the
    default model, byte-identical to the pre-context path. Unwrap order mirrors
    the nesting: CeilingContext outermost, then MultiTargetContext, then
    RangedFillerContext, then the bare entry payload."""
    from jobs._core.downtime_sources import (
        MultiTargetContext, RangedFillerContext)
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    ranged: tuple[tuple[float, float], ...] = ()
    if isinstance(payload, RangedFillerContext):
        ranged = payload.windows
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    return ReaperRotationModel(entry=entry, gcd_base_s=gcd,
                               ranged_windows=ranged, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to
    a multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). DD-agnostic (a constant x1.10 on every candidate,
    so it doesn't change the argmax) and buff-aware when given. Empty schedule ->
    single target, byte-identical to the pre-AoE scorer."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.reaper.scoring import score_delivered_potency
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
    """Run the idealized rotation once. Returns (timeline, _unused) — the tuple
    shape mirrors the MCH sim (whose 2nd element is Queen battery); RPR has no
    analogous scalar, so it is always 0."""
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
    """Perfect simulator: sweep + local-search refinement, buff-aware when
    `buff_intervals` is given."""
    model = _model_for(sim_context)
    return engine.perfect(model, _make_score(model.mt_schedule), fight_duration_s,
                          downtime_windows or [], buff_intervals)


def _canonical_burst_forbidden(
        buff_intervals: list[tuple[float, float, float]],
        ) -> tuple[tuple[int, float, float], ...]:
    """RPR canonical anchors held into each full-stack buff window (the
    comparison lane). Thin wrapper over the engine helper, kept module-local so
    the canonical-sim tests can address it directly."""
    return engine.canonical_burst_forbidden(buff_intervals, _CANONICAL_ALIGN_ANCHORS)


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
