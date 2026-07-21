"""Idealized BLM rotation — the Black Mage `RotationModel` for the shared engine.

The second **caster** simulator (after Red Mage), and the first job with a true
**MP phase economy**. The time loop, downtime/weave/charge handling, parameter
sweep, local-search refinement and canonical buff alignment all live in
`jobs/_core/sim/engine.py`. This module supplies only the BLM-specific rotation:
the Astral Fire / Umbral Ice phase machine, the MP gate, the Polyglot / Astral
Soul economy, the High Thunder DoT, and the Ley Lines haste window. The four
`simulate_*` shims at the bottom bind this model to the engine (names kept so the
sidecar, the scorer and the tests call them unchanged).

BLM-specific rotation encoded:
- **Phase machine.** A Fire phase under Astral Fire III (Fire IV ×6 → Flare Star
  → Despair, draining MP) alternates with a short Ice phase under Umbral Ice III
  (Blizzard III → Blizzard IV, refilling MP). **MP gates the fire phase**: a Fire
  IV only fires when enough MP remains to *finish* the 6-soul set, so the cast mix
  (6 Fire IV + Paradox + Flare Star + Despair per cycle) is correct by construction.
- **Manafont** (full MP refill + AF3 + hearts + Thunderhead + Paradox) extends the
  fire phase with a second Fire IV batch — fired at a set boundary (mp spent), so
  it cleanly doubles the fire phase rather than firing mid-set. Aligned to the
  2-min burst by the refinement.
- **Polyglot** accrues deterministically (1 / 30s of Enochian) and is spent on
  Xenoglossy (890, the highest-potency GCD) in the fire phase; Amplifier grants a
  bonus stack. No RNG, so — unlike RDM's procs — no proc-budget sim_context.
- **High Thunder** DoT maintained (refreshed near expiry while Thunderhead is up);
  scored per cast by time-to-next-cast (see scoring.py).
- **Ley Lines** is a ~15% haste window (a reduced-GCD window via `gcd_duration`,
  the caster analog of MCH Overheated), held into raid buffs.
- **Firestarter** makes the ice→fire Fire III instant + free (granted by the
  fire-phase Paradox, carried through ice).

Multi-target (N>=2): the phase machine forks to the AoE rotation. The fire filler
swaps Fire IV -> Flare (the divergent +3-Astral-Soul economy, two Flares -> Flare
Star), and the gauge-equivalent spells take their higher-potency variant per slot
(Blizzard III/IV <-> High Blizzard II / Freeze, Xenoglossy <-> Foul); Flare Star
always cleaves. High Thunder stays the ST DoT (its DoT is scored single-target, so
a player's AoE High Thunder II is under-credited — the <=100%-safe direction).
N==1 -> byte-identical single-target ceiling.

Out of scope for v1 (documented, intentionally not modeled):
- Triplecast / Swiftcast as DPS — on a stationary ceiling instant-casts add no
  potency (every cast already happens); they're movement/utility, held by the
  player, so the sim doesn't fire them (keeps the ceiling honest).
- Enochian decay during downtime (the ceiling assumes it's maintained via
  Umbral Soul / Transpose, as top players do); frame-perfect MP-tick timing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.blackmage import data as bd


# --- Ability IDs (aliased from data for readability) ----------------------
FIRE_III     = bd.FIRE_III
FIRE_IV      = bd.FIRE_IV
DESPAIR      = bd.DESPAIR
FLARE_STAR   = bd.FLARE_STAR
BLIZZARD_III = bd.BLIZZARD_III
BLIZZARD_IV  = bd.BLIZZARD_IV
HIGH_THUNDER = bd.HIGH_THUNDER
PARADOX      = bd.PARADOX
XENOGLOSSY   = bd.XENOGLOSSY
MANAFONT     = bd.MANAFONT
LEY_LINES    = bd.LEY_LINES
AMPLIFIER    = bd.AMPLIFIER
# AoE line (cast only in multi-target windows; gated on N>=2).
FLARE            = bd.FLARE
FOUL             = bd.FOUL
HIGH_BLIZZARD_II = bd.HIGH_BLIZZARD_II
FREEZE           = bd.FREEZE


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S = 2.50           # BLM base GCD (per-player Spell Speed threaded via gcd_base_s)
# Refresh High Thunder when the DoT has <= this left (and Thunderhead is up).
# Scored by time-to-next (capped at the duration), so refreshing a hair early just
# credits less — overcap-safe.
THUNDER_REFRESH_AT_S = 3.0
# MP needed to FINISH the current 6-soul set (so a Fire IV is only started when it
# can be turned into a Flare Star) = remaining souls × Fire IV cost.
_FULL_SET_MP = bd.ASTRAL_SOUL_CAP * bd.MP_COSTS[FIRE_IV]
# Pre-pull Fire III channel begincast time (3.5s cast during the countdown,
# resolving at t≈0) — placed in the pre-zone, begincast-anchored like RDM's.
PREPULL_CHANNEL_T = -bd.CAST_TIMES[FIRE_III]
# AoE fire filler (N>=2): Flare builds +3 Astral Soul, so a per-fire-entry count of
# 2 closes the 6-soul set -> Flare Star. The MP drain is modeled so two Flares
# exhaust a fire phase (then the rotation returns to ice); its exact value is NOT
# load-bearing for the <=100% bound (that comes from the per-slot AoE>=ST potency,
# plus the gauge-equivalent ice/Polyglot max-swaps), only for cast-count
# calibration. ⚠️ refine against a live AoE pull (scripts/validate_job_ceiling.py).
FLARE_AOE_MP = 4500
FLARE_MIN_MP = 0
_AOE_FLARES_PER_SET = bd.ASTRAL_SOUL_CAP // 3   # 2 Flares (+3 each) -> 6 Astral Soul


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """BLM picker tunables — no axis beyond the shared knobs in v1
    (max_weaves_per_gcd / forbidden_windows). The MP economy makes the ST line
    largely forced, so there is no GCD-fork sweep (measure-first, like RPR)."""
    pass


@dataclass
class SimState(SimStateBase):
    mp: int = bd.MP_CAP
    astral_fire: int = 0          # 0–3
    umbral_ice: int = 0           # 0–3
    umbral_hearts: int = 0        # 0–3 (tracked for realism; doesn't bind MP in ST)
    astral_soul: int = 0          # 0–6 -> Flare Star
    polyglot: int = 0             # 0–3 -> Xenoglossy
    firestarter: bool = False     # next Fire III instant + free
    thunderhead: bool = False     # enables High Thunder
    paradox_ready: bool = False   # FIRE Paradox available (lit entering Astral Fire)
    umbral_paradox_ready: bool = False  # ICE Paradox available (UI3 + 3 Umbral Hearts)
    thunder_dot_end: float = 0.0  # High Thunder DoT expiry
    ley_end: float = 0.0          # Ley Lines haste window end
    instant_this_slot: bool = False  # captured in gcd_duration for weave_budget
    aoe_flare_count: int = 0      # Flares cast this fire entry (caps the AoE set at 2)
    # Polyglot accrual schedule (times of each +1, 1 / 30s of Enochian).
    polyglot_schedule: list[float] = field(default_factory=list)


# --- Refinement / canonical anchors ---------------------------------------
# Hold the 2-min burst enablers (Ley Lines / Manafont / Amplifier) into raid-buff
# windows; the payoff GCDs (Flare Star, Xenoglossy, Despair) follow them.
_PERFECT_ANCHORS: tuple[int, ...] = (LEY_LINES, MANAFONT, AMPLIFIER)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (LEY_LINES, MANAFONT, AMPLIFIER)

# Sweep axes (kept job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)


# --- The BLM rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    bd.JOB_DATA.tincture_main_stat, bd.JOB_DATA.tincture_role_coeff)


class BlackMageRotationModel(engine.BaseRotationModel):
    cooldowns = bd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=GCD_BASE_S, cast_times=bd.CAST_TIMES)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=2 the fire
        # filler swaps to Flare (the divergent +3-Astral-Soul economy) and the
        # gauge-equivalent ice / Polyglot spells take their higher-potency AoE
        # variant. Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed (threaded only when faster than the constant): SpS
        # scales BOTH the GCD recast AND the cast times by the same haste factor, so
        # rebuild the HardcastGCD with both scaled. None keeps the tier constant,
        # byte-identical. Ley Lines haste multiplies on top (in gcd_duration).
        if gcd_base_s is not None:
            factor = gcd_base_s / GCD_BASE_S
            self.timing = replace(
                BlackMageRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in bd.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        """Live target count at `t` from the multi-target schedule (1 if none)."""
        return n_at(t, self.mt_schedule)

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {MANAFONT: 0.0, AMPLIFIER: 0.0}
        state.charges = {LEY_LINES: 2.0}    # 2-charge oGCD (DT 7.1)
        state.mp = bd.MP_CAP
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull Fire III hardcast during the countdown -> resolves at t≈0. This
        # establishes Astral Fire III + Thunderhead + Paradox-ready and costs 2000
        # MP (no Firestarter at the very start). It's free in-fight time (the cast
        # was spent pre-fight) and real opener damage, so it's always taken.
        state.timeline.append((PREPULL_CHANNEL_T, FIRE_III))
        state.astral_fire = 3
        state.mp = bd.MP_CAP - bd.FIRE_III_HARDCAST_MP
        state.thunderhead = True
        state.paradox_ready = True
        # Polyglot accrual schedule (Enochian starts at the pre-pull cast).
        dur = state.fight_duration_s
        n = int(dur / bd.POLYGLOT_INTERVAL_S) + 1
        state.polyglot_schedule = [bd.POLYGLOT_INTERVAL_S * (i + 1) for i in range(n)]

    def _release_polyglot(self, state: SimState) -> None:
        """Accrue Polyglot stacks now due (1 / 30s of Enochian, capped; overflow is
        wasted, which the picker avoids by spending Xenoglossy)."""
        while state.polyglot_schedule and state.polyglot_schedule[0] <= state.t:
            state.polyglot_schedule.pop(0)
            state.polyglot = min(bd.POLYGLOT_CAP, state.polyglot + 1)

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Two hardcasts in the ST line never eat their cast lock, so the ceiling
        # models them recast-bound ("instant"):
        #   * Fire III under Firestarter (free + instant — the ice->fire entry).
        #   * Blizzard III, the ice entry — cast FROM Astral Fire III, which halves
        #     opposite-element cast times (3.5 -> 1.75s <= the recast slot), and top
        #     parses Swiftcast / Triplecast it besides (probe 2026-07-02: every
        #     top-pull Blizzard III resolves at/below the ~2.45s recast, never the
        #     3.5s bar). A player who hardcasts the entry unaspected scores below
        #     the ceiling, as they should.
        instant_hardcast = (gcd_id == BLIZZARD_III
                            or (gcd_id == FIRE_III and state.firestarter))
        base_cast = 0.0 if instant_hardcast else self.timing._cast_time(gcd_id)
        instant = base_cast <= 0.0
        state.instant_this_slot = instant
        slot = (self.timing.gcd_recast_s if instant
                else max(base_cast, self.timing.gcd_recast_s))
        # Ley Lines haste window (multiplies the whole slot — equivalent to hasting
        # both cast and recast, since max(a,b)·h == max(a·h, b·h)).
        if state.t < state.ley_end:
            slot *= bd.LEY_LINES_HASTE
        return slot

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        base = (self.timing.instant_weaves if state.instant_this_slot
                else self.timing.hardcast_weaves)
        return min(base, params.max_weaves_per_gcd)

    def pick_gcd(self, state: SimState, params) -> int:
        self._release_polyglot(state)
        if state.astral_fire > 0:
            return self._pick_fire(state, params)
        if state.umbral_ice > 0:
            return self._pick_ice(state, params)
        return FIRE_III    # unaspected (pre-opener / post-long-downtime) -> enter fire

    def _ice_entry(self, n: int) -> int:
        """The fire->ice transition GCD. Blizzard III and High Blizzard II are
        gauge-equivalent (both -> UI3 + reset + MP regen), so take the higher
        potency at the live target count — High Blizzard II wins at N>=3, Blizzard
        III at N<=2. At N==1 this is Blizzard III, byte-identical."""
        return (HIGH_BLIZZARD_II
                if potency_for(HIGH_BLIZZARD_II, n, bd.JOB_DATA) > bd.POTENCIES[BLIZZARD_III]
                else BLIZZARD_III)

    def _heart_spell(self, n: int) -> int:
        """The Umbral-Hearts builder. Blizzard IV and Freeze are gauge-equivalent
        (both -> 3 Umbral Hearts + ice Paradox), so take the higher potency — Freeze
        wins at N>=3, Blizzard IV at N<=2. At N==1 this is Blizzard IV,
        byte-identical."""
        return (FREEZE
                if potency_for(FREEZE, n, bd.JOB_DATA) > bd.POTENCIES[BLIZZARD_IV]
                else BLIZZARD_IV)

    def _pick_fire(self, state: SimState, params) -> int:
        n = self._n(state.t)
        # 1. Refresh the High Thunder DoT near expiry (Thunderhead up). Kept as the
        #    ST High Thunder even at N>=2: its DoT is scored single-target, so the
        #    ceiling's DoT >= a player's AoE High Thunder II (which we under-credit).
        if state.thunderhead and (state.thunder_dot_end - state.t) <= THUNDER_REFRESH_AT_S:
            return HIGH_THUNDER
        # 2. Spend a full Astral Soul gauge (Flare Star cleaves -> always optimal).
        if state.astral_soul >= bd.ASTRAL_SOUL_CAP:
            return FLARE_STAR
        # 3. Paradox (re-arms Firestarter for the next ice->fire entry). Shared,
        #    single-target 540 — kept at every N (the player casts it too).
        if state.paradox_ready and state.mp >= bd.MP_COSTS[PARADOX]:
            return PARADOX
        # 4. Spend Polyglot — Foul out-potencies Xenoglossy from N>=2 (1050 vs 890),
        #    same Polyglot cost (gauge-equivalent), so take the higher.
        if state.polyglot >= 1:
            return FOUL if n >= 2 else XENOGLOSSY
        # 5. Build the soul set.
        if n >= 2:
            # AoE fire: Flare (+3 Astral Soul) builds the 6-soul set in two casts.
            # The per-entry count closes it at exactly 2 -> Flare Star; the AoE fire
            # phase out-potencies the ST Fire IV line from N==2 (Flare 408 vs Fire IV
            # 300, and Flare Star / Foul cleave).
            if state.aoe_flare_count < _AOE_FLARES_PER_SET and state.mp >= FLARE_MIN_MP:
                return FLARE
        else:
            # ST: build only if MP can FINISH the set (so a started set always reaches
            # its Flare Star — the MP gate that fixes the cast mix).
            need = (bd.ASTRAL_SOUL_CAP - state.astral_soul) * bd.MP_COSTS[FIRE_IV]
            if state.mp >= need:
                return FIRE_IV
        # 6. Can't continue fire: dump remaining MP, then leave for ice.
        if state.mp >= bd.DESPAIR_MIN_MP:
            return DESPAIR
        return self._ice_entry(n)    # enter the ice phase

    def _pick_ice(self, state: SimState, params) -> int:
        n = self._n(state.t)
        # Refresh the DoT in ice if due (Thunderhead granted on the transition).
        if state.thunderhead and (state.thunder_dot_end - state.t) <= THUNDER_REFRESH_AT_S:
            return HIGH_THUNDER
        # Blizzard IV / Freeze — grants the 3 Umbral Hearts (and lights the ice Paradox).
        if state.umbral_hearts < bd.UMBRAL_HEARTS_CAP:
            return self._heart_spell(n)
        # Ice Paradox — a free 540-potency instant (UI3 + 3 hearts). Part of every
        # cycle: BLM casts Paradox in BOTH phases.
        if state.umbral_paradox_ready:
            return PARADOX
        # Top off MP if still short (rare — the two Blizzards usually fill it).
        if state.mp < bd.MP_CAP:
            return self._heart_spell(n)
        return FIRE_III    # MP full -> back to fire (instant + free under Firestarter)

    def pick_ogcd(self, state: SimState, params):
        self._release_polyglot(state)
        t = state.t
        fw = params.forbidden_windows
        # Ley Lines — haste window, fire phase only (where the casts are densest).
        # 2 charges; can't be cast while already standing in Ley Lines (so the 2nd
        # charge is held, not double-stacked).
        if (state.astral_fire > 0 and state.charges.get(LEY_LINES, 0) >= 1
                and state.t >= state.ley_end
                and not is_forbidden(LEY_LINES, t, fw)):
            return LEY_LINES
        # Amplifier — +1 Polyglot when not capped.
        if (state.cd_ready.get(AMPLIFIER, 0) <= t and state.polyglot < bd.POLYGLOT_CAP
                and not is_forbidden(AMPLIFIER, t, fw)):
            return AMPLIFIER
        # Manafont — full MP refill, fired at a fire-phase set boundary (MP spent)
        # so it cleanly enables a second Fire IV batch instead of firing mid-set.
        if (state.astral_fire > 0 and state.astral_soul == 0
                and state.mp < bd.DESPAIR_MIN_MP
                and state.cd_ready.get(MANAFONT, 0) <= t
                and not is_forbidden(MANAFONT, t, fw)):
            return MANAFONT
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Ice spells refill MP (net positive under UI3 regen) — ST + AoE variants.
        if ability_id in (BLIZZARD_III, BLIZZARD_IV, HIGH_BLIZZARD_II, FREEZE):
            state.mp = min(bd.MP_CAP, state.mp + bd.UI3_MP_REGEN_PER_GCD)

        # Generic cooldown / charges (Manafont / Ley Lines / Amplifier).
        apply_cooldown(state, self.cooldowns, ability_id)

        # Per-ability effects + MP costs.
        if ability_id == FIRE_IV:
            state.mp = max(0, state.mp - bd.MP_COSTS[FIRE_IV])
            state.astral_soul = min(bd.ASTRAL_SOUL_CAP, state.astral_soul + 1)
        elif ability_id == FLARE:
            # AoE soul builder (N>=2): +3 Astral Soul, grants AF3, drains MP. The
            # per-fire-entry count gate (in _pick_fire) closes the set at 2 Flares
            # -> 6 soul -> Flare Star.
            state.mp = max(0, state.mp - FLARE_AOE_MP)
            state.astral_soul = min(bd.ASTRAL_SOUL_CAP, state.astral_soul + 3)
            state.astral_fire = 3
            state.aoe_flare_count += 1
        elif ability_id == FLARE_STAR:
            state.astral_soul = 0
        elif ability_id == PARADOX:
            if state.astral_fire > 0:
                # Fire Paradox: 1600 MP, re-arms Firestarter for the next ice->fire entry.
                state.mp = max(0, state.mp - bd.MP_COSTS[PARADOX])
                state.firestarter = True
                state.paradox_ready = False
            else:
                # Umbral Ice Paradox: free instant (no MP, no Firestarter).
                state.umbral_paradox_ready = False
        elif ability_id == DESPAIR:
            state.mp = 0
        elif ability_id == FIRE_III:
            if state.firestarter:
                state.firestarter = False           # free + instant
            else:
                state.mp = max(0, state.mp - bd.FIRE_III_HARDCAST_MP)
            state.astral_fire = 3
            state.umbral_ice = 0
            state.astral_soul = 0
            state.aoe_flare_count = 0               # fresh fire entry
            state.thunderhead = True
            state.paradox_ready = True
            state.umbral_paradox_ready = False
        elif ability_id in (BLIZZARD_III, HIGH_BLIZZARD_II):
            # Ice entry (ST / AoE, gauge-equivalent): -> UI3, drop AF, reset soul.
            state.umbral_ice = 3
            state.astral_fire = 0
            state.astral_soul = 0
            state.aoe_flare_count = 0
            state.thunderhead = True
        elif ability_id in (BLIZZARD_IV, FREEZE):
            # Umbral Hearts builder (ST / AoE, gauge-equivalent).
            state.umbral_hearts = bd.UMBRAL_HEARTS_CAP
            state.umbral_paradox_ready = True    # UI3 + 3 hearts -> ice Paradox available
        elif ability_id == HIGH_THUNDER:
            state.thunderhead = False
            state.thunder_dot_end = t + bd.HIGH_THUNDER_DOT_DURATION_S
        elif ability_id in (XENOGLOSSY, FOUL):
            state.polyglot = max(0, state.polyglot - 1)
        elif ability_id == MANAFONT:
            state.mp = bd.MP_CAP
            state.astral_fire = 3
            state.umbral_hearts = bd.UMBRAL_HEARTS_CAP
            state.aoe_flare_count = 0               # new fire batch
            state.thunderhead = True
            state.paradox_ready = True
        elif ability_id == AMPLIFIER:
            state.polyglot = min(bd.POLYGLOT_CAP, state.polyglot + 1)
        elif ability_id == LEY_LINES:
            state.ley_end = t + bd.LEY_LINES_DURATION_S

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _model_for(duration_s: float, sim_context) -> BlackMageRotationModel:
    """Build a model bound to this run's per-pull context: the per-player effective
    GCD (CeilingContext, faster-than-constant Spell Speed) and any
    `MultiTargetContext` (the AoE N(t) schedule). BLM is RNG-free, so there's no
    proc payload; the entry-gauge payload (phase-continuation pulls) is wired here
    when calibration adds it. `None`/none -> the default model, byte-identical."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
    return BlackMageRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy import to avoid a scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.blackmage.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests / helpers call `_score`).
_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — BLM has no pet/
    payload scalar, so aux is always 0."""
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
    """Sweep SimParams, return the highest-scoring (timeline, 0)."""
    model = _model_for(fight_duration_s, sim_context)
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
    """Perfect simulator: sweep + local-search refinement (buff-aware when given),
    then the shared raid-window burst max-guard (Ley Lines + Manafont + Amplifier
    aligned to the party window; max-guarded so it never regresses). BLM has no
    GCD-fork, so this matches `beam_perfect` at width 1."""
    dt = downtime_windows or []
    model = _model_for(fight_duration_s, sim_context)
    score = _make_score(model.mt_schedule)
    base = engine.perfect(model, score, fight_duration_s, dt, buff_intervals)
    return engine.canonical_aligned_max_guard(
        model, score, fight_duration_s, dt, buff_intervals, base, beam_width=None)


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
