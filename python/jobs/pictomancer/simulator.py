"""Idealized Pictomancer rotation — the PCT `RotationModel` for the shared engine.

The third **caster** simulator (after RDM / BLM), and the first job whose
optimization axis is a **downtime-painting economy**. The time loop, downtime /
weave / charge handling, parameter sweep, local-search refinement and canonical
buff alignment all live in `jobs/_core/sim/engine.py`. This module supplies only
the PCT-specific rotation: the aetherhue chain, the palette / paint economy, the
canvas -> muse -> portrait ladder, Hammer Time, and the Starry Muse burst
(Hyperphantasia / Inspiration / Starstruck / Rainbow Bright). The four
`simulate_*` shims at the bottom bind this model to the engine (names kept so
the sidecar / scorer / tests call them unchanged).

PCT-specific structure encoded:

- **Mixed SpS-scaled recasts** (the Viper per-ability pattern, hasteable): RGB
  spells run the 2.5s standard, the subtractive CMY spells + Comet run 3.3s,
  in-combat motifs 4.0s, Rainbow Drip 6.0s — all scaled together by the
  per-player Spell Speed factor, then by the Inspiration window (-25% on the
  damaging spells while Hyperphantasia stacks remain — probe-verified on both
  cast and recast). Instant-vs-hardcast is captured in `gcd_duration` (never
  `gcd_slot`) so the greedy loop, the beam and any exact seam get identical
  timing.
- **Motif painting**: 0-potency 3s-cast GCDs that load the canvas a Muse then
  consumes. Pre-pull paints all three (instant out of combat, not logged — the
  player's log shows the same); `on_downtime_window` re-paints empty canvases
  inside downtime windows (motifs are self-targeted — probe: M12S-P1's ~6.6s
  gap is filled with motif casts); in uptime an empty canvas is repainted in
  the filler priority slot (painting early is free — canvas is storage — so
  the repaint happens as soon as nothing better exists).
- **The Living Muse pool** (3 charges / 40s) is keyed on POM_MUSE in `cooldowns`
  so the engine's generic multi-charge regen runs — including through downtime;
  the Winged / Clawed / Fanged variants spend the shared pool manually here.
  The creature stage cycles Pom -> Wing -> Claw -> Maw; Winged completes the
  Moogle portrait (Mog of the Ages), Fanged the Madeen (Retribution), sharing
  one 30s portrait recast keyed on MOG.
- **White paint is banked, not dumped**: a Holy in White (570) displaces a
  marginal chain GCD worth ~680 amortized, so the greedy line spends paint only
  at the fight tail; the beam explores Holy forks everywhere (`gcd_candidates`)
  so any fight where a dump IS optimal is still found.
- **Hammer combo**: guaranteed crit+DH (tier-measured x2.26), folded into the
  incremental beam score exactly as the delivered scorer prices it.

Out of scope for v1 (documented, intentionally not modeled):
- Swiftcast as DPS — every swiftable PCT cast is recast-bound (motif max(3,4)s,
  Drip max(4,6)s, RGB max(1.5,2.5), CMY max(2.3,3.3)), so an instant cast
  changes no slot length (probe-confirmed); it's movement utility the player
  holds, so the sim doesn't fire it.
- Star Prism's 0-damage auto follow-up (34682) — occupies no GCD slot; the
  delivered side scores it at 0.
- Phase-continuation entry state (M12S-P2 loaded opens) — wired during live
  calibration via `sim_context` (see scoring.py).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.pictomancer import data as pd


# --- Ability IDs (aliased from data for readability) -------------------------
FIRE        = pd.FIRE_IN_RED
AERO        = pd.AERO_IN_GREEN
WATER       = pd.WATER_IN_BLUE
BLIZZARD    = pd.BLIZZARD_IN_CYAN
STONE       = pd.STONE_IN_YELLOW
THUNDER     = pd.THUNDER_IN_MAGENTA
HOLY        = pd.HOLY_IN_WHITE
COMET       = pd.COMET_IN_BLACK
HAMMER_MOTIF = pd.HAMMER_MOTIF
STARRY_SKY_MOTIF = pd.STARRY_SKY_MOTIF
POM_MUSE    = pd.POM_MUSE
STRIKING_MUSE = pd.STRIKING_MUSE
STARRY_MUSE = pd.STARRY_MUSE
MOG         = pd.MOG_OF_THE_AGES
RETRIBUTION = pd.RETRIBUTION_OF_THE_MADEEN
STAR_PRISM  = pd.STAR_PRISM
SUBTRACTIVE = pd.SUBTRACTIVE_PALETTE
RAINBOW_DRIP = pd.RAINBOW_DRIP

# The aetherhue chains by step (0 -> 1 -> 2 -> 0). The CMY chain runs the same
# steps while Subtractive stacks are held.
_RGB_BY_STEP = (FIRE, AERO, WATER)
_CMY_BY_STEP = (BLIZZARD, STONE, THUNDER)
_RGB_AOE_SWAP = {FIRE: pd.FIRE_II_IN_RED, AERO: pd.AERO_II_IN_GREEN,
                 WATER: pd.WATER_II_IN_BLUE}
_CMY_AOE_SWAP = {BLIZZARD: pd.BLIZZARD_II_IN_CYAN, STONE: pd.STONE_II_IN_YELLOW,
                 THUNDER: pd.THUNDER_II_IN_MAGENTA}
# AoE II -> base transition equivalence (apply_cast folds the II ids onto the
# same chain / gauge effects).
_AOE_TO_BASE = {v: k for k, v in {**_RGB_AOE_SWAP, **_CMY_AOE_SWAP}.items()}
# Hammer combo by remaining stacks (3 -> Stamp, 2 -> Brush, 1 -> Polishing).
_HAMMER_BY_STACKS = {3: pd.HAMMER_STAMP, 2: pd.HAMMER_BRUSH, 1: pd.POLISHING_HAMMER}

# --- Rotation tuning ----------------------------------------------------------
PCT_GCD_S = 2.50            # base GCD (per-player Spell Speed threads via gcd_base_s)
# The measured pre-pull line: Rainbow Drip hardcast during the countdown, its
# snapshot landing at t~0; the 6s recast (begun at ~-3.5s) frees the GCD at
# ~+2.5s — probe: the second GCD's begincast sits at 2.54s on every cold open.
PREPULL_CHANNEL_T = -3.5
PREPULL_RESIDUAL_S = 2.5
# Picker thresholds.
STARSTRUCK_GUARD_S = 2.7    # force Star Prism before Starstruck lapses
TAIL_SPEND_S = 8.0          # fight-tail: dump paint (Holy beats an unfinished chain)
COMET_TAIL_S = 14.0         # fight-tail: Comet before the CMY chain it can't finish
# Hold an empty canvas for an upcoming downtime window instead of paying an
# uptime slot — the free-paint deferral (the M10S calibration finding: the sim
# arrived at every window fully painted and wasted the gap, while players
# arrive empty and paint 2-3 motifs free inside it).
DOWNTIME_DEFER_LEAD_S = 32.0
STARRY_DOWNTIME_HOLD_S = 12.0   # hold Starry when a gap starts this soon
MOTIF_PAYOFF_MIN_S = 5.0    # don't repaint a canvas whose muse can't fire in time
STARRY_MIN_PAYOFF_S = 4.0   # fire Starry even late (Star Prism alone pays)
DOWNTIME_EPS_S = 2.0

_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
_SIG_BUCKET_S = PCT_GCD_S   # decision-state timer bucket (~one GCD)

# Hold the burst enablers into raid-buff windows (refine / canonical alignment).
_ANCHORS: tuple[int, ...] = (STARRY_MUSE, SUBTRACTIVE, STRIKING_MUSE, MOG, RETRIBUTION)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    pd.JOB_DATA.tincture_main_stat, pd.JOB_DATA.tincture_role_coeff)

# Prune-credit rates for banked resources (admissible-ish steering only — the
# final beam selection always re-scores with the exact score_fn).
_PRUNE_PALETTE_P    = 22.0      # 50 palette -> the CMY+Comet upgrade cycle
_PRUNE_SUBTRACTIVE_P = 370.0    # a held CMY stack over the RGB filler it displaces
_PRUNE_BLACK_P      = 410.0     # a banked Comet over displaced filler
_PRUNE_HAMMER_P     = 780.0     # a hammer stack (x2.26 crit-DH) over filler
_PRUNE_MUSE_CHARGE_P = 800.0    # a banked Living Muse (free oGCD potency)
_PRUNE_STRIKING_P   = 2300.0    # a banked Striking charge -> 3 net hammers
_PRUNE_PORTRAIT_P   = {0: 0.0, 1: 1000.0, 2: 1100.0}
_PRUNE_STARSTRUCK_P = 1100.0
_PRUNE_BRIGHT_P     = 470.0     # a pending instant Drip over displaced filler
_PRUNE_HYPER_P      = 100.0     # ~haste value of one Inspiration stack


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """PCT picker tunables — only the shared knobs (max_weaves / forbidden_windows)."""
    pass


@dataclass
class SimState(SimStateBase):
    # Aetherhue chain position (0 -> Fire/Blizzard, 1 -> Aero/Stone, 2 -> Water/Thunder).
    hue_step: int = 0
    subtractive: int = 0          # CMY stacks (0-3)
    palette: int = 0              # 0-100 (GaugeModel.name == field convention)
    white_paint: int = 0          # 0-5
    black_paint: bool = False     # Monochrome Tones held (max 1)
    # Canvas -> muse ladder.
    creature_stage: int = 0       # 0 Pom -> 1 Wing -> 2 Claw -> 3 Maw (cyclic)
    creature_canvas: bool = False
    weapon_canvas: bool = False
    landscape_canvas: bool = False
    portrait: int = 0             # 0 none / 1 Moogle (Mog) / 2 Madeen (Retribution)
    # Hammer Time.
    hammer_stacks: int = 0
    hammer_end: float = float("-inf")
    # Starry Muse self effects.
    hyperphantasia: int = 0
    starstruck_end: float = float("-inf")
    rainbow_bright: bool = False
    bright_end: float = float("-inf")
    spectrum_free: bool = False
    # Captured in gcd_duration for weave_budget (the BLM pattern).
    instant_this_slot: bool = False
    # Incremental (raid-buff-agnostic, crit-DH-aware) running score for the O(1)
    # beam-prune key — the exact per-cast math `score_delivered_potency` runs
    # with raid buffs / tincture off.
    _score_flat: float = 0.0


class PictomancerRotationModel(engine.BaseRotationModel):
    cooldowns = pd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=PCT_GCD_S, cast_times=pd.CAST_TIMES)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = (STARRY_MUSE, SUBTRACTIVE, STRIKING_MUSE)
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=3 the
        # aetherhue chain swaps to its full-to-all "II" versions per slot
        # (closed-form potency comparison). Empty () -> single target,
        # byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed: scales the base recast, every per-ability
        # recast multiple AND the cast times by the same factor. None keeps the
        # tier constant, byte-identical. Inspiration haste multiplies on top
        # (in gcd_duration).
        self.gcd_base_s = PCT_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != PCT_GCD_S:
            factor = self.gcd_base_s / PCT_GCD_S
            self.timing = replace(
                PictomancerRotationModel.timing, gcd_recast_s=self.gcd_base_s,
                cast_times={k: v * factor for k, v in pd.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    # --- Lifecycle ------------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {POM_MUSE: 3.0, STRIKING_MUSE: 2.0}
        state.cd_ready = {STARRY_MUSE: 0.0, MOG: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Out-of-combat prep (instant motifs, not logged — matching real logs):
        # all three canvases painted, creature at Pom. The pre-pull Rainbow Drip
        # hardcast lands at t~0 (+1 white paint); its 6s recast frees the GCD
        # loop at ~2.5s, whose weave space carries the opener pot / Pom Muse /
        # Striking Muse (the engine weaves them from the first slots).
        state.creature_canvas = True
        state.weapon_canvas = True
        state.landscape_canvas = True
        state.creature_stage = 0
        state.timeline.append((PREPULL_CHANNEL_T, RAINBOW_DRIP))
        state._score_flat += potency_for(RAINBOW_DRIP, self._n(0.0), pd.JOB_DATA)
        state.white_paint = 1
        state.last_gcd_t = 0.0
        state.t = PREPULL_RESIDUAL_S * (self.gcd_base_s / PCT_GCD_S)

    # --- GCD timing -------------------------------------------------------------

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Per-ability cast + recast, both scaled by the per-player speed factor
        # (already folded into self.timing), then the Inspiration haste on the
        # whole slot (max(a,b)*h == max(a*h, b*h) — the BLM Ley Lines identity,
        # so cast and recast are hasted symmetrically, as probed).
        f = self.gcd_base_s / PCT_GCD_S
        if gcd_id == RAINBOW_DRIP and state.rainbow_bright:
            cast = 0.0
            recast = self.gcd_base_s
        else:
            cast = self.timing._cast_time(gcd_id)
            recast = self.gcd_base_s * pd.RECAST_MULT.get(gcd_id, 1.0)
        state.instant_this_slot = cast <= 0.0
        slot = max(cast, recast)
        if state.hyperphantasia > 0 and gcd_id in pd.INSPIRED_IDS:
            slot *= pd.INSPIRATION_HASTE
        return slot

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        base = (self.timing.instant_weaves if state.instant_this_slot
                else self.timing.hardcast_weaves)
        return min(base, params.max_weaves_per_gcd)

    # --- GCD selection ------------------------------------------------------------

    def _chain_spell(self, state: SimState, cmy: bool) -> int:
        """The current aetherhue-chain GCD (RGB, or CMY while stacks are held),
        swapped to its full-to-all "II" version when the live target count makes
        it the higher potency (N>=3 for every pair)."""
        base = (_CMY_BY_STEP if cmy else _RGB_BY_STEP)[state.hue_step]
        n = self._n(state.t)
        if n >= 2:
            alt = (_CMY_AOE_SWAP if cmy else _RGB_AOE_SWAP)[base]
            if potency_for(alt, n, pd.JOB_DATA) > potency_for(base, n, pd.JOB_DATA):
                return alt
        return base

    def _next_paint_window(self, state: SimState) -> tuple[float, float] | None:
        """The next downtime window (within the defer lead) with room for at
        least one in-combat motif slot."""
        t = state.t
        slot = 4.0 * (self.gcd_base_s / PCT_GCD_S)
        for s, e in sorted(state.downtime_windows):
            if e <= t:
                continue
            if s - t > DOWNTIME_DEFER_LEAD_S:
                return None
            if e - max(s, t) >= slot + 0.5:
                return (max(s, t), e)
        return None

    def _motif_pick(self, state: SimState) -> int | None:
        """Repaint an empty canvas (painting early is free — canvas is storage),
        with two calibration-derived gates:

        * the CONSUMER must actually fire again before the fight ends — an
          uptime repaint whose muse/Striking/Starry never comes back is a dead
          4s slot (the M9S finding: 3 wasted tail repaints ~= the 101% breach);
        * an upcoming downtime window with paint capacity DEFERS the repaint —
          the window paints it free (the M10S finding: the greedy arrived at
          every window fully painted and wasted the gap). A canvas whose
          deferral would cap its consumer's charge pool (or drift Starry)
          paints now regardless."""
        t = state.t
        end = state.fight_duration_s
        remaining = end - t
        slot = 4.0 * (self.gcd_base_s / PCT_GCD_S)
        win = self._next_paint_window(state)
        capacity = int((win[1] - win[0]) // slot) if win is not None else 0

        wants: list[tuple[int, bool]] = []   # (motif_id, deferrable)
        if not state.creature_canvas and remaining > slot + 1.0:
            at_end = (state.charges.get(POM_MUSE, 0.0)
                      + remaining / pd.COOLDOWNS[POM_MUSE][0])
            if at_end >= 1.0:
                # Deferring must not cap the 3-charge muse pool while the empty
                # canvas blocks spending.
                caps = (win is not None
                        and state.charges.get(POM_MUSE, 0.0)
                        + (win[1] - t) / pd.COOLDOWNS[POM_MUSE][0]
                        > pd.COOLDOWNS[POM_MUSE][1] - 0.05)
                wants.append((pd.CREATURE_MOTIFS[state.creature_stage], not caps))
        if not state.weapon_canvas and remaining > slot + 3.0:
            at_end = (state.charges.get(STRIKING_MUSE, 0.0)
                      + remaining / pd.COOLDOWNS[STRIKING_MUSE][0])
            if at_end >= 1.0:
                caps = (win is not None
                        and state.charges.get(STRIKING_MUSE, 0.0)
                        + (win[1] - t) / pd.COOLDOWNS[STRIKING_MUSE][0]
                        > pd.COOLDOWNS[STRIKING_MUSE][1] - 0.05)
                wants.append((HAMMER_MOTIF, not caps))
        if not state.landscape_canvas and remaining > slot + STARRY_MIN_PAYOFF_S:
            if state.cd_ready.get(STARRY_MUSE, 0.0) <= end - STARRY_MIN_PAYOFF_S:
                # Deferring must not drift Starry: if it comes off cooldown
                # before the window closes, paint now.
                drifts = (win is not None
                          and state.cd_ready.get(STARRY_MUSE, 0.0) < win[1] - 2.0
                          and win[0] - t > 2.0)
                wants.append((STARRY_SKY_MOTIF, not drifts))
        if not wants:
            return None
        deferrable = sum(1 for _m, d in wants if d)
        # Paint now: every non-deferrable canvas first, then any overflow the
        # window can't fit.
        for mid, d in wants:
            if not d:
                return mid
        if deferrable > capacity:
            return wants[0][0]
        return None

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        remaining = state.fight_duration_s - t
        # 0. Star Prism about to lapse — never forfeit the 1100.
        if state.starstruck_end > t and (state.starstruck_end - t) <= STARSTRUCK_GUARD_S:
            return STAR_PRISM
        # 0b. Fight tail: a banked Comet (940) outranks finishing the CMY chain
        # when both won't fit — never strand the black paint at the kill cutoff.
        if state.black_paint and remaining < COMET_TAIL_S:
            return COMET
        # 1. The CMY chain while Subtractive stacks are held (hasted in-burst).
        if state.subtractive > 0:
            return self._chain_spell(state, cmy=True)
        # 2. Spend the banked Comet (it blocks the next Subtractive's conversion).
        if state.black_paint:
            return COMET
        # 3. Star Prism inside Starstruck.
        if state.starstruck_end > t:
            return STAR_PRISM
        # 4. Hammer combo (guaranteed crit+DH — beats every filler).
        if state.hammer_stacks > 0 and state.hammer_end > t:
            return _HAMMER_BY_STACKS[state.hammer_stacks]
        # 5. Rainbow Bright Drip (instant 1000).
        if state.rainbow_bright and state.bright_end > t:
            return RAINBOW_DRIP
        # 6. Repaint an empty canvas.
        motif = self._motif_pick(state)
        if motif is not None:
            return motif
        # 7. Fight tail: dump paint (Holy 570 beats an unfinishable chain step).
        if remaining < TAIL_SPEND_S and state.white_paint >= 1:
            return HOLY
        # 8. The RGB chain (the default filler; feeds palette + paint).
        return self._chain_spell(state, cmy=False)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The beam's fork set: every currently-legal GCD. PCT has no forced
        chains — the genuine decisions are paint-now-vs-bank (Holy/Comet
        timing), hammer placement, Star Prism timing inside Starstruck, motif
        slot placement, and the CMY-chain start relative to the burst."""
        t = state.t
        out = [self.pick_gcd(state, params)]

        def add(aid: int) -> None:
            if aid not in out:
                out.append(aid)

        if state.subtractive > 0:
            add(self._chain_spell(state, cmy=True))
        else:
            add(self._chain_spell(state, cmy=False))
        if state.black_paint:
            add(COMET)
        if state.starstruck_end > t:
            add(STAR_PRISM)
        if state.hammer_stacks > 0 and state.hammer_end > t:
            add(_HAMMER_BY_STACKS[state.hammer_stacks])
        if state.rainbow_bright and state.bright_end > t:
            add(RAINBOW_DRIP)
        if state.white_paint >= 1:
            add(HOLY)
        motif = self._motif_pick(state)
        if motif is not None:
            add(motif)
        return out

    # --- oGCD selection -------------------------------------------------------------

    def _starry_burns_into_downtime(self, state: SimState) -> bool:
        """True when an imminent downtime window would eat the Starstruck burst
        (fire after the window instead — a player never bursts into a gap).
        No hold at the fight tail: a truncated burst still beats none."""
        t = state.t
        for s, e in state.downtime_windows:
            if t < s < t + STARRY_DOWNTIME_HOLD_S \
                    and e < state.fight_duration_s - STARRY_MIN_PAYOFF_S:
                return True
        return False

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        remaining = state.fight_duration_s - t
        fw = params.forbidden_windows
        # Starry Muse — the burst enabler, fired FIRST (the DRG Lance-Charge
        # lesson: in the strict scenario pick order alone sets self-window
        # alignment; 120s == the burst cadence, so ASAP == at-burst). Held when
        # a downtime window is imminent (the 20s Starstruck would burn into the
        # gap); fight-end escape: Star Prism alone pays from ~4s out.
        if (state.landscape_canvas and state.cd_ready.get(STARRY_MUSE, 0.0) <= t
                and remaining > STARRY_MIN_PAYOFF_S
                and not self._starry_burns_into_downtime(state)
                and not is_forbidden(STARRY_MUSE, t, fw)):
            return STARRY_MUSE
        # Subtractive Palette — the CMY enabler; must precede the burst GCDs.
        # Gated on the black slot being free (using it while a Comet is banked
        # wastes the Monochrome conversion).
        if (state.subtractive == 0 and not state.black_paint
                and (state.spectrum_free or state.palette >= pd.SUBTRACTIVE_PALETTE_COST)
                and not is_forbidden(SUBTRACTIVE, t, fw)):
            return SUBTRACTIVE
        # Striking Muse — arms the crit+DH hammers.
        if (state.weapon_canvas and state.charges.get(STRIKING_MUSE, 0.0) >= 1.0
                and state.hammer_stacks == 0 and remaining > 3.0
                and not is_forbidden(STRIKING_MUSE, t, fw)):
            return STRIKING_MUSE
        # Portraits (shared 30s recast keyed on MOG).
        if state.portrait == 1 and state.cd_ready.get(MOG, 0.0) <= t \
                and not is_forbidden(MOG, t, fw):
            return MOG
        if state.portrait == 2 and state.cd_ready.get(MOG, 0.0) <= t \
                and not is_forbidden(RETRIBUTION, t, fw):
            return RETRIBUTION
        # Living Muse (variant by creature stage) — ASAP; consumption is
        # canvas-gated to ~1/40s, so the 3-charge pool self-paces.
        if state.creature_canvas and state.charges.get(POM_MUSE, 0.0) >= 1.0:
            return pd.CREATURE_MUSES[state.creature_stage]
        return None

    # --- Cast transitions --------------------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental score (mirrors scoring.score_delivered_potency exactly,
        # raid buffs / tincture off): AoE-aware table potency, x2.26 on the
        # guaranteed-crit-DH hammers.
        base = potency_for(ability_id, self._n(t), pd.JOB_DATA)
        if base > 0:
            if ability_id in pd.ALWAYS_CRIT_DH_IDS:
                base *= pd.GUARANTEED_CRIT_DH_MULT
            state._score_flat += base

        # Hyperphantasia consumption (damaging spells only — hammers and motifs
        # leave the stacks intact, probe-verified). The 5th consumed stack
        # grants Rainbow Bright.
        if ability_id in pd.INSPIRED_IDS and state.hyperphantasia > 0:
            state.hyperphantasia -= 1
            if state.hyperphantasia == 0:
                state.rainbow_bright = True
                state.bright_end = t + pd.RAINBOW_BRIGHT_DURATION_S

        # Generic cooldown / charges (POM pool regen, Striking, Starry, Mog).
        apply_cooldown(state, self.cooldowns, ability_id)

        base_id = _AOE_TO_BASE.get(ability_id, ability_id)

        # Aetherhue chain + palette / paint.
        if base_id == FIRE:
            state.hue_step = 1
        elif base_id == AERO:
            state.hue_step = 2
        elif base_id == WATER:
            state.hue_step = 0
            state.palette = min(pd.PALETTE_CAP, state.palette + pd.PALETTE_PER_WATER)
            state.white_paint = min(pd.WHITE_PAINT_CAP, state.white_paint + 1)
        elif base_id == BLIZZARD:
            state.hue_step = 1
            state.subtractive -= 1
        elif base_id == STONE:
            state.hue_step = 2
            state.subtractive -= 1
        elif base_id == THUNDER:
            state.hue_step = 0
            state.subtractive -= 1
            state.white_paint = min(pd.WHITE_PAINT_CAP, state.white_paint + 1)
        elif ability_id == HOLY:
            state.white_paint = max(0, state.white_paint - 1)
        elif ability_id == COMET:
            state.black_paint = False
        elif ability_id == STAR_PRISM:
            state.starstruck_end = float("-inf")
        elif ability_id == RAINBOW_DRIP:
            state.rainbow_bright = False
            state.bright_end = float("-inf")
            state.white_paint = min(pd.WHITE_PAINT_CAP, state.white_paint + 1)
        elif ability_id in pd.ALWAYS_CRIT_DH_IDS:
            state.hammer_stacks = max(0, state.hammer_stacks - 1)

        # Motifs load the canvas.
        elif ability_id in pd.CREATURE_MOTIFS:
            state.creature_canvas = True
        elif ability_id == HAMMER_MOTIF:
            state.weapon_canvas = True
        elif ability_id == STARRY_SKY_MOTIF:
            state.landscape_canvas = True

        # Muses / portraits / the burst buttons.
        elif ability_id in pd.CREATURE_MUSES:
            state.creature_canvas = False
            if ability_id != POM_MUSE:      # POM spends via apply_cooldown above
                state.charges[POM_MUSE] = max(
                    0.0, state.charges.get(POM_MUSE, 0.0) - 1.0)
            if ability_id == pd.WINGED_MUSE:
                state.portrait = 1
            elif ability_id == pd.FANGED_MUSE:
                state.portrait = 2
            state.creature_stage = (state.creature_stage + 1) % 4
        elif ability_id == STRIKING_MUSE:
            state.weapon_canvas = False
            state.hammer_stacks = 3
            state.hammer_end = t + pd.HAMMER_TIME_DURATION_S
        elif ability_id == STARRY_MUSE:
            state.landscape_canvas = False
            state.hyperphantasia = pd.HYPERPHANTASIA_STACKS
            state.starstruck_end = t + pd.STARSTRUCK_DURATION_S
            state.spectrum_free = True
        elif ability_id == MOG:
            state.portrait = 0
        elif ability_id == RETRIBUTION:
            state.portrait = 0
            state.cd_ready[MOG] = t + pd.COOLDOWNS[MOG][0]   # shared portrait recast
        elif ability_id == SUBTRACTIVE:
            state.subtractive = pd.SUBTRACTIVE_STACKS
            state.hue_step = 0                               # chain restarts at cyan
            if state.spectrum_free:
                state.spectrum_free = False
            else:
                state.palette = max(0, state.palette - pd.SUBTRACTIVE_PALETTE_COST)
            if state.white_paint >= 1 and not state.black_paint:
                state.white_paint -= 1                       # Monochrome Tones
                state.black_paint = True

    # --- Downtime -------------------------------------------------------------------

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        """PCT's signature downtime move: re-paint empty canvases inside the
        window (motifs are self-targeted — no enemy needed; probe: M12S-P1's
        ~6.6s gap is filled with motif casts). In combat a motif is a 3s cast /
        4s slot; the last one ends exactly at `win_end` so the first uptime GCD
        is a damaging one. The aetherhue chain drops if the gap outlives its
        30s buff."""
        t = state.t
        if win_end - t > pd.AETHERHUES_DURATION_S:
            state.hue_step = 0
        if win_end > state.fight_duration_s - DOWNTIME_EPS_S:
            return
        slot = 4.0 * (self.gcd_base_s / PCT_GCD_S)
        needs: list[int] = []
        remaining_after = state.fight_duration_s - win_end
        if not state.creature_canvas and remaining_after > MOTIF_PAYOFF_MIN_S:
            needs.append(pd.CREATURE_MOTIFS[state.creature_stage])
        if not state.weapon_canvas and remaining_after > MOTIF_PAYOFF_MIN_S:
            needs.append(HAMMER_MOTIF)
        if not state.landscape_canvas and remaining_after > STARRY_MIN_PAYOFF_S:
            needs.append(STARRY_SKY_MOTIF)
        fit = int((win_end - t) // slot)
        needs = needs[:fit]
        for i, mid in enumerate(needs):
            state.timeline.append((win_end - slot * (len(needs) - i), mid))
            if mid in pd.CREATURE_MOTIFS:
                state.creature_canvas = True
            elif mid == HAMMER_MOTIF:
                state.weapon_canvas = True
            else:
                state.landscape_canvas = True

    # --- Beam search seam ---------------------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental crit-DH-aware running score
        plus admissible-ish credits for banked resources, so an investing line
        (banked palette / paint / charges / portrait) isn't pruned before it
        pays off. The final selection always re-scores with the exact score_fn."""
        t = state.t
        hammers = state.hammer_stacks if state.hammer_end > t else 0
        return (state._score_flat
                + state.palette * _PRUNE_PALETTE_P
                + state.subtractive * _PRUNE_SUBTRACTIVE_P
                + (_PRUNE_BLACK_P if state.black_paint else 0.0)
                + hammers * _PRUNE_HAMMER_P
                + state.charges.get(POM_MUSE, 0.0) * _PRUNE_MUSE_CHARGE_P
                + state.charges.get(STRIKING_MUSE, 0.0) * _PRUNE_STRIKING_P
                + _PRUNE_PORTRAIT_P[state.portrait]
                + (_PRUNE_STARSTRUCK_P if state.starstruck_end > t else 0.0)
                + (_PRUNE_BRIGHT_P if state.rainbow_bright and state.bright_end > t else 0.0)
                + state.hyperphantasia * _PRUNE_HYPER_P)

    def beam_signature(self, state: SimState):
        """Bucketed decision-state key (the GNB lesson: a lossless timer key
        fragments the beam into sub-GCD near-duplicates and starves the
        effective width). Gauges, chain position and flags are exact; window /
        cooldown timers are bucketed to ~one GCD. `state.t` is bucketed too —
        PCT beams do NOT advance in lockstep (a 2.5s RGB fork vs a 3.3s CMY
        fork vs a 4.0s motif fork diverge in time)."""
        t = state.t

        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)

        return (
            int(t / _SIG_BUCKET_S),
            state.hue_step, state.subtractive, state.palette // 25,
            state.white_paint, state.black_paint,
            state.creature_stage, state.creature_canvas, state.weapon_canvas,
            state.landscape_canvas, state.portrait,
            state.hammer_stacks if state.hammer_end > t else 0,
            state.hyperphantasia, state.rainbow_bright, state.spectrum_free,
            slot(state.starstruck_end - t),
            int(state.charges.get(POM_MUSE, 0.0) * 4),
            int(state.charges.get(STRIKING_MUSE, 0.0) * 4),
            slot(state.cd_ready.get(STARRY_MUSE, 0.0) - t),
            slot(state.cd_ready.get(MOG, 0.0) - t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding -----------------------------------------

def _model_for(sim_context) -> PictomancerRotationModel:
    """Build the model bound to this run's per-pull context: the per-player
    effective GCD (CeilingContext) and any `MultiTargetContext` (the AoE N(t)
    schedule). PCT is RNG-free, so there's no proc payload; the entry-gauge
    payload (M12S-P2 loaded continuations) is wired during live calibration.
    `None`/none -> the default model, byte-identical."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
    return PictomancerRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Engine-facing score_fn bound to a multi-target N(t) `schedule` (each cast
    valued per-target via `aoe_potency.potency_for`). Buff-aware when given.
    Empty schedule -> single target, byte-identical. Lazy import to avoid a
    scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score_fn(timeline, aux, buff_intervals):
        from jobs.pictomancer.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score_fn


# Module-level no-schedule scorer (tests / helpers call `_score`).
_score = _make_score()


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(sim_context)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, _make_score(model.mt_schedule), duration_key,
        list(downtime_tuple), buff_intervals, width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The PCT ceiling: the diverse beam over the paint / hammer / motif-timing
    forks on top of the burst-timing refinement (beam-only, like NIN/DRG — the
    exact DP seam is measured after beam calibration)."""
    tl, aux = _perfect_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        tuple((round(s, 3), round(e, 3), round(m, 4))
              for s, e, m in buff_intervals) if buff_intervals else None,
        sim_context)
    return list(tl), aux


# --- Module-level entrypoints (bind the model to the shared engine) ----------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once (greedy baseline). Returns (timeline, 0)
    — PCT has no pet/payload scalar, so aux is always 0."""
    if params is None:
        params = SimParams()
    model = _model_for(sim_context)
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
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
