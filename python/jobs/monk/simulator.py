"""Idealized Monk rotation — the MNK `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the MNK-specific rotation: the form cycle with its
three Fury economies, the Perfect Balance -> Masterful Blitz machine (with the
lunar-vs-solar GCD fork), the Riddle of Fire self-window, the Rumination replies,
the budgeted chakra economy, and the downtime Meditation / Form Shift priming.
The four `simulate_*` shims at the bottom bind this model to the engine (names
kept so the sidecar / scorer / tests call them unchanged).

MNK-specific structure encoded:

- **The form cycle** (opo-opo -> raptor -> coeurl -> ...): within each form the
  pick is Fury-driven — generator when empty (Dragon Kick / Twin Snakes /
  Demolish), spender when stacked (Leaping Opo / Rising Raptor / Pouncing
  Coeurl, +200/+200/+150). Leaping Opo is a guaranteed crit when opo-eligible
  (x1.62, the SAM Setsugekka pattern). The spend-vs-bank choice near window
  edges is exposed as a beam fork.
- **Perfect Balance -> Blitz** (the job's strategic GCD fork): PB (40s x2)
  grants 3 form-free GCDs, each banking a Beast Chakra of its action's form.
  The FIRST PB GCD commits the goal — 3-same-opo (-> Elixir Burst, Lunar Nadi)
  vs 3-distinct (-> Rising Phoenix, Solar) — and the beam forks it (the NIN
  mudra-goal pattern: `_blitz_goal` is params-free so `pick_gcd` and
  `apply_cast` derive the same goal; a beam-forked start is committed by the
  first cast's form family). Both Nadi lit -> the next blitz is Phantom Rush
  (1500) regardless of composition, so the goal rule banks toward it. The
  greedy goal rule alone reproduces the live blitz mix exactly (EB/RP/PR
  17-blitz counts on 631s M11S parses).
- **Riddle of Fire** (+15%, measured 20.7s / 60s): a windowed self-buff folded
  into the INCREMENTAL beam score (the GNB No-Mercy pattern) and fired FIRST in
  `pick_ogcd` (the DRG Lance-Charge lesson: in the strict scenario pick order
  alone sets self-buff alignment). PB is gated so its blitz lands inside the
  window; the replies (Wind's 1040 / Fire's 1400) are held into it with a
  before-expiry escape.
- **Chakra is a measured BUDGET** (the DNC pattern): generation is crit-RNG +
  party-fed (invisible to the cast stream), so the ceiling spends the player's
  own The Forbidden Chakra count (`MonkCtx.tfc_budget` via sim_context) —
  linearly paced, dumped into Riddle of Fire windows up to the physical 2-TFC
  bank (chakra caps at 10 under Brotherhood).
- **Downtime**: Forbidden Meditation (1s GCD, no target needed) pumps chakra
  through the window — its yield is already inside the measured budget, so the
  casts are emitted for timeline realism only — and Form Shift re-arms Formless
  Fist at the window edge so re-engage opens with a full-value opo GCD (the NIN
  edge-priming pattern).

Out of scope for v1 (documented, intentionally not modeled):
- The dedicated AoE line (Rockbreaker / Four-point Fury / Shadow of the
  Destroyer / Enlightenment) — pure single-target ceiling; multi-target windows
  beyond the free-splash blitz/reply cleave stay disclaimed (DRG/GNB/NIN
  precedent).
- Six-Sided Star's +80/chakra (flat 780 both sides — symmetric) and its
  disconnect usage (a full-uptime ceiling never disconnects; the player's SSS
  casts are credited on delivered).
- Positional hit/miss (idealized always hits — the RPR convention); the exact
  DP seam (beam-only, like DRG/GNB/NIN).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from jobs._core.sim import engine
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.monk import data as md


# --- Ability IDs (aliased from data for readability) ------------------------
DRAGON_KICK     = md.DRAGON_KICK
LEAPING_OPO     = md.LEAPING_OPO
TWIN_SNAKES     = md.TWIN_SNAKES
RISING_RAPTOR   = md.RISING_RAPTOR
DEMOLISH        = md.DEMOLISH
POUNCING_COEURL = md.POUNCING_COEURL
SIX_SIDED_STAR  = md.SIX_SIDED_STAR
WINDS_REPLY     = md.WINDS_REPLY
FIRES_REPLY     = md.FIRES_REPLY
FORM_SHIFT      = md.FORM_SHIFT
MEDITATION      = md.FORBIDDEN_MEDITATION
ELIXIR_BURST    = md.ELIXIR_BURST
RISING_PHOENIX  = md.RISING_PHOENIX
CELESTIAL_REVOLUTION = md.CELESTIAL_REVOLUTION
PHANTOM_RUSH    = md.PHANTOM_RUSH
TFC             = md.THE_FORBIDDEN_CHAKRA
PERFECT_BALANCE = md.PERFECT_BALANCE
RIDDLE_OF_FIRE  = md.RIDDLE_OF_FIRE
RIDDLE_OF_WIND  = md.RIDDLE_OF_WIND
BROTHERHOOD     = md.BROTHERHOOD


# --- Rotation tuning ---------------------------------------------------------
# The Greased-Lightning weaponskill GCD (2.5 x 0.80) — the wiki/job-guide display
# value at zero Skill Speed. Live top parses run ~1.93-1.95 (light SkS melds);
# the per-player inference threads that in via `gcd_base_s` (only ever faster ->
# monotone-safe, anchored by the constant band in subgcd_gcd_sweep).
MNK_GCD_S = 2.00

MEDITATION_GCD_S = 1.0     # Forbidden Meditation's fixed GCD-linked recast
SSS_RECAST_MULT = 2.0      # Six-Sided Star runs its own 4s (2x) slot

# Forms (state.form values).
_NO_FORM, _OPO, _RAPTOR, _COEURL = 0, 1, 2, 3
# Blitz goals (state.pb_goal values).
_GOAL_NONE, _GOAL_LUNAR, _GOAL_SOLAR = 0, 1, 2
# Beast-Chakra type bits (state.pb_types mask).
_BIT = {_OPO: 1, _RAPTOR: 2, _COEURL: 4}

_FORM_OF = {**{a: _OPO for a in md.OPO_GCD_IDS},
            **{a: _RAPTOR for a in md.RAPTOR_GCD_IDS},
            **{a: _COEURL for a in md.COEURL_GCD_IDS}}
_NEXT_FORM = {_OPO: _RAPTOR, _RAPTOR: _COEURL, _COEURL: _OPO}

# Picker thresholds.
PB_BLITZ_FIT_S       = 8.5    # PB only when the blitz (4 GCDs later) lands in-RoF
PB_ROF_SOON_S        = 4.0    # pre-window PB (the observed 61.95s pattern)
PB_ENDGAME_S         = 30.0   # fight-end escape: an unbuffed blitz beats no blitz
REPLY_EXPIRY_GCDS    = 3.0    # cast a pending reply this close to rumination expiry
TFC_BANK             = 2      # chakra bank entering a window (10 under Brotherhood)
TFC_ENDGAME_S        = 10.0   # dump the remaining budget at the end
SSS_LAST_SLOT_S      = 2.2    # remaining time where SSS (780) wins the final GCD
DOWNTIME_PRIME_MIN_S = 2.0    # min window length to re-arm Formless at its edge
DOWNTIME_MEDITATIONS = 5      # chakra pump emitted for timeline realism

# Hold the burst enablers into raid-buff windows (refine / canonical alignment).
_ANCHORS: tuple[int, ...] = (RIDDLE_OF_FIRE, BROTHERHOOD, PERFECT_BALANCE,
                             RIDDLE_OF_WIND)
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
_SIG_BUCKET_S = MNK_GCD_S   # decision-state timer bucket (~one GCD)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    md.JOB_DATA.tincture_main_stat, md.JOB_DATA.tincture_role_coeff)

# Fallback chakra budget when no sim_context is supplied (~ the live steady-state
# rate: 54 TFC / 631s and 37 / 450s both measure ~1 per 12s).
_DEFAULT_TFC_RATE_S = 12.0

# Prune-credit rates for banked resources (admissible-ish steering only — the
# final beam selection always re-scores with the exact score_fn).
_PRUNE_OPO_FURY_P    = md.OPO_FURY_BONUS_P * md.GUARANTEED_CRIT_MULT
_PRUNE_RAPTOR_FURY_P = float(md.RAPTOR_FURY_BONUS_P)
_PRUNE_COEURL_FURY_P = float(md.COEURL_FURY_BONUS_P)
_PRUNE_BEAST_P       = 300.0   # a banked Beast Chakra -> 1/3 of a 900 blitz
_PRUNE_BOTH_NADI_P   = 600.0   # a lit Lunar+Solar pair -> the Phantom Rush premium
_PRUNE_TFC_P         = float(md.POTENCIES[TFC])


@dataclass(frozen=True)
class MonkCtx:
    """Per-pull measured context (hashable — joins the perfect-sim cache key).
    `tfc_budget` is the player's The Forbidden Chakra cast count: chakra
    generation is crit-RNG + party-fed (invisible to the cast stream), so the
    ceiling spends the SAME chakra the player actually got and below-average
    luck never costs efficiency (the DNC budget pattern)."""
    tfc_budget: int = 0

    def __bool__(self) -> bool:
        return self.tfc_budget > 0


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """MNK picker tunables — only the shared knobs (max_weaves / forbidden_windows)."""
    pass


@dataclass
class SimState(SimStateBase):
    # Form machine.
    form: int = _NO_FORM
    form_end: float = float("-inf")
    formless_end: float = float("-inf")
    # Fury stacks (GaugeModel.name == field convention).
    opo_fury: int = 0
    raptor_fury: int = 0
    coeurl_fury: int = 0
    # Perfect Balance -> Blitz machine.
    pb_left: int = 0             # form-free GCDs remaining
    pb_goal: int = _GOAL_NONE    # committed by the first PB GCD (beam fork)
    beast_n: int = 0             # Beast Chakra held (blitz at 3)
    pb_types: int = 0            # Beast Chakra form bitmask
    lunar: bool = False
    solar: bool = False
    # Self-buff / rumination windows (absolute end-times).
    rof_end: float = float("-inf")
    fire_rum_end: float = float("-inf")
    wind_rum_end: float = float("-inf")
    # Chakra budget.
    tfc_total: int = 0
    tfc_left: int = 0
    # Incremental (raid-buff-agnostic, self-buff-AWARE) running score for the O(1)
    # beam-prune key — the exact per-cast math `score_delivered_potency` runs with
    # raid buffs / tincture off.
    _score_flat: float = 0.0


class MonkRotationModel(engine.BaseRotationModel):
    cooldowns = md.COOLDOWNS
    timing = InstantGCD(base_s=MNK_GCD_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 ctx: MonkCtx | None = None,
                 duration_hint_s: float = 600.0):
        # Per-player Skill Speed scales every weaponskill (MNK is a FLAT-GCD job —
        # no haste self-window); None keeps the constant, byte-identical.
        self.gcd_base_s = MNK_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != MNK_GCD_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)
        self.ctx = ctx if ctx is not None else _default_ctx(duration_hint_s)

    # --- Lifecycle -----------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {PERFECT_BALANCE: 2.0}
        state.cd_ready = {RIDDLE_OF_FIRE: 0.0, RIDDLE_OF_WIND: 0.0,
                          BROTHERHOOD: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # The measured opener: Form Shift + Meditation x5 during the countdown
        # (the pot lands in the first weave via the engine's `_maybe_pot`), then
        # the first Dragon Kick after the melee run-in. Only the Form Shift is
        # emitted in the pre-zone (the delivered side reconstructs the player's
        # via `prepull_buff_ids`); the pre-pull chakra is already inside the
        # measured TFC budget.
        state.tfc_total = state.tfc_left = self.ctx.tfc_budget
        state.timeline.append((-1.5, FORM_SHIFT))
        state.formless_end = md.FORMLESS_DURATION_S
        state.t = md.JOB_DATA.role_policy.engage_delay_s

    # --- GCD timing ----------------------------------------------------------

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        if gcd_id == SIX_SIDED_STAR:
            return SSS_RECAST_MULT * self.gcd_base_s
        if gcd_id == MEDITATION:
            return MEDITATION_GCD_S
        return self.gcd_base_s

    # --- Form / Fury helpers ---------------------------------------------------

    def _opo_eligible(self, state: SimState) -> bool:
        t = state.t
        return (state.pb_left > 0 or state.formless_end > t
                or (state.form == _OPO and state.form_end > t))

    def _form_pick(self, state: SimState, form: int) -> int:
        """The Fury-greedy action for `form`: spender when stacked, generator
        when empty."""
        if form == _OPO:
            return LEAPING_OPO if state.opo_fury >= 1 else DRAGON_KICK
        if form == _RAPTOR:
            return RISING_RAPTOR if state.raptor_fury >= 1 else TWIN_SNAKES
        return POUNCING_COEURL if state.coeurl_fury >= 1 else DEMOLISH

    def _blitz_goal(self, state: SimState) -> int:
        """The Beast-Chakra goal this PB window should build. Deterministic in
        the state (no params) so `pick_gcd` (pure) and `apply_cast` (the
        first-GCD commit) derive the same goal; the beam forks it explicitly.
        Both Nadi lit -> composition is irrelevant (Phantom Rush) -> the
        strongest line (3 opo). Lunar-only -> 3-distinct (Solar) so the NEXT
        blitz is the 1500 Phantom Rush. Else -> 3-same-opo (Lunar)."""
        if state.lunar and state.solar:
            return _GOAL_LUNAR
        if state.lunar:
            return _GOAL_SOLAR
        return _GOAL_LUNAR

    def _pb_pick(self, state: SimState) -> int:
        goal = state.pb_goal or self._blitz_goal(state)
        if goal == _GOAL_LUNAR:
            return self._form_pick(state, _OPO)
        # Solar: the first form family not yet banked — RAPTOR first, so the
        # first cast's family disambiguates the goal for `apply_cast`'s commit
        # (an opo-first line is lunar by construction; the NIN default-the-
        # ambiguous-case rule).
        for form in (_RAPTOR, _COEURL, _OPO):
            if not state.pb_types & _BIT[form]:
                return self._form_pick(state, form)
        return self._form_pick(state, _OPO)

    def _resolve_blitz(self, state: SimState) -> int:
        if state.lunar and state.solar:
            return PHANTOM_RUSH
        distinct = bin(state.pb_types).count("1")
        if distinct == 1:
            return ELIXIR_BURST
        if distinct == 3:
            return RISING_PHOENIX
        return CELESTIAL_REVOLUTION

    def _uptime_pick(self, state: SimState) -> int:
        """The best normal GCD: the current form's Fury-greedy action; Formless
        (or no form at all) enters at opo-opo, the strongest family."""
        t = state.t
        if state.formless_end > t or state.form == _NO_FORM or state.form_end <= t:
            return self._form_pick(state, _OPO)
        return self._form_pick(state, state.form)

    # --- GCD selection -------------------------------------------------------

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        # Forced chains first: the PB window, then the pending blitz.
        if state.pb_left > 0:
            return self._pb_pick(state)
        if state.beast_n >= 3:
            return self._resolve_blitz(state)
        # Pending replies: held into the RoF window, released before rumination
        # expiry (the fight-end escape is inherent — expiry always fires them).
        gcd = self.gcd_base_s
        if state.fire_rum_end > t:
            return FIRES_REPLY
        if state.wind_rum_end > t:
            rof_in = state.cd_ready.get(RIDDLE_OF_FIRE, 0.0) - t
            expiring = state.wind_rum_end - t < REPLY_EXPIRY_GCDS * gcd
            if state.rof_end > t or expiring or rof_in > state.wind_rum_end - t:
                return WINDS_REPLY
        # Fight-end squeeze: Six-Sided Star (780) beats any single final GCD.
        if state.fight_duration_s - t < SSS_LAST_SLOT_S:
            return SIX_SIDED_STAR
        return self._uptime_pick(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The beam's fork set. Forced chains are single moves; elsewhere the
        forks are (a) the lunar-vs-solar commit on the FIRST PB GCD, (b) the
        Fury spend-vs-bank choice (time the +200/+150 spenders into windows),
        (c) reply timing, and (d) the pre-downtime / fight-end Six-Sided Star."""
        t = state.t
        if state.beast_n >= 3:
            return [self._resolve_blitz(state)]
        if state.pb_left > 0:
            if state.pb_left == 3 and state.pb_goal == _GOAL_NONE \
                    and not (state.lunar and state.solar):
                # The strategic commit: 3-same-opo (Lunar) vs 3-distinct (Solar).
                lunar_first = self._form_pick(state, _OPO)
                solar_first = self._form_pick(state, _RAPTOR)
                return [self._pb_pick(state)] + \
                    [c for c in (lunar_first, solar_first)
                     if c != self._pb_pick(state)]
            return [self._pb_pick(state)]
        base = self.pick_gcd(state, params)
        out = [base]

        def _add(aid: int) -> None:
            if aid not in out:
                out.append(aid)

        # Reply-timing fork: hold vs release.
        if base in (FIRES_REPLY, WINDS_REPLY):
            _add(self._uptime_pick(state))
        elif state.fire_rum_end > t:
            _add(FIRES_REPLY)
        elif state.wind_rum_end > t:
            _add(WINDS_REPLY)
        # Fury spend-vs-bank fork within the current family.
        if base in (LEAPING_OPO, DRAGON_KICK):
            _add(DRAGON_KICK if base == LEAPING_OPO else LEAPING_OPO)
        elif base == RISING_RAPTOR:
            _add(TWIN_SNAKES)
        elif base == POUNCING_COEURL:
            _add(DEMOLISH)
        # Six-Sided Star at the fight end / into a downtime edge (its 4s recast
        # spills into dead time for free).
        remaining = state.fight_duration_s - t
        nxt = min((s for s, e in state.downtime_windows if s >= t),
                  default=float("inf"))
        if remaining < 8.0 or nxt - t < 2.0 * self.gcd_base_s:
            _add(SIX_SIDED_STAR)
        return out

    # --- oGCD selection ------------------------------------------------------

    def _tfc_due(self, state: SimState) -> float:
        d = state.fight_duration_s
        return state.tfc_total * state.t / d if d > 0 else 0.0

    def _tfc_now(self, state: SimState) -> bool:
        """Budgeted chakra pacing (the DNC pattern): linear schedule so the
        opener can't front-load luck, dumped into the RoF window up to the
        physical 2-TFC bank (10 chakra under Brotherhood), all-out at the end."""
        if state.tfc_left <= 0:
            return False
        used = state.tfc_total - state.tfc_left
        due = self._tfc_due(state)
        if state.rof_end > state.t or \
                state.fight_duration_s - state.t < TFC_ENDGAME_S:
            return used < due + TFC_BANK
        return used < due

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        # Riddle of Fire — the +15% window everything keys off; fired FIRST so
        # the payload (blitzes / replies / TFC dumps) lands inside it.
        if state.cd_ready.get(RIDDLE_OF_FIRE, 0.0) <= t \
                and not is_forbidden(RIDDLE_OF_FIRE, t, fw):
            return RIDDLE_OF_FIRE
        # Brotherhood — the party buff + the chakra feed, on cooldown at burst.
        if state.cd_ready.get(BROTHERHOOD, 0.0) <= t \
                and not is_forbidden(BROTHERHOOD, t, fw):
            return BROTHERHOOD
        # Riddle of Wind — arms Wind's Reply (auto haste itself is unmodeled).
        if state.cd_ready.get(RIDDLE_OF_WIND, 0.0) <= t \
                and not is_forbidden(RIDDLE_OF_WIND, t, fw):
            return RIDDLE_OF_WIND
        # Perfect Balance — gated so the blitz (4 GCDs out) lands inside RoF;
        # waits for a pending Wind's Reply (or the whole reply+blitz chain can't
        # fit the window); pre-window fire when RoF is imminent; fight-end and
        # charge-cap escapes.
        if (state.charges.get(PERFECT_BALANCE, 0.0) >= 1.0 and state.pb_left == 0
                and state.beast_n == 0 and state.wind_rum_end <= t
                and not is_forbidden(PERFECT_BALANCE, t, fw)):
            in_rof = state.rof_end > t
            rof_in = state.cd_ready.get(RIDDLE_OF_FIRE, 0.0) - t
            if ((in_rof and state.rof_end - t >= PB_BLITZ_FIT_S)
                    or (not in_rof and 0.0 < rof_in <= PB_ROF_SOON_S)
                    or state.fight_duration_s - t < PB_ENDGAME_S
                    or state.charges.get(PERFECT_BALANCE, 0.0) >= 1.97):
                return PERFECT_BALANCE
        # The Forbidden Chakra — the budgeted spend.
        if self._tfc_now(state):
            return TFC
        return None

    # --- Cast transitions ----------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Eligibility snapshot BEFORE any consumption (a window/stack never amps
        # or gates its own granting cast).
        opo_eligible = self._opo_eligible(state)

        # Incremental score (mirrors scoring.score_delivered_potency exactly,
        # raid buffs / tincture off): table potency + the state-derived Fury
        # bonuses, x Riddle of Fire, x the guaranteed crit when opo-eligible.
        base = md.POTENCIES.get(ability_id, 0)
        if ability_id == LEAPING_OPO and state.opo_fury >= 1:
            base += md.OPO_FURY_BONUS_P
        elif ability_id == RISING_RAPTOR and state.raptor_fury >= 1:
            base += md.RAPTOR_FURY_BONUS_P
        elif ability_id == POUNCING_COEURL and state.coeurl_fury >= 1:
            base += md.COEURL_FURY_BONUS_P
        if base > 0:
            m = md.RIDDLE_OF_FIRE_MULT if state.rof_end > t else 1.0
            if ability_id in md.ALWAYS_CRIT_IDS and opo_eligible:
                m *= md.GUARANTEED_CRIT_MULT
            state._score_flat += base * m

        # Form GCDs: Fury economy + the form / PB machine.
        if ability_id in md.FORM_GCD_IDS:
            fam = _FORM_OF[ability_id]
            # Fury consumption (after scoring).
            if ability_id == LEAPING_OPO and state.opo_fury >= 1:
                state.opo_fury -= 1
            elif ability_id == RISING_RAPTOR and state.raptor_fury >= 1:
                state.raptor_fury -= 1
            elif ability_id == POUNCING_COEURL and state.coeurl_fury >= 1:
                state.coeurl_fury -= 1
            # Fury grants (form-gated: opo needs opo-eligibility; raptor/coeurl
            # actions require their form / PB / Formless to execute at all).
            elif ability_id == DRAGON_KICK and opo_eligible:
                state.opo_fury = min(1, state.opo_fury + 1)
            elif ability_id == TWIN_SNAKES:
                state.raptor_fury = min(1, state.raptor_fury + 1)
            elif ability_id == DEMOLISH:
                state.coeurl_fury = min(2, state.coeurl_fury + 2)
            # PB stacks bank a Beast Chakra; otherwise the form wheel advances.
            if state.pb_left > 0:
                if state.pb_goal == _GOAL_NONE:
                    # Beam-forked first GCD commits the goal by its form family.
                    state.pb_goal = (_GOAL_LUNAR if fam == _OPO
                                     else _GOAL_SOLAR)
                state.pb_left -= 1
                state.beast_n += 1
                state.pb_types |= _BIT[fam]
            else:
                if state.formless_end > t:
                    state.formless_end = float("-inf")
                state.form = _NEXT_FORM[fam]
                state.form_end = t + md.FORM_DURATION_S
            return

        # Blitzes: Nadi bookkeeping + Formless.
        if ability_id in md.BLITZ_IDS:
            if ability_id == PHANTOM_RUSH:
                state.lunar = state.solar = False
            elif ability_id == ELIXIR_BURST:
                state.lunar = True
            elif ability_id == RISING_PHOENIX:
                state.solar = True
            elif ability_id == CELESTIAL_REVOLUTION:
                if state.lunar:
                    state.solar = True
                else:
                    state.lunar = True
            state.beast_n = 0
            state.pb_types = 0
            state.pb_goal = _GOAL_NONE
            state.formless_end = t + md.FORMLESS_DURATION_S
            return

        # Special GCDs.
        if ability_id == WINDS_REPLY:
            state.wind_rum_end = float("-inf")
            return
        if ability_id == FIRES_REPLY:
            state.fire_rum_end = float("-inf")
            state.formless_end = t + md.FORMLESS_DURATION_S
            return
        if ability_id == FORM_SHIFT:
            state.formless_end = t + md.FORMLESS_DURATION_S
            return
        if ability_id in (SIX_SIDED_STAR, MEDITATION):
            return

        # oGCD effects (window opens AFTER scoring the granting cast).
        if ability_id == RIDDLE_OF_FIRE:
            state.rof_end = t + md.RIDDLE_OF_FIRE_DURATION_S
            state.fire_rum_end = t + md.FIRES_RUMINATION_DURATION_S
        elif ability_id == RIDDLE_OF_WIND:
            state.wind_rum_end = t + md.WINDS_RUMINATION_DURATION_S
        elif ability_id == PERFECT_BALANCE:
            state.pb_left = md.PB_STACKS
            state.pb_goal = _GOAL_NONE
        elif ability_id == TFC:
            state.tfc_left = max(0, state.tfc_left - 1)

        apply_cooldown(state, self.cooldowns, ability_id)

    # --- Downtime ------------------------------------------------------------

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        """MNK's downtime moves: Forbidden Meditation pumps chakra through the
        window (its yield is already inside the measured TFC budget, so the 1s
        GCDs are emitted for timeline realism only — matching the observed x5)
        and Form Shift re-arms Formless Fist at the edge so re-engage opens with
        a full-value opo GCD."""
        t = state.t
        if win_end - t < DOWNTIME_PRIME_MIN_S \
                or win_end > state.fight_duration_s - 1.0:
            return
        n = min(DOWNTIME_MEDITATIONS,
                max(0, int((win_end - t - 2.0) / MEDITATION_GCD_S)))
        for i in range(n):
            state.timeline.append(
                (win_end - 2.0 - MEDITATION_GCD_S * (n - i), MEDITATION))
        state.timeline.append((win_end - 1.0, FORM_SHIFT))
        state.formless_end = win_end + md.FORMLESS_DURATION_S

    # --- Beam search seam ----------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental self-buff-aware running score
        plus admissible-ish credits for banked resources (Fury stacks, Beast
        Chakra, the lit-Nadi Phantom Rush premium, the remaining TFC budget), so
        an investing line isn't pruned before it pays off. The final selection
        always re-scores with the exact score_fn."""
        credit = (state.opo_fury * _PRUNE_OPO_FURY_P
                  + state.raptor_fury * _PRUNE_RAPTOR_FURY_P
                  + state.coeurl_fury * _PRUNE_COEURL_FURY_P
                  + state.beast_n * _PRUNE_BEAST_P
                  + state.tfc_left * _PRUNE_TFC_P)
        if state.lunar and state.solar:
            credit += _PRUNE_BOTH_NADI_P
        return state._score_flat + credit

    def beam_signature(self, state: SimState):
        """Bucketed decision-state key (the GNB lesson: a lossless timer key
        fragments the beam into sub-GCD near-duplicates and starves the effective
        width). Gauges, form/PB/Nadi state and the TFC budget are exact; window
        and cooldown timers are bucketed to ~one GCD. `state.t` is bucketed too —
        MNK beams don't stay in lockstep across a Six-Sided-Star (4s) fork or a
        downtime re-entry."""
        t = state.t

        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)

        return (
            int(t / _SIG_BUCKET_S),
            state.form if state.form_end > t else _NO_FORM,
            state.formless_end > t,
            state.opo_fury, state.raptor_fury, state.coeurl_fury,
            state.pb_left, state.pb_goal, state.beast_n, state.pb_types,
            state.lunar, state.solar,
            state.tfc_left,
            slot(state.rof_end - t),
            slot(state.fire_rum_end - t), slot(state.wind_rum_end - t),
            int(state.charges.get(PERFECT_BALANCE, 0.0) * 4),
            slot(state.cd_ready.get(RIDDLE_OF_FIRE, 0.0) - t),
            slot(state.cd_ready.get(RIDDLE_OF_WIND, 0.0) - t),
            slot(state.cd_ready.get(BROTHERHOOD, 0.0) - t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding ------------------------------------

def _default_ctx(duration_s: float) -> MonkCtx:
    return MonkCtx(tfc_budget=max(0, int(duration_s / _DEFAULT_TFC_RATE_S)))


def _model_for(sim_context, duration_s: float) -> MonkRotationModel:
    """Build the model bound to this run's per-pull context: a per-player
    effective GCD (CeilingContext) and/or the measured chakra budget (MonkCtx).
    `None` -> the default model (rate-based budget)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    if isinstance(payload, MultiTargetContext):
        payload = payload.inner
    ctx = payload if isinstance(payload, MonkCtx) else None
    return MonkRotationModel(gcd_base_s=gcd, ctx=ctx, duration_hint_s=duration_s)


def _score(timeline, aux, buff_intervals):
    """Engine-facing score_fn (lazy import to avoid a scoring<->simulator cycle)."""
    from jobs.monk.scoring import score_delivered_potency
    return score_delivered_potency(timeline, buff_intervals=buff_intervals)


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(sim_context, duration_key)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, _score, duration_key, list(downtime_tuple), buff_intervals,
        width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The MNK ceiling: the diverse beam over the lunar/solar, Fury-banking and
    reply-timing forks on top of the burst-timing refinement. (Beam-only, like
    DRG/GNB/NIN — the exact DP seam is deferred.)"""
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
    — MNK has no pet/payload scalar, so aux is always 0."""
    if params is None:
        params = SimParams()
    model = _model_for(sim_context, fight_duration_s)
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
    model = _model_for(sim_context, fight_duration_s)
    return engine.canonical_aligned(model, _score, fight_duration_s,
                                    downtime_windows or [], buff_intervals)
