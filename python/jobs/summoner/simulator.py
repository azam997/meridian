"""Idealized Summoner rotation — the SMN `RotationModel` for the shared engine.

The fourth **caster** simulator (after RDM / BLM / PCT), and the first
**pet-cycle** job. The time loop, downtime / weave / charge handling, parameter
sweep, local-search refinement and canonical buff alignment all live in
`jobs/_core/sim/engine.py`. This module supplies only the SMN-specific rotation:
the 60s demi cycle, the gem / attunement phases, the favors, and Aetherflow. The
four `simulate_*` shims at the bottom bind this model to the engine (names kept
so the sidecar / scorer / tests call them unchanged).

SMN-specific structure encoded:

- **The demi cycle** — Solar Bahamut / Bahamut / Solar Bahamut / Phoenix on ONE
  shared 60s recast (keyed on SUMMON_SOLAR_BAHAMUT in `cooldowns`; the other two
  spend it manually, the PCT portrait pattern). The game fixes the ORDER, so
  `demi_idx` cycles it. A summon opens a 15s trance: the impulse filler GCD
  (Umbral/Astral Impulse, Fountain of Fire), one Enkindle, one flare (Sunflare /
  Deathflare; Phoenix's slot is the Rekindle HEAL — never simmed), and the 4
  folded pet autos (priced at the summon cast — see data.py).
- **Mixed SpS-scaled recasts** (the Viper per-ability pattern): rites run 1.5s /
  2.5s / 3.0s slots, Slipstream 3.5s — all scaled by the per-player Spell Speed
  factor. Instant-vs-hardcast is captured in `gcd_duration` (never `gcd_slot`)
  so the greedy loop, the beam and any exact seam get identical timing. No haste
  window — SMN's fast slots are per-ability constants (`RECAST_MULT`).
- **Gem phases** — each demi grants the three arcanum; a primal summon attunes
  2/4/4 rites and arms the favors (Cyclone -> Strike, Slipstream, Mountain
  Buster per Topaz Rite). Primal ORDER is a real GCD fork (`gcd_candidates`).
  A demi summon wastes leftover gems / attunement / favors (the game replaces
  them) — top parses keep the 60s cadence regardless (probe: 60.2s gaps even
  through M12S-P1's downtime), and the beam explores the alternative.
- **The fold symmetry rule**: the demi summon is never HELD at the fight tail —
  a real player's full-credit tail summon must always be matchable by the
  ceiling (the folded autos are credited at cast on both sides). The only demi
  hold is the downtime guard (don't burn a 15s trance into a gap), with the
  mandatory fight-end escape.

Out of scope for v1 (documented, intentionally not modeled):
- Swiftcast as DPS — every swiftable SMN cast is recast-bound (Ruin III
  max(1.5, 2.5), Ruby Rite max(2.8, 3.0), Slipstream max(3.0, 3.5)), so an
  instant cast changes no slot length (probe: 8 of 10 Slipstreams are
  Swiftcast, cadence unchanged); it's movement utility.
- Lux Solaris / Rekindle / Radiant Aegis — heals/shields (defensive_ids).
- The ~0.80x pet damage-per-potency coefficient — symmetric nominal fold (see
  data.py); calibration lever only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from jobs._core.entry_gauge import EntryState, seed_entry_gauge
from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.summoner import data as sd


# --- Ability IDs (aliased from data for readability) -------------------------
RUIN_III    = sd.RUIN_III
RUIN_IV     = sd.RUIN_IV
SOLAR       = sd.SUMMON_SOLAR_BAHAMUT
BAHAMUT     = sd.SUMMON_BAHAMUT
PHOENIX     = sd.SUMMON_PHOENIX
IFRIT       = sd.SUMMON_IFRIT_II
TITAN       = sd.SUMMON_TITAN_II
GARUDA      = sd.SUMMON_GARUDA_II
RUBY_RITE   = sd.RUBY_RITE
TOPAZ_RITE  = sd.TOPAZ_RITE
EMERALD_RITE = sd.EMERALD_RITE
CYCLONE     = sd.CRIMSON_CYCLONE
STRIKE      = sd.CRIMSON_STRIKE
MOUNTAIN_BUSTER = sd.MOUNTAIN_BUSTER
SLIPSTREAM  = sd.SLIPSTREAM
ENERGY_DRAIN = sd.ENERGY_DRAIN
NECROTIZE   = sd.NECROTIZE
SEARING_LIGHT = sd.SEARING_LIGHT
SEARING_FLASH = sd.SEARING_FLASH

# AoE swaps (closed-form per-slot comparison in confirmed multi-target windows;
# the state machine folds the AoE id onto its base id's transitions).
_AOE_SWAP: dict[int, int] = {
    RUIN_III:     sd.TRI_DISASTER,
    RUBY_RITE:    sd.RUBY_CATASTROPHE,
    TOPAZ_RITE:   sd.TOPAZ_CATASTROPHE,
    EMERALD_RITE: sd.EMERALD_CATASTROPHE,
    sd.ASTRAL_IMPULSE:   sd.ASTRAL_FLARE,
    sd.UMBRAL_IMPULSE:   sd.UMBRAL_FLARE,
    sd.FOUNTAIN_OF_FIRE: sd.BRAND_OF_PURGATORY,
    NECROTIZE:    sd.PAINFLARE,
    ENERGY_DRAIN: sd.ENERGY_SIPHON,
}
_AOE_TO_BASE = {v: k for k, v in _AOE_SWAP.items()}

# --- Rotation tuning ----------------------------------------------------------
SMN_GCD_S = 2.50            # base GCD (per-player Spell Speed threads via gcd_base_s)
# The measured pre-pull line: Ruin III hardcast during the countdown, its
# snapshot landing at t~0.3; the recast (begun at ~-1.2s) frees the GCD loop at
# ~+1.3s — probe: Summon Solar Bahamut lands at 1.7-2.0s on every cold open.
PREPULL_CHANNEL_T = -1.2
PREPULL_RESIDUAL_S = 1.3
# Hold a ready demi when a downtime window would eat a MEANINGFUL chunk of the
# 15s trance; the mandatory fight-end escape (never hold at the tail — the fold
# symmetry rule). The overlap gate matters: M9S has a 0.22s targetability blip
# and holding the whole window for it costs ~7s of demi-cycle alignment (the
# T-elos finding) — a real player summons straight through a blip.
DEMI_DOWNTIME_HOLD_S = 15.0
DEMI_HOLD_MIN_OVERLAP_S = 4.0
DEMI_TAIL_ESCAPE_S = 20.0
ATTUNEMENT_DURATION_S = 30.0

_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
_SIG_BUCKET_S = SMN_GCD_S   # decision-state timer bucket (~one GCD)
# state.t is bucketed FINER than the other timers: the demi summon waits for a
# GCD slot boundary after its 60.0s cooldown, and top parses phase their short
# Emerald slots so a boundary lands ON the cooldown (drift ~0.0s/cycle vs the
# coarse-bucket beam's +0.7s/cycle). Lines that differ only in that sub-slot
# phase are EXACTLY the ones a ~one-GCD t bucket dedups away — 0.5s keeps them
# distinct (the T-elos finding: fine-t beam snaps every demi gap to 60.01s).
_SIG_T_BUCKET_S = 0.5

# A 1.5s Emerald slot can't double-weave (the NIN weave-cap pattern).
_WEAVE_CAP: dict[int, int] = {
    EMERALD_RITE: 1,
    sd.EMERALD_CATASTROPHE: 1,
}

# Hold the burst enablers into raid-buff windows (refine / canonical alignment).
# The Enkindles / flares are demi-window-gated — their timing rides the summon
# GCD, which the beam forks — so only the free oGCDs anchor.
_ANCHORS: tuple[int, ...] = (SEARING_LIGHT, ENERGY_DRAIN)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)

# Prune-credit rates for banked resources (admissible-ish steering only — the
# final beam selection always re-scores with the exact score_fn). Net over the
# Ruin III filler (160 p/s) each displaces.
_PRUNE_AETHERFLOW_P   = 500.0    # a Necrotize (free oGCD potency)
_PRUNE_FURTHER_RUIN_P = 120.0    # Ruin IV 520 over the Ruin III it displaces
_PRUNE_FLASH_P        = 700.0    # a pending Searing Flash (free oGCD)
_PRUNE_GEM_P = {IFRIT: 1000.0, TITAN: 800.0, GARUDA: 600.0}   # phase net-over-filler
_PRUNE_ATTUNE_P       = 150.0    # per banked rite (net over filler, averaged)
_PRUNE_CYCLONE_P      = 320.0    # Cyclone + the Strike it arms (2 x 560 - 2 x 400)
_PRUNE_STRIKE_P       = 160.0
_PRUNE_SLIPSTREAM_P   = 190.0
_PRUNE_TITANS_FAVOR_P = 160.0    # a Mountain Buster (free oGCD)
_PRUNE_ENKINDLE_P     = 1400.0   # pending Enkindle payoff (window average)
_PRUNE_FLARE_P        = 750.0    # pending Sunflare/Deathflare (window average)
# Remaining-time credit rate (p/s) — the TIME-FAIRNESS term. The engine's beam
# advances in lockstep by CAST INDEX, so at equal step a line that took fast
# 1.5s Emerald slots sits behind in wall-clock with less accumulated potency
# than a 2.5s Ruin III line, and raw-score ranking systematically starves the
# fast-slot forks (the T-elos M9S finding: the width-256 winner cast ZERO
# Emerald Rites, backfilling with per-second-worse Ruin III). Crediting each
# line's remaining fight time at ~the rotation's sustained rate makes the
# ranking potency-per-SECOND-fair; the final selection still re-scores exactly.
_PRUNE_TIME_RATE_P    = 300.0


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """SMN picker tunables — only the shared knobs (max_weaves / forbidden_windows)."""
    pass


@dataclass
class SimState(SimStateBase):
    # Demi cycle.
    demi_idx: int = 0             # position in DEMI_CYCLE (mod 4)
    active_demi: int = 0          # summon id of the live trance (0 = none)
    demi_end: float = float("-inf")
    enkindle_ready: bool = False
    flare_ready: bool = False
    # Gem / attunement phases.
    ruby_gem: bool = False
    topaz_gem: bool = False
    emerald_gem: bool = False
    attunement: int = 0           # rites remaining
    attunement_rite: int = 0      # the rite id (0 = none)
    attunement_end: float = float("-inf")
    # Favors.
    cyclone_ready: bool = False
    strike_ready: bool = False
    slipstream_ready: bool = False
    titans_favor: bool = False
    # Aetherflow.
    aetherflow: int = 0           # GaugeModel.name == field convention
    further_ruin: bool = False
    searing_flash_ready: bool = False
    # Captured in gcd_duration for weave_budget (the BLM pattern).
    instant_this_slot: bool = False
    # Incremental (raid-buff-agnostic) running score for the O(1) beam-prune
    # key — the exact per-cast math `score_delivered_potency` runs with raid
    # buffs / tincture off (SMN has no guaranteed-crit family).
    _score_flat: float = 0.0


class SummonerRotationModel(engine.BaseRotationModel):
    cooldowns = sd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=SMN_GCD_S, cast_times=sd.CAST_TIMES)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = (SEARING_LIGHT,)
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = (),
                 entry: EntryState | None = None):
        # Multi-target N(t) schedule (the AoE-aware ceiling): where the target
        # count makes the AoE swap the higher potency, the picker swaps per slot
        # (closed-form comparison). Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Phase-continuation entry state (M12S-P2 probes show COLD opens, so
        # this is normally None — wired for completeness; measure returns 0).
        self.entry = entry
        # Per-player Spell Speed: scales the base recast, every per-ability
        # recast multiple AND the cast times by the same factor. None keeps the
        # tier constant, byte-identical.
        self.gcd_base_s = SMN_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != SMN_GCD_S:
            factor = self.gcd_base_s / SMN_GCD_S
            self.timing = replace(
                SummonerRotationModel.timing, gcd_recast_s=self.gcd_base_s,
                cast_times={k: v * factor for k, v in sd.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    def _swap(self, base_id: int, t: float) -> int:
        """The AoE variant when the live target count makes it the higher
        potency; the base id otherwise."""
        n = self._n(t)
        if n >= 2:
            alt = _AOE_SWAP.get(base_id)
            if alt is not None and \
                    potency_for(alt, n, sd.JOB_DATA) > potency_for(base_id, n, sd.JOB_DATA):
                return alt
        return base_id

    # --- Lifecycle ------------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {SOLAR: 0.0, ENERGY_DRAIN: 0.0, SEARING_LIGHT: 0.0}
        if self.entry:
            seed_entry_gauge(state, self.entry.gauge_map, sd.JOB_DATA.gauges)
        return state

    def prepull(self, state: SimState, params) -> None:
        # The measured pre-pull line: a Ruin III hardcast during the countdown,
        # snapshot at t~0.3 (begincast ~-1.2); its 2.5s recast frees the GCD
        # loop at ~1.3s, whose weave space carries the opener pot (the engine
        # weaves it from the first slots).
        state.timeline.append((PREPULL_CHANNEL_T, RUIN_III))
        state._score_flat += potency_for(RUIN_III, self._n(0.0), sd.JOB_DATA)
        state.last_gcd_t = 0.0
        state.t = PREPULL_RESIDUAL_S * (self.gcd_base_s / SMN_GCD_S)

    # --- GCD timing -------------------------------------------------------------

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Per-ability cast + recast, both scaled by the per-player speed factor
        # (already folded into self.timing). No haste window on SMN.
        cast = self.timing._cast_time(gcd_id)
        recast = self.gcd_base_s * sd.RECAST_MULT.get(gcd_id, 1.0)
        state.instant_this_slot = cast <= 0.0
        return max(cast, recast)

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        base = (self.timing.instant_weaves if state.instant_this_slot
                else self.timing.hardcast_weaves)
        base = min(base, _WEAVE_CAP.get(gcd_id, base))
        return min(base, params.max_weaves_per_gcd)

    # --- GCD selection ------------------------------------------------------------

    def _next_demi(self, state: SimState) -> int:
        return sd.DEMI_CYCLE[state.demi_idx % len(sd.DEMI_CYCLE)]

    def _demi_ready(self, state: SimState) -> bool:
        return (state.cd_ready.get(SOLAR, 0.0) <= state.t
                and state.demi_end <= state.t)

    def _demi_burns_into_downtime(self, state: SimState) -> bool:
        """True when an imminent downtime window would eat a MEANINGFUL chunk
        of the 15s trance (fire after the gap instead — a player never bursts
        into a real gap, but summons straight through a sub-slot targetability
        blip). NEVER holds at the fight tail: the folded autos + Enkindle are
        credited at cast, so a truncated window still pays (the fold symmetry
        rule)."""
        t = state.t
        if state.fight_duration_s - t < DEMI_TAIL_ESCAPE_S:
            return False
        for s, e in state.downtime_windows:
            if not (t < s < t + DEMI_DOWNTIME_HOLD_S):
                continue
            overlap = min(e, t + sd.DEMI_WINDOW_S) - s
            if overlap > DEMI_HOLD_MIN_OVERLAP_S \
                    and e < state.fight_duration_s - DEMI_TAIL_ESCAPE_S:
                return True
        return False

    def _gems_held(self, state: SimState) -> list[int]:
        """Held primal gems in the greedy consensus order (Garuda first — the
        measured top-parse default; the beam forks the other orders)."""
        out: list[int] = []
        if state.emerald_gem:
            out.append(GARUDA)
        if state.ruby_gem:
            out.append(IFRIT)
        if state.topaz_gem:
            out.append(TITAN)
        return out

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        in_window = state.demi_end > t and state.active_demi != 0
        # 1. The demi impulse — the window's filler (640/580/500 beats
        # everything else slot-for-slot; probe: 6 impulses every window).
        if in_window:
            return self._swap(sd.DEMI_IMPULSE[state.active_demi], t)
        # 2. The demi summon on its 60s dictate (the cadence probe: 60.2s gaps
        # everywhere). Held only for the downtime guard.
        if self._demi_ready(state) and not self._demi_burns_into_downtime(state):
            return self._next_demi(state)
        # 3. Attunement rites.
        if state.attunement > 0 and state.attunement_end > t:
            return self._swap(state.attunement_rite, t)
        # 4. Favors (instant melee dashes / the hardcast Slipstream).
        if state.cyclone_ready:
            return CYCLONE
        if state.strike_ready:
            return STRIKE
        if state.slipstream_ready:
            return SLIPSTREAM
        # 5. The next primal phase.
        gems = self._gems_held(state)
        if gems:
            return gems[0]
        # 6. Ruin IV (instant 520; granted by Energy Drain).
        if state.further_ruin:
            return RUIN_IV
        # 7. Ruin III (the filler).
        return self._swap(RUIN_III, t)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The beam's fork set: every currently-legal GCD. The genuine decisions
        are the primal ORDER within the minute (buff alignment), Ruin IV
        placement, demi timing at downtime edges, and favor placement."""
        t = state.t
        out = [self.pick_gcd(state, params)]

        def add(aid: int) -> None:
            if aid not in out:
                out.append(aid)

        if state.demi_end > t and state.active_demi != 0:
            add(self._swap(sd.DEMI_IMPULSE[state.active_demi], t))
        if self._demi_ready(state):
            add(self._next_demi(state))
        if state.attunement > 0 and state.attunement_end > t:
            add(self._swap(state.attunement_rite, t))
        if state.cyclone_ready:
            add(CYCLONE)
        if state.strike_ready:
            add(STRIKE)
        if state.slipstream_ready:
            add(SLIPSTREAM)
        if state.demi_end <= t:
            # A primal cannot be summoned while a demi trance is live (the pet
            # slot is occupied — probe: primal summons only ever follow the
            # window). Without this gate the beam found an ILLEGAL tail line
            # (Ifrit + Garuda summoned inside the last Solar window).
            for gem in self._gems_held(state):
                add(gem)
        if state.further_ruin:
            add(RUIN_IV)
        add(self._swap(RUIN_III, t))
        return out

    # --- oGCD selection -------------------------------------------------------------

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        remaining = state.fight_duration_s - t
        fw = params.forbidden_windows
        # Searing Light — the party-buff enabler, fired FIRST (the DRG
        # Lance-Charge lesson: in the strict scenario pick order alone sets
        # alignment; 120s == the burst cadence, so ASAP == at-burst).
        if (state.cd_ready.get(SEARING_LIGHT, 0.0) <= t and remaining > 2.0
                and not is_forbidden(SEARING_LIGHT, t, fw)):
            return SEARING_LIGHT
        # The demi-window pair (state-gated, not cooldown-gated).
        if state.enkindle_ready and state.demi_end > t and state.active_demi != 0:
            return sd.DEMI_ENKINDLE[state.active_demi]
        if state.flare_ready and state.demi_end > t \
                and state.active_demi in sd.DEMI_FLARE:
            return sd.DEMI_FLARE[state.active_demi]
        # Searing Flash (armed by Searing Light; already full-potency-to-all).
        if state.searing_flash_ready and not is_forbidden(SEARING_FLASH, t, fw):
            return SEARING_FLASH
        # Aetherflow: spend, then refill on cooldown (never overcap).
        if state.aetherflow > 0:
            return self._swap(NECROTIZE, t)
        if (state.cd_ready.get(ENERGY_DRAIN, 0.0) <= t and state.aetherflow == 0
                and not is_forbidden(ENERGY_DRAIN, t, fw)):
            return self._swap(ENERGY_DRAIN, t)
        # Mountain Buster (granted per Topaz Rite; spent before the next grant).
        if state.titans_favor:
            return MOUNTAIN_BUSTER
        return None

    # --- Cast transitions --------------------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental score (mirrors scoring.score_delivered_potency exactly,
        # raid buffs / tincture off): AoE-aware table potency. The pet folds
        # make this a pure table lookup — no state-derived bonuses.
        base = potency_for(ability_id, self._n(t), sd.JOB_DATA)
        if base > 0:
            state._score_flat += base

        # Generic cooldown (Solar demi pool / Energy Drain / Searing Light).
        apply_cooldown(state, self.cooldowns, ability_id)

        base_id = _AOE_TO_BASE.get(ability_id, ability_id)

        # Demi summons: open the trance, grant the three gems, advance the
        # cycle; leftover gems / attunement / favors are REPLACED (wasted).
        if base_id in sd.DEMI_SUMMON_IDS:
            state.active_demi = base_id
            state.demi_end = t + sd.DEMI_WINDOW_S
            state.enkindle_ready = True
            state.flare_ready = base_id in sd.DEMI_FLARE
            state.ruby_gem = state.topaz_gem = state.emerald_gem = True
            state.attunement = 0
            state.attunement_rite = 0
            state.cyclone_ready = state.strike_ready = False
            state.slipstream_ready = False
            state.titans_favor = False
            state.demi_idx += 1
            if base_id != SOLAR:      # Solar spends via apply_cooldown above
                state.cd_ready[SOLAR] = t + sd.COOLDOWNS[SOLAR][0]

        # Primal summons: clear the gem, attune, arm the favor.
        elif base_id in sd.PRIMAL_SUMMON_IDS:
            rite, count = sd.PRIMAL_RITES[base_id]
            state.attunement = count
            state.attunement_rite = rite
            state.attunement_end = t + ATTUNEMENT_DURATION_S
            if base_id == IFRIT:
                state.ruby_gem = False
                state.cyclone_ready = True
            elif base_id == TITAN:
                state.topaz_gem = False
            else:
                state.emerald_gem = False
                state.slipstream_ready = True

        # Rites.
        elif base_id in sd.RITE_IDS:
            state.attunement = max(0, state.attunement - 1)
            if base_id == TOPAZ_RITE:
                state.titans_favor = True

        # Favors.
        elif ability_id == CYCLONE:
            state.cyclone_ready = False
            state.strike_ready = True
        elif ability_id == STRIKE:
            state.strike_ready = False
        elif ability_id == SLIPSTREAM:
            state.slipstream_ready = False
        elif ability_id == MOUNTAIN_BUSTER:
            state.titans_favor = False

        # Aetherflow.
        elif base_id == ENERGY_DRAIN:
            state.aetherflow = sd.AETHERFLOW_CAP
            state.further_ruin = True
            if ability_id != ENERGY_DRAIN:    # Siphon spends the shared recast
                state.cd_ready[ENERGY_DRAIN] = t + sd.COOLDOWNS[ENERGY_DRAIN][0]
        elif base_id == NECROTIZE:
            state.aetherflow = max(0, state.aetherflow - 1)
        elif ability_id == RUIN_IV:
            state.further_ruin = False

        # Searing.
        elif ability_id == SEARING_LIGHT:
            state.searing_flash_ready = True
        elif ability_id == SEARING_FLASH:
            state.searing_flash_ready = False

        # The demi-window pair.
        elif ability_id in sd.DEMI_ENKINDLE.values():
            state.enkindle_ready = False
        elif ability_id in sd.DEMI_FLARE.values():
            state.flare_ready = False

    # --- Beam search seam ---------------------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental running score plus
        admissible-ish credits for banked resources, so an investing line
        (held gems / pending window payoffs / banked aetherflow) isn't pruned
        before it pays off. The final selection always re-scores with the
        exact score_fn."""
        t = state.t
        in_window = state.demi_end > t and state.active_demi != 0
        credit = (state._score_flat
                  # Time fairness: the beam steps in lockstep by cast index, so
                  # lines diverge in wall-clock; credit the remaining fight
                  # time at the sustained rate or fast-slot (Emerald) forks are
                  # systematically starved at the width cutoff.
                  + max(0.0, state.fight_duration_s - t) * _PRUNE_TIME_RATE_P
                  + state.aetherflow * _PRUNE_AETHERFLOW_P
                  + (_PRUNE_FURTHER_RUIN_P if state.further_ruin else 0.0)
                  + (_PRUNE_FLASH_P if state.searing_flash_ready else 0.0)
                  + (_PRUNE_GEM_P[IFRIT] if state.ruby_gem else 0.0)
                  + (_PRUNE_GEM_P[TITAN] if state.topaz_gem else 0.0)
                  + (_PRUNE_GEM_P[GARUDA] if state.emerald_gem else 0.0)
                  + (state.attunement * _PRUNE_ATTUNE_P
                     if state.attunement_end > t else 0.0)
                  + (_PRUNE_CYCLONE_P if state.cyclone_ready else 0.0)
                  + (_PRUNE_STRIKE_P if state.strike_ready else 0.0)
                  + (_PRUNE_SLIPSTREAM_P if state.slipstream_ready else 0.0)
                  + (_PRUNE_TITANS_FAVOR_P if state.titans_favor else 0.0))
        if in_window:
            if state.enkindle_ready:
                credit += _PRUNE_ENKINDLE_P
            if state.flare_ready:
                credit += _PRUNE_FLARE_P
        return credit

    def beam_signature(self, state: SimState):
        """Bucketed decision-state key (the GNB lesson: a lossless timer key
        fragments the beam and starves the effective width). Gauges, gems and
        flags are exact; window / cooldown timers are bucketed to ~one GCD.
        `state.t` is bucketed too — SMN beams do NOT advance in lockstep (a
        1.5s Emerald fork vs a 3.0s Ruby fork vs a 2.5s summon fork diverge in
        time, so time-diverged lines must not collapse onto one key) — and
        FINER than the other timers (_SIG_T_BUCKET_S): sub-slot phase
        differences are how the beam holds the drift-free demi-alignment lines
        alive (top parses phase their Emerald slots so a GCD boundary lands ON
        the 60.0s demi cooldown)."""
        t = state.t

        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)

        return (
            int(t / _SIG_T_BUCKET_S),
            state.demi_idx % len(sd.DEMI_CYCLE),
            state.active_demi if state.demi_end > t else 0,
            slot(state.demi_end - t),
            state.enkindle_ready, state.flare_ready,
            state.ruby_gem, state.topaz_gem, state.emerald_gem,
            state.attunement_rite if state.attunement_end > t else 0,
            state.attunement if state.attunement_end > t else 0,
            state.cyclone_ready, state.strike_ready,
            state.slipstream_ready, state.titans_favor,
            state.aetherflow, state.further_ruin, state.searing_flash_ready,
            slot(state.cd_ready.get(SOLAR, 0.0) - t),
            slot(state.cd_ready.get(ENERGY_DRAIN, 0.0) - t),
            slot(state.cd_ready.get(SEARING_LIGHT, 0.0) - t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding -----------------------------------------

def _model_for(sim_context) -> SummonerRotationModel:
    """Build the model bound to this run's per-pull context: the per-player
    effective GCD (CeilingContext), any `MultiTargetContext` (the AoE N(t)
    schedule), and any phase-continuation `EntryState` (M12S-P2 probes open
    COLD, so entry is normally None). `None`/none -> the default model,
    byte-identical."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    return SummonerRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule,
                                 entry=entry)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Engine-facing score_fn bound to a multi-target N(t) `schedule` (each cast
    valued per-target via `aoe_potency.potency_for`). Buff-aware when given.
    Empty schedule -> single target, byte-identical. Lazy import to avoid a
    scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score_fn(timeline, aux, buff_intervals):
        from jobs.summoner.scoring import score_delivered_potency
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
    """The SMN ceiling: the diverse beam over the primal-order / Ruin IV /
    demi-timing forks on top of the burst-timing refinement (beam-only, like
    NIN/DRG/PCT — the exact DP seam is measured after beam calibration)."""
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
    — the pet folds ride the player cast ids, so aux is always 0."""
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
