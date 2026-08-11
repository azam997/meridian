"""Idealized MCH rotation — the Machinist `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the MCH-specific rotation: the gauge state (heat /
battery / Overheated), the priority pickers, the per-cast state transitions, the
pre-pull Reassemble, and the Flamethrower downtime-edge squeeze. The `simulate_*`
functions at the bottom are thin shims that bind this model to the engine (kept
under their original names so the sidecar, the scorer and the tests call them
unchanged).

Design: **priority-based picker**, not a frame-perfect simulator. At each GCD
slot it greedily picks the highest-priority available action; oGCDs weave into
the post-GCD window. The output isn't game-engine-correct — it's a defensible
upper bound the analyzer validates against real top-parse data.

MCH-specific rotation encoded:
- Pre-pull Reassemble at -5s (canonical opener for early CDs).
- All MCH PvE weaponskills are instant in DT 7.x (no cast-time clamp).
- Hypercharge consumes 50 heat OR a free Hypercharged buff (Barrel Stab);
  grants 5 Blazing Shot stacks at 1.5s recast each.
- Barrel Stabilizer grants free Hypercharge + Full Metal Machinist proc.
- Chain Saw grants Excavator-ready proc.
- Blazing Shot advances Double Check / Checkmate recast by 15s.
- Queen at battery >= 50 (her 12s pet duration is the real recast gate).

Out of scope for v1:
- Mechanic-specific positioning (boss invulns are downtime, not modeled).
- Procs that aren't documented in the data tables.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from functools import lru_cache

from jobs._core.sim import engine, optimal
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.machinist import data as md


# --- Ability IDs (locally aliased for readability) ------------------------
HEATED_SPLIT       = 7411
HEATED_SLUG        = 7412
HEATED_CLEAN       = 7413
DRILL              = 16498
AIR_ANCHOR         = 16500
CHAIN_SAW          = 25788
EXCAVATOR          = 36981
FMF                = 36982
BLAZING_SHOT       = 36978
DOUBLE_CHECK       = 36979
CHECKMATE          = 36980
REASSEMBLE         = 2876
WILDFIRE           = 2878
HYPERCHARGE        = 17209
BARREL_STABILIZER  = 7414
QUEEN              = 16501
FLAMETHROWER       = 7418
# AoE line (cast only in 3+ target windows — see the value crossovers below).
# MCH keeps its SINGLE-TARGET rotation through 2 targets: Auto Crossbow is NOT
# gauge-equivalent to Blazing Shot (only Blazing Shot grants the DC/CM CDR), and
# the Heated combo banks battery (-> Queen) that Scattergun does not, so both AoE
# buttons only out-VALUE the ST line at 3 targets despite higher raw potency at 2.
SCATTERGUN         = md.SCATTERGUN_ABILITY_ID
AUTO_CROSSBOW      = md.AUTO_CROSSBOW_ABILITY_ID
BIOBLASTER         = md.BIOBLASTER_ABILITY_ID

# Defensive / utility actions the simulator intentionally ignores. They carry
# no DPS value, so firing them in the idealized rotation only adds noise to the
# timeline diff (the player uses them reactively, not on cooldown). The picker
# simply never returns them: Tactician (16889), Dismantle (2887), Second Wind
# (7541), Arm's Length (7548), Foot Graze (7553), Leg Graze (7554).

# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S          = 2.50     # MCH true BiS GCD (no headroom — the tool-vs-proc
                              # reorder + the battery-timing beam close the search
                              # residual the old 2.43 hid)
GCD_OVERHEATED_S    = 1.5     # Blazing Shot recast during Overheated
PRE_PULL_REASSEMBLE_T = -5.0  # canonical pre-pull Reassemble timing
QUEEN_RECAST_S      = 12.5    # Queen cannot be re-summoned while the pet is
                              # active (12s duration) + ~0.5s wind-down. The
                              # FFXIV wiki's "6s recast" is a server-side
                              # value that's masked by the active-pet block.
                              # VERIFIED non-binding (scripts/probe_mch_ids.py):
                              # min observed re-summon gap across 289 top-parse
                              # pairs is 20.0s — battery economy dominates.
QUEEN_ACTIVE_S      = 17.5    # Time from summon for the Automaton Queen's full
                              # sequence to deliver — her LAST tick (Crowned
                              # Collider) lands here. MEASURED across 1992 Queen
                              # activations in real logs (dev-cache probe):
                              # ~6.5s wind-up to her first hit, Pile Bunker at
                              # ~14.7s, Crowned Collider at ~17.4s (medians). Her
                              # big finishers land LAST, so a cut-off Queen keeps
                              # only a fraction of her value (see
                              # `queen_deliverable_fraction`).
QUEEN_OVERDRIVE_MIN_S = 6.0   # The finishers (Pile Bunker + Crowned Collider,
                              # ~63% of her potency) can be fired MANUALLY (Queen
                              # Overdrive, action 16502) to salvage burst before
                              # the boss leaves / the fight ends — trading away
                              # the trailing Arm Punches. MEASURED Overdrive ->
                              # damage delay: Pile Bunker ~2.7s, Crowned Collider
                              # ~5.4s after the press (n=32), so ~6s of targetable
                              # time is the floor to land both finishers. Below
                              # it she does ~nothing; above it she's worth her
                              # finishers, ramping to full as more Arm Punches fit.
HYPERCHARGE_WINDOW_S = 10.0    # Hypercharged buff duration

# Tools — used in pick_gcd ordering.
_TOOL_IDS = (CHAIN_SAW, AIR_ANCHOR, DRILL, EXCAVATOR, FMF)

# Every weaponskill (GCD) the sim casts — the hits a Wildfire payload counts.
# Mirrors the scorer's "non-oGCD metadata" check on the sim's own move set
# (Flamethrower and the tincture marker are excluded there too).
_WEAPONSKILL_IDS = frozenset({
    HEATED_SPLIT, HEATED_SLUG, HEATED_CLEAN, DRILL, AIR_ANCHOR, CHAIN_SAW,
    EXCAVATOR, FMF, BLAZING_SHOT,
    # AoE weaponskills (count toward a Wildfire payload like any other GCD).
    SCATTERGUN, AUTO_CROSSBOW, BIOBLASTER,
})

# Valid Reassemble targets — the picker holds Reassemble unless the *next*
# GCD would land on one of these (660p tools / procs). Firing Reassemble
# on Blazing Shot (240p) or Heated combo (220-420p) gives a much smaller
# crit-DH bonus and is canonical misuse per MCH-expert guidance. Full Metal
# Field is intentionally excluded: it already delivers a guaranteed crit
# direct hit, so Reassemble on it is wasted.
_VALID_REASSEMBLE_TARGETS: set[int] = {DRILL, AIR_ANCHOR, CHAIN_SAW, EXCAVATOR}

# Floor an Overdrive salvages: the finishers' share of the full sequence
# (derived from the potency tables so it tracks any data correction). At 50
# battery that's (680 + 780) / 2300 ~= 0.63.
QUEEN_OVERDRIVE_FLOOR: float = (
    (md.QUEEN_POTENCY_BASE["pile_bunker"] + md.QUEEN_POTENCY_BASE["crowned_collider"])
    / sum(md.QUEEN_POTENCY_BASE[k] * md.QUEEN_SEQUENCE_COUNTS[k]
          for k in md.QUEEN_POTENCY_BASE)
)

# --- AoE-vs-single-target value crossovers --------------------------------
# MCH keeps its single-target rotation through 2 targets and swaps to the AoE
# line at 3+ — but the crossover DIFFERS per ability and is NOT a bare per-target
# potency check (that swaps a full target too early — the reported 2-target bug),
# because the single-target line carries value the AoE buttons don't:
#
#   * AUTO CROSSBOW vs Blazing Shot — only Blazing Shot reduces the Double Check /
#     Checkmate recasts (Auto Crossbow grants NO CDR — wiki-verified). The extra
#     DC/CM that CDR buys CLEAVE, so the CDR's worth grows with the target count:
#     value = 240 + 2 x (15/30) x potency_for(DC, n). Auto Crossbow (180n) only
#     overtakes it at n >= 6 (1080 > 1050), NOT n=3 (540 < 672) — a closed form
#     that matches the full-sim crossover exactly. So 3-5 targets is a HYBRID
#     (AoE Scattergun filler + single-target Blazing Shot Hypercharge), which is
#     what top MCH does for the Gauss/Ricochet CDR. See `_blazing_shot_value`.
#   * SCATTERGUN vs the Heated combo — the combo banks 10 battery on Clean Shot
#     -> Queen (46p/battery, single-target so N-independent); Scattergun banks
#     none, only extra heat. value = 320 + 5x24 + (10/3)x46 = 593 vs 130n + 10x24,
#     crossing at n >= 3 (630 > 593). Heat is valued at the base Blazing-Shot rate
#     (a conservative floor — its true worth rises with the same N-aware CDR, which
#     only delays the swap, the safe direction). See `_HEATED_COMBO_VALUE`.
#   * BIOBLASTER vs Drill needs no adjustment — both spend a Drill charge with no
#     CDR/battery asymmetry, so its raw-potency check is already complete; Drill
#     wins until 7 targets.
def _blazing_shot_value(n: int) -> float:
    """Blazing Shot's potency PLUS its Double Check / Checkmate CDR value AT n
    targets. The CDR generates extra DC/CM casts, which cleave, so its worth scales
    with the target count via `potency_for` — pushing the Auto Crossbow crossover
    to n=6 (the cleaving oGCDs the CDR buys outweigh Auto Crossbow's per-target
    potency until then). A constant (single-target) value would swap at n=3 and
    strand the CDR's AoE value (the audit's N>=3 finding)."""
    return md.POTENCIES[BLAZING_SHOT] + sum(
        (md.BLAZING_SHOT_CDR_S / md.COOLDOWNS[cd][0]) * potency_for(cd, n, md.JOB_DATA)
        for cd in (DOUBLE_CHECK, CHECKMATE))


_HEATED_COMBO_VALUE: float = (
    (md.POTENCIES[HEATED_SPLIT] + md.POTENCIES[HEATED_SLUG]
     + md.POTENCIES[HEATED_CLEAN]) / 3.0
    + md.HEAT_GENERATORS[HEATED_SPLIT] * md.HEAT_VALUE_P_PER_UNIT
    + (md.BATTERY_GENERATORS[HEATED_CLEAN] / 3.0) * md.BATTERY_VALUE_P_PER_UNIT)


# --- Admissible-bound inputs (derived from the data tables, never hardcoded, so a
# potency / crit-mult edit can't silently make `admissible_remaining` inadmissible —
# which would be a silently-wrong "provable" optimum). Each is >= the true scored
# value of the weaponskill it caps (every weaponskill assumed Reassemble-crit-DH'd,
# the admissible over-count).
_UB_CRIT: float = md.GUARANTEED_CRIT_DH_MULT
_UB_TOOL_P: float = 660.0 * _UB_CRIT                 # Drill / Air Anchor / Chain Saw / Excavator
_UB_FMF_P: float = md.POTENCIES[FMF] * _UB_CRIT      # Full Metal Field (innately crit-DH)
_UB_FILLER_P: float = max(                            # the richest non-premium GCD
    md.POTENCIES[HEATED_SPLIT], md.POTENCIES[HEATED_SLUG],
    md.POTENCIES[HEATED_CLEAN], md.POTENCIES[BLAZING_SHOT]) * _UB_CRIT


def _targetable_time_after(t: float, fight_duration_s: float,
                           downtime_windows: list[tuple[float, float]]) -> float:
    """Continuous targetable, in-fight time from `t` until the boss first goes
    untargetable or the fight ends — the window a Queen summoned at `t` has to
    land her hits in."""
    if t >= fight_duration_s:
        return 0.0
    horizon = fight_duration_s
    for win_start, win_end in downtime_windows:
        if win_start <= t < win_end:
            return 0.0                       # already untargetable at summon
        if t < win_start < horizon:
            horizon = win_start
    return horizon - t


def queen_deliverable_fraction(
        summon_t: float, fight_duration_s: float,
        downtime_windows: list[tuple[float, float]]) -> float:
    """Fraction of a Queen's potency that actually lands if summoned at
    `summon_t`. She hits over an ~`QUEEN_ACTIVE_S` autonomous sequence and stops
    the moment the boss leaves (downtime) or the fight ends. Because the
    finishers can be fired manually (Overdrive), she's worth `QUEEN_OVERDRIVE_FLOOR`
    of her value once `QUEEN_OVERDRIVE_MIN_S` of targetable time fits and ramps
    linearly to 1.0 over her full window; below that minimum she does ~nothing.

    Used on BOTH sides for symmetry: the sim credits each summon's deliverable
    battery (so the ceiling never banks a Queen that can't hit the target), and
    the delivered scorer discounts a player's cut-off Queen the same way."""
    avail = _targetable_time_after(summon_t, fight_duration_s, downtime_windows)
    if avail >= QUEEN_ACTIVE_S:
        return 1.0
    if avail < QUEEN_OVERDRIVE_MIN_S:
        return 0.0
    return (QUEEN_OVERDRIVE_FLOOR + (1.0 - QUEEN_OVERDRIVE_FLOOR)
            * (avail - QUEEN_OVERDRIVE_MIN_S)
            / (QUEEN_ACTIVE_S - QUEEN_OVERDRIVE_MIN_S))


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """MCH picker tunables. Adds the Queen levers to the shared knobs
    (max_weaves_per_gcd / triple_weave_clip_s / forbidden_windows).

    queen_cast_battery: minimum battery to summon Queen in the main fight.
        BUFF-AGNOSTIC path only. Linear at 46p/battery, so per-Queen value is
        identical from 50 to 100 — agnostically the only thing that matters is
        consuming ALL generated battery before the fight ends, and the 50-battery
        minimum is the real constraint. NOT true buff-aware: the battery level
        you summon at IS a timing lever (you reach 90 later than 50), and timing
        decides which summons land in raid windows (she snapshots the multiplier
        at SUMMON for her whole payload). So when `state.buff_intervals` is
        present this threshold is bypassed in favour of `_queen_should_bank`,
        which banks battery toward a reachable window instead of a flat cutoff.
    queen_eof_window_s: in the last N seconds drop the Queen threshold to the
        50-battery minimum so end-of-fight residual gets spent.
    """
    queen_cast_battery: int = 50
    queen_eof_window_s: float = 60.0


@dataclass
class SimState(SimStateBase):
    heat: int = 0
    battery: int = 0
    procs: dict[int, float] = field(default_factory=dict)
    overheated_stacks: int = 0
    overheated_window_end: float = 0.0   # absolute t at which BS stacks expire
    free_hypercharges: int = 0
    combo_step: int = 0   # 0 expects Split, 1 expects Slug, 2 expects Clean
    queen_battery_spent: float = 0.0  # cumulative DELIVERABLE battery (battery at
                                      # each summon x its deliverable fraction)
    # Open Wildfire window: cast time + weaponskill hits banked so far (cap 6).
    # Tracked so the beam-dedup / DP-dominance signature captures the payload's
    # remaining capacity — the one WF term the cooldown offset alone can't carry.
    wf_cast_t: float = -1e9
    wf_hits: int = 0
    # Incremental BUFF-AGNOSTIC running score (mirrors score_delivered_potency's
    # flat pass: potency x crit-DH for Reassembled/FMF weaponskills + 240/WF hit
    # + per-summon deliverable Queen battery), maintained in apply_cast so
    # `beam_prune` is O(1) instead of re-scanning the timeline per successor.
    # Prune-only: the engine's final selection always re-scores exactly.
    _score_flat: float = 0.0
    _reassemble_until: float = -1e9
    # Incremental EXACT score components for the DP's `exact_g`/`terminal_g`
    # (raid-buff-agnostic but POT-AWARE — the canonical scorer folds the in-sim
    # tincture marker into a multiplier window even when `buff_intervals` is
    # None, so a flat sum can't reproduce it). `_g_main` is pass 1 + the WF
    # payload + the Flamethrower tick, each contribution scaled by the pot
    # multiplier at its snapshot time; `_q_raw`/`_q_rawm` carry the per-summon
    # raw battery and raw-battery-x-pot-multiplier sums the scorer's Queen
    # redistribution needs (see `MachinistRotationModel.exact_g`).
    _g_main: float = 0.0
    _q_raw: float = 0.0
    _q_rawm: float = 0.0


# --- Refinement / canonical anchors ---------------------------------------
# Base refinement (buff-agnostic, tool-drift fix): the validated original.
_PERFECT_REFINEMENT_ANCHORS: tuple[int, ...] = (HYPERCHARGE, QUEEN, WILDFIRE)
# The buff-aware pass also considers Barrel Stabilizer (it gates FMF + a free
# Hypercharge, a big burst worth aligning).
_BUFF_REFINEMENT_ANCHORS: tuple[int, ...] = (
    HYPERCHARGE, QUEEN, WILDFIRE, BARREL_STABILIZER)
# Canonical "hold the 2-min burst for the window": Queen and the heat-driven
# Hypercharges are NOT force-held (Queen is summoned ~on cooldown to avoid
# battery overcap, HC fires several times a minute) — only the 120s burst
# defines the window.
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (WILDFIRE, BARREL_STABILIZER)
_CANONICAL_HOLD_S: float = engine._CANONICAL_HOLD_S

# Sweep axes. Kept small (5*3*2 = 30 runs). Add axes here when a future fixture
# surfaces a new lever worth tuning.
_SWEEP_QUEEN_BATTERIES: tuple[int, ...]     = (50, 60, 70, 80, 90)
_SWEEP_QUEEN_EOF_WINDOWS: tuple[float, ...] = (30.0, 60.0, 90.0)
_SWEEP_MAX_WEAVES: tuple[int, ...]          = (2, 3)

# Buff-aware Queen banking (buff-aware ceiling path only — see SimParams +
# `_queen_should_bank`). Over-estimates battery generation so a hold only happens
# when reaching the window clearly won't overcap: under-banking just summons a
# touch early (the safe error), overcapping strands battery (the costly one).
_QUEEN_BANK_GEN_RATE_PER_S: float = 2.5   # conservative-high battery/s while banking


# --- The MCH rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    md.JOB_DATA.tincture_main_stat, md.JOB_DATA.tincture_role_coeff)


class MachinistRotationModel(engine.BaseRotationModel):
    cooldowns = md.COOLDOWNS
    timing = InstantGCD(base_s=GCD_BASE_S)
    agnostic_anchors = _PERFECT_REFINEMENT_ANCHORS
    buff_anchors = _BUFF_REFINEMENT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()) -> None:
        # Per-player Skill Speed: `gcd_base_s` (= min(constant, inferred), threaded only
        # when the player is faster than the constant) speeds the whole rotation. The
        # Overheated Blazing Shot recast scales by the same haste factor. `None` keeps
        # the tier constant — byte-identical to the singleton.
        self.gcd_base_s = GCD_BASE_S if gcd_base_s is None else gcd_base_s
        self.gcd_overheated_s = GCD_OVERHEATED_S * (self.gcd_base_s / GCD_BASE_S)
        if self.gcd_base_s != GCD_BASE_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=2 the picker
        # swaps in AoE buttons and the beam forks the heat-divergent combo. Empty
        # () -> single target, byte-identical.
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {
            DRILL:        2.0,
            REASSEMBLE:   2.0,
            DOUBLE_CHECK: 3.0,
            CHECKMATE:    3.0,
        }
        state.cd_ready = {
            AIR_ANCHOR:         0.0,
            CHAIN_SAW:          0.0,
            BARREL_STABILIZER:  0.0,
            WILDFIRE:           0.0,
            HYPERCHARGE:        0.0,
            QUEEN:              0.0,
        }
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull Reassemble at -5s (canonical opener). MCH casts are instant,
        # so there's no pre-pull damage channel — just the buff setup.
        state.t = PRE_PULL_REASSEMBLE_T
        self.apply_cast(state, REASSEMBLE)
        engine.advance_time(self, state, 0.0)

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # The 1.5s Overheated recast belongs to Blazing Shot ALONE — any other
        # weaponskill keeps the normal recast even inside the window (so the
        # end-of-fight interrupt tool takes a full 2.5s slot, and stale stacks
        # past the 10s window never shrink a regular GCD). Keying on the PICKED
        # GCD is exact: the pickers only return Blazing Shot while stacks are
        # live and unexpired. This is also what the beam / exact solver call
        # (they never call `gcd_slot`), so the searched lines run at the right
        # speed.
        return self.gcd_overheated_s if gcd_id == BLAZING_SHOT else self.gcd_base_s

    def _expire_overheat(self, state: SimState) -> None:
        """Expire Hypercharged stacks if the 10s window has elapsed. Called at
        the top of both pickers so greedy and beam paths see the same state."""
        if state.overheated_stacks > 0 and state.t >= state.overheated_window_end:
            state.overheated_stacks = 0

    def _overheated_spender(self, state: SimState) -> int:
        """Blazing Shot, or its AoE form Auto Crossbow once it out-VALUES it. Both
        spend one Overheated stack, but ONLY Blazing Shot reduces the Double Check /
        Checkmate recasts, and those oGCDs cleave — so the pick weighs that CDR at
        the live target count (`_blazing_shot_value(n)`), not bare potency. Auto
        Crossbow (180n) loses to Blazing Shot+CDR until n=6, so 3-5 targets keep the
        single-target Hypercharge (the Scattergun-filler hybrid)."""
        n = self._n(state.t)
        if n >= 2 and potency_for(AUTO_CROSSBOW, n, md.JOB_DATA) > _blazing_shot_value(n):
            return AUTO_CROSSBOW
        return BLAZING_SHOT

    def _scattergun_beats_combo(self, n: int) -> bool:
        """True iff AoE Scattergun out-VALUES the single-target Heated combo at `n`
        targets. Compares per-GCD value (potency + heat + the combo's battery ->
        Queen), not bare potency — the combo's battery is why the crossover is 3
        targets, not 2 (see `_HEATED_COMBO_VALUE`)."""
        if n < 2:
            return False
        scatter = (potency_for(SCATTERGUN, n, md.JOB_DATA)
                   + md.HEAT_GENERATORS[SCATTERGUN] * md.HEAT_VALUE_P_PER_UNIT)
        return scatter > _HEATED_COMBO_VALUE

    def _maybe_scattergun(self, state: SimState) -> int:
        """Scattergun (AoE filler) once it out-values the Heated combo (3+
        targets), else the next single-target combo step."""
        if self._scattergun_beats_combo(self._n(state.t)):
            return SCATTERGUN
        return (HEATED_SPLIT, HEATED_SLUG, HEATED_CLEAN)[state.combo_step]

    def _bioblaster_beats_drill(self, n: int) -> bool:
        """True iff AoE Bioblaster out-potencies single-target Drill at `n` targets.
        Both spend one Drill charge with no CDR/battery asymmetry, so bare potency
        IS the complete comparison here — Drill wins until 7 targets."""
        return n >= 2 and potency_for(BIOBLASTER, n, md.JOB_DATA) > potency_for(
            DRILL, n, md.JOB_DATA)

    def _maybe_bioblaster(self, state: SimState) -> int:
        """Bioblaster (AoE, shares Drill's charge pool) when it out-potencies the
        single-target Drill at the live target count, else Drill."""
        if self._bioblaster_beats_drill(self._n(state.t)):
            return BIOBLASTER
        return DRILL

    def pick_gcd(self, state: SimState, params=None) -> int:
        """Highest-priority weaponskill available right now."""
        t = state.t
        self._expire_overheat(state)

        if state.overheated_stacks > 0:
            # Hypercharge is a *buff*, not a GCD lock: a regular 2.5s GCD can
            # interrupt the BS chain. In steady state the 10s window is too
            # tight, so interrupting mid-fight loses BS stacks to expiration.
            # The only place interrupting wins is **end of fight**, where the
            # trailing BS stacks would expire anyway. Trade a would-be-wasted
            # 240p BS for a 660p tool. Allow the interrupt only when remaining
            # stacks won't all fire before fight end.
            bs_time_needed = state.overheated_stacks * GCD_OVERHEATED_S
            time_to_fight_end = state.fight_duration_s - t
            stacks_will_be_wasted = time_to_fight_end < bs_time_needed
            if stacks_will_be_wasted:
                if FMF in state.procs and state.procs[FMF] > t:
                    return FMF
                if EXCAVATOR in state.procs and state.procs[EXCAVATOR] > t:
                    return EXCAVATOR
                if state.cd_ready.get(AIR_ANCHOR, 0) <= t:
                    return AIR_ANCHOR
                if state.cd_ready.get(CHAIN_SAW, 0) <= t:
                    return CHAIN_SAW
                if state.charges.get(DRILL, 0) >= 1:
                    return DRILL
            return self._overheated_spender(state)
        # Proc vs tool priority. A proc (Full Metal Field / Excavator) keeps a 30s
        # window, but a ready 1-charge cooldown tool (Air Anchor / Chain Saw) starts
        # DRIFTING the instant it's off cooldown — so a ready tool beats a proc that
        # ISN'T about to expire (canonical "never sit on a tool"). Without this the
        # greedy fired a just-granted FMF over a ready Air Anchor during the 2-min
        # burst, stranding Air Anchor across the next Overheated window and losing a
        # full cast over the fight (the true-gear tool-drift residual; see the diag).
        # Only a proc within ~1.5 GCDs of expiry jumps the queue.
        soon = self.gcd_base_s * 1.5
        fmf_expiring = FMF in state.procs and 0 < state.procs[FMF] - t < soon
        exc_expiring = EXCAVATOR in state.procs and 0 < state.procs[EXCAVATOR] - t < soon
        if fmf_expiring:
            return FMF
        if exc_expiring:
            return EXCAVATOR
        # Cooldown tools. Air Anchor before Chain Saw matches the canonical MCH
        # opener (Reassemble pre-cast lands on Air Anchor at t=0).
        if state.cd_ready.get(AIR_ANCHOR, 0) <= t:
            return AIR_ANCHOR
        if state.cd_ready.get(CHAIN_SAW, 0) <= t:
            return CHAIN_SAW
        # Non-expiring procs next (the window can wait; a drifting tool can't).
        if FMF in state.procs and state.procs[FMF] > t:
            return FMF
        if EXCAVATOR in state.procs and state.procs[EXCAVATOR] > t:
            return EXCAVATOR
        # Drill last — the two-charge tool (Bioblaster, its AoE form, at high N).
        if state.charges.get(DRILL, 0) >= 1:
            return self._maybe_bioblaster(state)
        # Heated combo — or Scattergun, the AoE filler, once it out-VALUES the
        # combo at 3+ targets (battery/heat-aware, see `_maybe_scattergun`).
        return self._maybe_scattergun(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """Every strategically-distinct GCD at this slot — the tool-ordering forks
        that shift battery/heat GENERATION timing, the lever behind the measured
        Queen battery-timing residual (a greedy tool order can strand battery a
        better order would bank before a Queen window). Greedy's pick is first, so
        the beam's floor is the greedy line (`beam_perfect` is additionally guarded
        >= the refined greedy ceiling). Inside an Overheated window the slot is
        forced (Blazing Shot, or the end-of-fight interrupt) — no fork; widen here
        only if a future gate needs more raise."""
        self._expire_overheat(state)
        if state.overheated_stacks > 0:
            return [self.pick_gcd(state, params)]
        t = state.t
        soon = self.gcd_base_s * 1.5
        fmf_live = FMF in state.procs and state.procs[FMF] > t
        exc_live = EXCAVATOR in state.procs and state.procs[EXCAVATOR] > t
        cands: list[int] = []
        # Greedy priority order (mirrors pick_gcd), then every other available
        # option: expiring procs, ready 1-charge tools, live procs, Drill, combo.
        if fmf_live and state.procs[FMF] - t < soon:
            cands.append(FMF)
        if exc_live and state.procs[EXCAVATOR] - t < soon:
            cands.append(EXCAVATOR)
        if state.cd_ready.get(AIR_ANCHOR, 0) <= t:
            cands.append(AIR_ANCHOR)
        if state.cd_ready.get(CHAIN_SAW, 0) <= t:
            cands.append(CHAIN_SAW)
        if fmf_live:
            cands.append(FMF)
        if exc_live:
            cands.append(EXCAVATOR)
        if state.charges.get(DRILL, 0) >= 1:
            cands.append(self._maybe_bioblaster(state))   # greedy pick first
            if self._bioblaster_beats_drill(self._n(t)):
                cands.append(DRILL)        # keep ST Drill as the near-crossover fork
        # Heated combo step, plus the Scattergun fork ONLY where it's competitive
        # (3+ targets): below that the combo strictly out-values it, so forking
        # Scattergun would just risk the 2-target AoE bug. At 3+ the beam still
        # weighs the combo's battery vs Scattergun's heat (the genuine divergence).
        combo_gcd = (HEATED_SPLIT, HEATED_SLUG, HEATED_CLEAN)[state.combo_step]
        if self._scattergun_beats_combo(self._n(t)):
            cands.append(SCATTERGUN)       # greedy pick first
        cands.append(combo_gcd)
        seen: set[int] = set()
        return [c for c in cands if not (c in seen or seen.add(c))]

    def beam_signature(self, state: SimState):
        """Diversity-dedup key (see engine.beam_search) AND the exact solver's
        default `dominance_key`: the full future-relevant MCH state, so two lines
        that reached the same gauge/cooldown position via a different tool order
        collapse to the higher-scoring one — losslessly. Battery and heat are
        CAP-bounded (overcap is non-monotone) so they live here as categorical
        values, never in a dominance vector. `queen_battery_spent` is score-like
        (already counted by the prune key via `final_aux`) and stays out. An open
        Wildfire window's remaining payload capacity is carried by `wf_hits`
        (its position by the WF cooldown offset), so no history term is missing."""
        t = state.t
        return (
            round(t, 2), state.heat, state.battery, state.overheated_stacks,
            round(max(0.0, state.overheated_window_end - t), 2),
            state.free_hypercharges, state.combo_step,
            round(max(0.0, state.procs.get(FMF, 0.0) - t), 2),
            round(max(0.0, state.procs.get(EXCAVATOR, 0.0) - t), 2),
            round(state.charges.get(DRILL, 0.0), 3),
            round(state.charges.get(REASSEMBLE, 0.0), 3),
            round(state.charges.get(DOUBLE_CHECK, 0.0), 3),
            round(state.charges.get(CHECKMATE, 0.0), 3),
            round(max(0.0, state.cd_ready.get(AIR_ANCHOR, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(CHAIN_SAW, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(BARREL_STABILIZER, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(WILDFIRE, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(HYPERCHARGE, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(QUEEN, 0.0) - t), 2),
            state.tincture_used,
            round(max(0.0, state.tincture_cd_ready - t), 1),
            state.wf_hits if t <= state.wf_cast_t + 10.0 else 0,
        )

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """Top-K ranking key, O(1) from the incremental running score (NOT a
        re-scan of the timeline — what keeps the width-64 beam fast). Equals the
        scorer's buff-agnostic flat pass (potency x crit-DH + WF payload hits +
        per-summon deliverable Queen battery; the Flamethrower downtime tick and
        the pot multiplier are intentionally omitted) plus an admissible credit
        for BANKED battery, so a line investing gauge toward a later Queen isn't
        pruned before she pays off. Buff-agnostic by design — the engine's final
        selection re-scores the survivors under `buff_intervals`, so ranking
        only steers survival, never the result."""
        return state._score_flat + state.battery * md.BATTERY_VALUE_P_PER_UNIT

    # --- Exact-solver seam (optimal.solve_optimal) --------------------------
    # `legal_gcds` defaults to the dense `gcd_candidates` above and
    # `dominance_key` to the (lossless) `beam_signature`; added here: the Queen
    # weave fork, a fast clone, and the O(1) incremental `exact_g`/`terminal_g`.
    # Battery/heat stay categorical (in the signature) — overcap is
    # non-monotone, so neither may join a Pareto `dominance_vector`.

    def ogcd_candidates(self, state: SimState, params) -> list:
        """Fork the ONE oGCD decision the GCD level can't see: summon Queen now
        vs hold her (banking battery toward a later, bigger summon). Everything
        else stays the greedy weave — the `_optimal_best` guard against the
        beam covers any residual oGCD-timing value."""
        greedy = self.pick_ogcd(state, params)
        if greedy == QUEEN:
            return [QUEEN, None]
        return [greedy]

    def clone(self, state: SimState) -> SimState:
        """Field-aware shallow clone for the hot DP loop: copy the mutated
        containers; the read-only downtime / buff intervals are shared and the
        scalar gauge/flag fields ride the shallow copy."""
        new = copy.copy(state)
        new.procs = dict(state.procs)
        new.charges = dict(state.charges)
        new.cd_ready = dict(state.cd_ready)
        new.timeline = list(state.timeline)
        return new

    def dominance_key(self, state: SimState):
        """Categorical Pareto bucket for the DP — a strict SUBSET of `beam_signature`:
        only the fields that are non-monotone or change the legal-move set, so two
        states must match them exactly to be comparable. Heat and battery stay here
        (overcap is non-monotone — a lump-spent gauge at the cap can make a higher
        level WORSE by wasting future generation, so they must NOT enter the vector);
        likewise the Overheated window (it forces Blazing Shot), `free_hypercharges`
        and the FMF/Excavator proc remainders (move-set gates), the combo step, the
        open-Wildfire payload capacity (`wf_hits`), and the tincture-marker state. The
        monotone tool/burst cooldowns + charges move to `dominance_vector`, which is
        what shrinks MCH's otherwise-enormous per-layer frontier. (`beam_signature`
        keeps the FULL tuple — the beam's diversity-dedup needs every field; only the
        DP splits key from vector.)"""
        t = state.t
        return (
            round(t, 2), state.heat, state.battery, state.overheated_stacks,
            round(max(0.0, state.overheated_window_end - t), 2),
            state.free_hypercharges, state.combo_step,
            round(max(0.0, state.procs.get(FMF, 0.0) - t), 2),
            round(max(0.0, state.procs.get(EXCAVATOR, 0.0) - t), 2),
            state.tincture_used,
            round(max(0.0, state.tincture_cd_ready - t), 1),
            state.wf_hits if t <= state.wf_cast_t + 10.0 else 0,
        )

    def dominance_vector(self, state: SimState) -> tuple:
        """The MONOTONE-GOOD resources, signed so larger is always weakly better (a
        state ahead on score AND every component dominates): the single-charge
        tool/burst cooldowns (negated remaining → readier is larger) and the
        multi-charge counts (more charges → larger). Each is replayable by a
        readier/more-charged dominator and never forbids a move; unlike heat/battery
        none has overcap non-monotonicity — a single cooldown just sits ready, and a
        charge pool is pointwise-monotone under the shared capped regen
        (`min(cap, c + r·t)` preserves order) while being spent one cast at a time, so
        an extra charge is pure optionality. Losslessness is pinned by the `_ExactKey`
        invariant test (test_optimal_solver)."""
        t = state.t
        return (
            -round(max(0.0, state.cd_ready.get(AIR_ANCHOR, 0.0) - t), 2),
            -round(max(0.0, state.cd_ready.get(CHAIN_SAW, 0.0) - t), 2),
            -round(max(0.0, state.cd_ready.get(BARREL_STABILIZER, 0.0) - t), 2),
            -round(max(0.0, state.cd_ready.get(WILDFIRE, 0.0) - t), 2),
            -round(max(0.0, state.cd_ready.get(HYPERCHARGE, 0.0) - t), 2),
            -round(max(0.0, state.cd_ready.get(QUEEN, 0.0) - t), 2),
            round(state.charges.get(DRILL, 0.0), 3),
            round(state.charges.get(REASSEMBLE, 0.0), 3),
            round(state.charges.get(DOUBLE_CHECK, 0.0), 3),
            round(state.charges.get(CHECKMATE, 0.0), 3),
        )

    def admissible_remaining(self, state: SimState) -> float:
        """Admissible (never-underestimating) upper bound on the additional score
        reachable from `state` to fight end — the B&B prune the +inf default disables.
        Buff-agnostic but tincture-inflated, matching `exact_g`'s agnostic-pot-aware
        objective: multiplying the whole bound by the pot multiplier keeps it an
        over-estimate once the in-sim pot multiplies casts. Every premium count is
        cooldown-gated; the rest of the GCD budget fills at the richest filler;
        Wildfire payloads and Queen battery are bounded by their generators.

        Deliberately LOOSE (MCH's GCD count varies with the 1.5s Overheated cadence and
        its per-GCD potency spans 240-900, so a tight per-slot ceiling isn't available
        the way SAM's is) but SOUND — the `dominance_vector` split is MCH's primary
        frontier lever; this bound only trims the hopeless tail. Admissibility is pinned
        by the strengthened invariant test (`assert_solver_invariants`)."""
        rem = state.fight_duration_s - state.t
        if rem <= 0.0:
            return 0.0
        # Under an AoE schedule each cast is worth its per-target (cleaved) potency,
        # which EXCEEDS the single-target potencies this bound is built from — so the
        # bound would under-estimate (inadmissible). Disable it there (return +inf): the
        # lossless dominance keeps the DP exact, just unpruned, exactly as before this
        # bound existed. `_optimal_best` runs the DP under AoE (unlike SAM, which routes
        # AoE to the beam), so this guard is what keeps that path's "provable" optimum
        # honest. The bound matters on the single-target axis anyway.
        if any(n >= 2 for _s, _e, n in self.mt_schedule):
            return float("inf")
        # Upper bound on remaining GCD slots: nothing fits more than the 1.5s
        # Overheated Blazing Shot cadence, so count at the fastest possible GCD.
        min_gcd = min(self.gcd_base_s, self.gcd_overheated_s)
        n = int(math.ceil(rem / min_gcd)) + 1
        # Premium tool casts, cooldown-gated (each lands in a GCD slot).
        n_air = 1 + int(rem / md.COOLDOWNS[AIR_ANCHOR][0])
        n_saw = 1 + int(rem / md.COOLDOWNS[CHAIN_SAW][0])
        n_exc = n_saw                                  # Excavator gated by Chain Saw
        n_drill = int(state.charges.get(DRILL, 2.0)) + int(rem / md.COOLDOWNS[DRILL][0]) + 1
        n_fmf = 1 + int(rem / md.COOLDOWNS[BARREL_STABILIZER][0])
        # Fill the GCD budget highest-value-first. The per-cast ceilings are fixed-
        # ordered (FMF 900 > tool 660 > filler 420, all crit-DH'd), so the "top n" is a
        # closed-form three-tier fill — no per-state sort in the hot DP loop.
        n_tools = n_air + n_saw + n_exc + n_drill
        if n <= n_fmf:
            gcd_ub = n * _UB_FMF_P
        elif n <= n_fmf + n_tools:
            gcd_ub = n_fmf * _UB_FMF_P + (n - n_fmf) * _UB_TOOL_P
        else:
            gcd_ub = (n_fmf * _UB_FMF_P + n_tools * _UB_TOOL_P
                      + (n - n_fmf - n_tools) * _UB_FILLER_P)
        # Wildfire payloads (<=6 hits x 240 each), bounded by the 120s cooldown.
        n_wf = 1 + int(rem / md.COOLDOWNS[WILDFIRE][0])
        wf_ub = n_wf * 6 * 240.0
        # Queen pet potency: current banked battery + an upper bound on future battery
        # generation (Air Anchor / Chain Saw / Excavator +20 each, Heated Clean +10).
        n_clean = int(n / 3) + 1
        battery_gen = (n_air + n_saw + n_exc) * 20 + n_clean * 10
        queen_ub = (state.battery + battery_gen) * md.BATTERY_VALUE_P_PER_UNIT
        tinct = self.tincture_spec.multiplier if self.tincture_spec else 1.0
        return (gcd_ub + wf_ub + queen_ub) * tinct

    def exact_g(self, state: SimState, score_fn) -> float:
        """Exact prefix score, O(1) from the incremental accumulators — the DP's
        per-child cost (the default re-scan was the measured bottleneck behind
        the duration gate; see `_DP_MAX_DURATION_S`). Buff-agnostic but
        POT-AWARE, matching the canonical scorer's `buff_intervals=None` branch:
        `_g_main` carries pass 1 + WF payload + the FT tick (each scaled by the
        pot multiplier at its snapshot time); the Queen term reproduces the
        scorer's redistribution — per-summon raw battery x its summon-time
        multiplier, uniformly rescaled so the battery total equals the
        deliverable `queen_battery_spent`. A raid-buff-aware solve re-scores
        (the DP only runs the agnostic axis, so this stays O(1) where it
        matters)."""
        if state.buff_intervals:
            return score_fn(state.timeline, self.final_aux(state),
                            state.buff_intervals)
        g = state._g_main
        if state.tincture_used:
            # Pot marker in the timeline -> the scorer takes its per-summon
            # (overlay) Queen path even with no raid buffs.
            if state._q_raw > 0:
                g += (state.queen_battery_spent / state._q_raw) \
                    * state._q_rawm * md.BATTERY_VALUE_P_PER_UNIT
        else:
            g += state.queen_battery_spent * md.BATTERY_VALUE_P_PER_UNIT
        return g

    def terminal_g(self, state: SimState, score_fn) -> float:
        """Score of a COMPLETE rotation — identical to `exact_g`: MCH has no
        end-of-fight convention (no trailing DoT), so the prefix score IS the
        terminal score. Must equal the canonical scorer on the produced timeline
        (pinned by the exactness test)."""
        return self.exact_g(state, score_fn)

    def pick_ogcd(self, state: SimState, params):
        """Highest-priority oGCD available right now, or None."""
        t = state.t
        fw = params.forbidden_windows
        # Hypercharge — converts heat into the Blazing Shot chain
        if state.cd_ready.get(HYPERCHARGE, 0) <= t:
            if state.heat >= md.HYPERCHARGE_MIN_HEAT or state.free_hypercharges > 0:
                if not is_forbidden(HYPERCHARGE, t, fw):
                    return HYPERCHARGE
        # Wildfire — paired with Hypercharge. WF's payload counts the weaponskills
        # in its 10s window (capped at 6); only a fresh Overheated window (the 1.5s
        # Blazing Shot chain) fits 6, so fire WF only when Hypercharge has just been
        # used (>=4 stacks left). Off a fresh Hypercharge it catches the full chain;
        # firing it bare (e.g. the t=0 opener, before any heat) wastes ~2 hits. Held
        # on cooldown until the next Hypercharge otherwise — a small CD drift that
        # buys a full 6-hit payload, exactly what top parses do.
        if state.cd_ready.get(WILDFIRE, 0) <= t and state.overheated_stacks >= 4 \
                and not is_forbidden(WILDFIRE, t, fw):
            return WILDFIRE
        # Barrel Stabilizer on cooldown — grants free Hypercharge + FMF
        if state.cd_ready.get(BARREL_STABILIZER, 0) <= t \
                and not is_forbidden(BARREL_STABILIZER, t, fw):
            return BARREL_STABILIZER
        # Queen: cast at params.queen_cast_battery battery normally; in the
        # end-of-fight window drop the threshold to the 50-battery minimum so
        # residual gauge isn't wasted (battery -> potency is linear at 46p).
        # Only summon when she can actually hit the target: a Queen swallowed by
        # an imminent boss-untargetable window or by the fight's end does ~zero,
        # since even her manually-fired finishers can't land. She IS summoned
        # when she can salvage a finisher (Overdrive) — that summon is just
        # credited a fraction (`queen_deliverable_fraction`), and the refinement
        # pass holds her past a downtime window when full delivery scores higher.
        if state.cd_ready.get(QUEEN, 0) <= t and state.battery >= 50 \
                and not is_forbidden(QUEEN, t, fw) \
                and self._queen_can_deliver(state, t):
            end_of_fight = (state.fight_duration_s - t) < params.queen_eof_window_s
            if end_of_fight:
                return QUEEN
            if state.buff_intervals:
                # Buff-aware: summon at the 50 floor unless banking battery toward
                # a reachable raid window scores better (she snapshots the buff at
                # summon). `queen_cast_battery` is bypassed here — banking is the
                # timing lever, not a flat cutoff.
                if not self._queen_should_bank(state, t):
                    return QUEEN
            elif state.battery >= params.queen_cast_battery:
                return QUEEN
        # Reassemble — look ahead at what the next GCD will be. Hold unless it's
        # a valid tool target, or we're about to cap (regen would be wasted).
        if state.charges.get(REASSEMBLE, 0) >= 1:
            next_gcd = self.pick_gcd(state, params)
            will_target_tool = next_gcd in _VALID_REASSEMBLE_TARGETS
            about_to_cap = state.charges[REASSEMBLE] >= 1.95
            if will_target_tool or about_to_cap:
                return REASSEMBLE
        # DC / CM as filler weaves
        if state.charges.get(DOUBLE_CHECK, 0) >= 1:
            return DOUBLE_CHECK
        if state.charges.get(CHECKMATE, 0) >= 1:
            return CHECKMATE
        # Defensive / utility oGCDs (Tactician etc.) are intentionally not fired.
        return None

    def _queen_can_deliver(self, state: SimState, t: float) -> bool:
        """True iff a Queen summoned at `t` can land at least her manually-fired
        finishers — i.e. she has > `QUEEN_OVERDRIVE_MIN_S` of targetable time
        before the boss leaves or the fight ends. Returning False makes the
        picker hold her: across a downtime window she's re-offered the moment
        uptime resumes; in the final dead seconds she's never summoned, so the
        residual battery is left unspent rather than dumped into air the boss
        isn't even present for."""
        return queen_deliverable_fraction(
            t, state.fight_duration_s, state.downtime_windows) > 0.0

    def _queen_should_bank(self, state: SimState, t: float) -> bool:
        """Buff-aware hold decision: True iff battery should be banked toward an
        upcoming, higher-multiplier raid-buff window instead of dumped now. Only
        consulted when `state.buff_intervals` is set (the buff-aware ceiling).

        Queen snapshots the multiplier at SUMMON for her whole ~17s payload, so
        landing her IN a window beats an out-of-window summon — provided banking
        doesn't overcap (stranding battery, the worse error). The overcap bound
        is the reach budget: battery headroom to the cap divided by the bank gen
        rate = how many seconds we can bank before wasting battery. Defers to the
        shared `reachable_richer_window` (False => summon now)."""
        max_lead = (md.BATTERY_CAP - state.battery) / _QUEEN_BANK_GEN_RATE_PER_S
        return engine.reachable_richer_window(
            t, state.buff_intervals, max_lead) is not None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        """Record the cast in the timeline and apply its state changes."""
        t = state.t
        state.timeline.append((t, ability_id))

        # Wildfire payload tracking (mirrors the scorer: weaponskills strictly
        # after the WF cast, within 10s, capped at 6) + the incremental
        # buff-agnostic running score for `beam_prune` (per-hit 240 credited as
        # banked; the flat potency/crit-DH pass below).
        if ability_id == WILDFIRE:
            state.wf_cast_t = t
            state.wf_hits = 0
        elif (ability_id in _WEAPONSKILL_IDS and state.wf_cast_t < t
                and t <= state.wf_cast_t + 10.0 and state.wf_hits < 6):
            state.wf_hits += 1
            state._score_flat += 240.0
            # Exact accumulator: the WF payload snapshots the multiplier at the
            # WILDFIRE CAST, not where the hit lands.
            state._g_main += 240.0 * self.pot_mult_at(state, state.wf_cast_t)

        base = potency_for(ability_id, self._n(t), md.JOB_DATA)
        if base > 0 and ability_id != WILDFIRE:
            mult = 1.0
            if ability_id in _WEAPONSKILL_IDS:
                reassembled = state._reassemble_until >= t
                if ability_id == FMF:
                    mult = md.GUARANTEED_CRIT_DH_MULT
                    if reassembled:                  # consumed but wasted
                        state._reassemble_until = -1e9
                elif reassembled:
                    mult = md.GUARANTEED_CRIT_DH_MULT
                    state._reassemble_until = -1e9   # consumed
            state._score_flat += base * mult
            state._g_main += base * mult * self.pot_mult_at(state, t)
        if ability_id == REASSEMBLE:
            state._reassemble_until = t + 5.0

        # Heat
        if ability_id in md.HEAT_GENERATORS:
            state.heat = min(md.HEAT_CAP, state.heat + md.HEAT_GENERATORS[ability_id])
        if ability_id in md.HEAT_SPENDERS and ability_id != HYPERCHARGE:
            state.heat = max(0, state.heat - md.HEAT_SPENDERS[ability_id])

        # Battery
        if ability_id in md.BATTERY_GENERATORS:
            state.battery = min(md.BATTERY_CAP,
                                state.battery + md.BATTERY_GENERATORS[ability_id])

        # Cooldown / charges (generic).
        apply_cooldown(state, self.cooldowns, ability_id)

        # Per-ability special effects
        if ability_id == HYPERCHARGE:
            state.overheated_stacks = 5
            state.overheated_window_end = t + HYPERCHARGE_WINDOW_S
            if state.free_hypercharges > 0:
                state.free_hypercharges -= 1
            else:
                state.heat = max(0, state.heat - md.HYPERCHARGE_MIN_HEAT)
        elif ability_id in (BLAZING_SHOT, AUTO_CROSSBOW):
            # Both spend an Overheated stack, but ONLY Blazing Shot reduces the
            # Double Check / Checkmate recast (wiki-verified: Auto Crossbow has no
            # such effect — it just applies its own recast to all weaponskills).
            state.overheated_stacks = max(0, state.overheated_stacks - 1)
            if ability_id == BLAZING_SHOT:
                for cd_id in (DOUBLE_CHECK, CHECKMATE):
                    recast, max_ch = md.COOLDOWNS[cd_id]
                    bonus_charge = md.BLAZING_SHOT_CDR_S / recast
                    state.charges[cd_id] = min(
                        max_ch, state.charges.get(cd_id, 0) + bonus_charge)
        elif ability_id == BARREL_STABILIZER:
            state.free_hypercharges += 1
            state.procs[FMF] = t + md.PROC_DURATION_S
        elif ability_id == CHAIN_SAW:
            state.procs[EXCAVATOR] = t + md.PROC_DURATION_S
        elif ability_id == EXCAVATOR:
            state.procs.pop(EXCAVATOR, None)
        elif ability_id == FMF:
            state.procs.pop(FMF, None)
        elif ability_id == QUEEN:
            # Credit only the battery she can actually deliver: a summon swallowed
            # by an imminent downtime window or the fight's end banks a fraction,
            # never the full gauge (see `queen_deliverable_fraction`).
            frac = queen_deliverable_fraction(
                t, state.fight_duration_s, state.downtime_windows)
            state.queen_battery_spent += state.battery * frac
            state._score_flat += (state.battery * frac
                                  * md.BATTERY_VALUE_P_PER_UNIT)
            # Exact accumulator: the scorer's per-summon path credits RAW battery
            # x the multiplier at summon, uniformly rescaled to the deliverable
            # total — track both sums so `exact_g` can apply the same formula.
            state._q_raw += state.battery
            state._q_rawm += state.battery * self.pot_mult_at(state, t)
            state.battery = 0
            state.cd_ready[QUEEN] = t + QUEEN_RECAST_S
        elif ability_id == BIOBLASTER:
            # AoE tool — shares Drill's 2-charge pool (the pick gates on Drill);
            # Bioblaster itself isn't in COOLDOWNS, so decrement Drill here.
            state.charges[DRILL] = max(0.0, state.charges.get(DRILL, 2.0) - 1)

        # Combo tracking
        if ability_id == HEATED_SPLIT:
            state.combo_step = 1
        elif ability_id == HEATED_SLUG:
            state.combo_step = 2 if state.combo_step == 1 else 0
        elif ability_id == HEATED_CLEAN:
            state.combo_step = 0 if state.combo_step == 2 else 0
        elif ability_id == SCATTERGUN:
            state.combo_step = 0   # the AoE filler breaks the Heated combo

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # Squeeze one Flamethrower tick at a boss-untargetable edge where it's
        # mechanically possible. As the boss goes untargetable you can fire
        # Flamethrower (a GCD) so a tick lands in the retarget gap as it
        # reappears. Two COMPUTABLE gates apply (the rest — no alternative add
        # to hit, fight-specific context — are too situational to infer, so the
        # UI confirms each candidate with the user):
        #   1. the window must exceed one GCD, else Flamethrower's own 2.5s recast
        #      delays your return to the boss for more than the tick is worth;
        #   2. it's a GCD, so the press must be >= one GCD after the last one.
        # It's ONLY ever a gain here — in uptime a real GCD always beats it — so
        # we credit a single tick at the boundary and nowhere else. Appended
        # directly (not via apply_cast): no heat/battery/combo side-effects and
        # no GCD-clock cost; it fills otherwise-dead time.
        s, e = win_start, win_end
        press_t = max(s, state.last_gcd_t + GCD_BASE_S)
        if (e - s) > GCD_BASE_S and press_t < e:
            state.timeline.append((press_t, FLAMETHROWER))
            # Exact accumulator: the canonical scorer's pass 1 treats the tick
            # like any other GCD — it consumes (and crits off) a live Reassemble
            # and takes the pot multiplier at the press. `_score_flat` stays
            # FT-free by design (prune-only), but the consumption flag is shared:
            # the scorer would not crit the next weaponskill either.
            mult = 1.0
            if state._reassemble_until >= press_t:
                mult = md.GUARANTEED_CRIT_DH_MULT
                state._reassemble_until = -1e9
            state._g_main += (md.POTENCIES[FLAMETHROWER] * mult
                              * self.pot_mult_at(state, press_t))

    def final_aux(self, state: SimState) -> float:
        # Total DELIVERABLE Queen battery (already discounted per summon). The
        # scorer turns this into pet potency at BATTERY_VALUE_P_PER_UNIT.
        return state.queen_battery_spent

    def sweep_params(self, extra_forbidden):
        for qb in _SWEEP_QUEEN_BATTERIES:
            for eof in _SWEEP_QUEEN_EOF_WINDOWS:
                for mw in _SWEEP_MAX_WEAVES:
                    yield SimParams(
                        queen_cast_battery=qb,
                        queen_eof_window_s=eof,
                        max_weaves_per_gcd=mw,
                        forbidden_windows=extra_forbidden,
                    )


_MODEL = MachinistRotationModel()

# Beam width for the GCD-perfect search over the tool-ordering forks. MCH forks
# are sparse (Overheated slots are forced, filler slots mostly single-option), so
# a PLD-sized beam converges; the `beam_signature` dedup keeps the width on
# distinct gauge/cooldown lines. `beam_perfect` is guarded >= the refined greedy
# ceiling, so the beam can only raise it.
_BEAM_WIDTH = 64


def _model_for(sim_context) -> MachinistRotationModel:
    """The model for this run. A `CeilingContext` with a faster-than-constant
    `gcd_base_s` (per-player Skill Speed) and/or a `MultiTargetContext` (the AoE
    N(t) schedule) builds a bound model; otherwise the shared singleton
    (byte-identical)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
    if gcd is None and not mt_schedule:
        return _MODEL
    return MachinistRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to
    a multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`; `aux` is Queen battery). Buff-aware when given.
    Empty schedule -> single target, byte-identical to the pre-AoE scorer. The
    model's incremental `_g_main`/`_score_flat` use the SAME schedule (via
    `self._n`), so the DP's `exact_g` stays exact under AoE too."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.machinist.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, aux, buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests / DP helpers call `_score`).
_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, queen_battery_spent).

    `timeline` is `[(cast_time_s, ability_id)]` sorted by time;
    `queen_battery_spent` is the total DELIVERABLE battery across all Queen casts
    (each summon's battery x its `queen_deliverable_fraction`), used downstream to
    compute Queen pet potency (x 46p / battery) — so a Queen that can't hit the
    target never inflates the ceiling."""
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
    """Sweep SimParams, return the (timeline, queen_battery_spent) whose
    score_delivered_potency is highest. Guaranteed >= the single-point
    simulate_idealized(default) since defaults are inside the sweep set."""
    model = _model_for(sim_context)
    timeline, aux, _params, _score_v = engine.sweep_best(
        model, _make_score(model.mt_schedule), fight_duration_s,
        downtime_windows or [], buff_intervals=buff_intervals)
    return timeline, aux


def _beam_best(model, score, fight_duration_s, downtime, buff_intervals):
    """The beam ceiling: the engine's burst-timing refinement + a beam search over
    the tool-ordering GCD forks on top (guarded never to fall below the refined
    greedy ceiling). The exact solver's incumbent seed + the buff-alignment /
    long-fight half of `_optimal_best`.

    Buff-aware, it ALSO evaluates the raid-window-aligned burst — Wildfire +
    Barrel Stabilizer held into the buff windows, with Queen banking active — via
    the shared `canonical_aligned_max_guard`, max'd against the free optimum. The
    free beam under-searches the WF hold: `refine` decides WF timing via
    `run_rotation`, where banking is OFF (only `beam_search` exposes
    `buff_intervals` to the picker), so it rejects the hold before the beam can
    pair it with banked Queen. The held variant is the true optimum on some
    durations and genuinely worse on others (where early WF wins); the max guard
    captures the gain without regressing. Agnostic path is byte-identical."""
    free = engine.beam_perfect(model, score, fight_duration_s, downtime,
                               buff_intervals, width=_BEAM_WIDTH)
    return engine.canonical_aligned_max_guard(
        model, score, fight_duration_s, downtime, buff_intervals, free,
        beam_width=_BEAM_WIDTH)


# Wall-clock cap for one exact solve (guards a slow short-fight solve; the
# duration gate below keeps long fights off the DP entirely). A time-out
# returns the best complete leaf, guarded >= the beam seed. Sized ~1.6x the
# measured at-gate solve (90s fight ~15s for the slowest weave budget) so a
# slower machine still PROVES instead of wasting the whole box on a timed-out run.
_DP_TIME_BOX_S = 24.0

# Live duration gate for the exact DP, sized by measured wall time. The
# `dominance_vector` split (the monotone tool/burst cooldowns + charges moved off
# the categorical key) collapsed MCH's per-layer frontier ~150x — the state space
# was the wall, not the +inf bound — so the bench (`bench_mch_dp.py`) is now:
# 60s ~0.7-0.9s, 80s ~4-7s, 85s ~6-12s, 90s ~8-15s, 100s ~12-28s, 120s unproven at
# a 60s box. The gate sits where BOTH weave budgets prove well within `_DP_TIME_BOX_S`
# (90s mw=3 ~15s < 24s). The admissible `admissible_remaining` bound is in place but
# MCH's loose per-GCD ceiling (Overheated GCD count + 240-900 potency spread) only
# trims the tail — the vector is the lever. Real kills (390-650s) still never hit the
# DP (the ~2.5x-per-+10s frontier keeps 600s intractable); the live ceiling there is
# the beam, and `_optimal_best` guards `max(DP, beam)` so the gate raise can only make
# the ceiling tighter, never push a pull over 100%.
_DP_MAX_DURATION_S = 90.0


@lru_cache(maxsize=64)
def _dp_throughput_cached(duration_key: float,
                          downtime_tuple: tuple[tuple[float, float], ...],
                          sim_context) -> tuple[tuple[tuple[float, int], ...], float]:
    """The PROVEN buff-agnostic throughput optimum for one (duration, downtime)
    (buff-independent, so the strict / observed / master scenarios share one solve).
    Seeds the B&B incumbent from the beam (a strong lower bound -> tight pruning),
    solves each weave budget keeping the best, and guards >= the seed so a time-out
    can never fall below the beam. Queen timing is part of the search (the
    `[QUEEN, None]` weave fork); `queen_cast_battery` stays at the legal minimum 50
    so the fork — not the greedy threshold — owns the hold decision.

    Scope of the proof: optimal over {dense GCD forks x Queen weave fork x greedy
    other weaves}. Hypercharge / Wildfire *holds* (which `refine` reaches via
    forbidden windows — worth real value near fight end) are NOT in the DP's move
    set, which is why the seed guard + `_optimal_best`'s max against the beam are
    load-bearing, exactly as on SAM/RPR."""
    downtime = list(downtime_tuple)
    model = _model_for(sim_context)
    score = _make_score(model.mt_schedule)
    seed_tl, seed_aux = _beam_best(model, score, duration_key, downtime, None)
    best = (score(seed_tl, seed_aux, None), tuple(seed_tl), seed_aux)
    for mw in _SWEEP_MAX_WEAVES:
        params = SimParams(max_weaves_per_gcd=mw)
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
    """The MCH ceiling. The beam (buff-aware, raid-burst aligned) is the base; on
    fights <= `_DP_MAX_DURATION_S` the exact buff-agnostic DP also runs and, being
    a provable upper bound on the agnostic axis, max's against the beam. Neither
    axis ever regresses. Buff-agnostic short fights skip the outer beam: the DP
    path computes (and is guarded >=) the identical agnostic beam as its incumbent
    seed."""
    if fight_duration_s > _DP_MAX_DURATION_S or buff_intervals:
        model = _model_for(sim_context)
        score = _make_score(model.mt_schedule)
        beam_tl, beam_aux = _beam_best(model, score, fight_duration_s, downtime,
                                       buff_intervals)
        if fight_duration_s > _DP_MAX_DURATION_S:
            return beam_tl, beam_aux
        dp_tl, dp_aux = _dp_throughput(fight_duration_s, downtime, sim_context)
        if score(dp_tl, dp_aux, buff_intervals) >= score(beam_tl, beam_aux,
                                                          buff_intervals):
            return dp_tl, dp_aux
        return beam_tl, beam_aux
    return _dp_throughput(fight_duration_s, downtime, sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: sweep + burst-timing refinement + the
    tool-ordering beam, max'd against the exact DP+B&B optimum on short fights
    (`_optimal_best`). Buff-aware when `buff_intervals` is given."""
    return _optimal_best(fight_duration_s, downtime_windows or [],
                         buff_intervals, sim_context)


def _canonical_burst_forbidden(
        buff_intervals: list[tuple[float, float, float]],
        ) -> tuple[tuple[int, float, float], ...]:
    """MCH canonical anchors (Wildfire + Barrel Stabilizer) held into each
    full-stack buff window. Thin wrapper over the engine helper, kept
    module-local so the canonical-sim tests can address it directly."""
    return engine.canonical_burst_forbidden(buff_intervals, _CANONICAL_ALIGN_ANCHORS)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows
    (the canonical 'hold for the window' line). Falls back to the throughput
    optimum when there are no party buffs to align to."""
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
