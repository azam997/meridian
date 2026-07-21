"""Idealized WAR rotation — the Warrior `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the WAR-specific rotation: the Beast Gauge state, the
combo / Inner Release proc machine, the priority pickers, and the per-cast
transitions. The four `simulate_*` functions at the bottom are thin shims that
bind this model to the engine (kept under their original names so the sidecar,
the scorer and the tests call them unchanged).

WAR-specific rotation encoded:
- One offensive gauge: Beast (combo finishers build it — Maim +10, Storm's Eye
  +10, Storm's Path +20; Infuriate +50 instant). Fell Cleave / Inner Chaos spend
  50.
- Main combo: Heavy Swing -> Maim -> Storm's Eye (refresh Surging Tempest) or
  Storm's Path (more gauge, when Surging Tempest doesn't need a refresh).
- Burst (every 60s): Inner Release grants 3 free guaranteed-crit-DH weaponskills
  (Fell Cleave, or Inner Chaos when Nascent Chaos is up from Infuriate) + Primal
  Rend; the 3rd consumes Wrathful -> Primal Wrath (oGCD); Primal Rend -> Primal
  Ruination. Upheaval / Onslaught weave on cooldown.
- Surging Tempest upkeep: Storm's Eye refreshes the 30s (bankable-60s) buff. The
  10% amp itself is applied in scoring (full-coverage overlay), not here — the
  sim only tracks the expiry to choose Storm's Eye vs Storm's Path.

Out of scope for v1 (documented, intentionally not modeled):
- AoE line (Overpower / Mythril Tempest / Decimate / Orogeny / Chaotic Cyclone) —
  single-target ceiling only.
- Tomahawk as steady-state filler (a real GCD always out-potencies it; only ever
  used when forced off the boss, which the downtime model already handles).
- Defensive gauge spends (none — Beast is fully offensive) / MP.
- Frame-perfect animation timing.
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.entry_gauge import EntryState, seed_entry_gauge
from jobs._core.tincture import spec_for_job
from jobs.warrior import data as wd


# --- Ability IDs (aliased from data for readability) -----------------------
HEAVY_SWING      = wd.HEAVY_SWING
MAIM             = wd.MAIM
STORMS_PATH      = wd.STORMS_PATH
STORMS_EYE       = wd.STORMS_EYE
FELL_CLEAVE      = wd.FELL_CLEAVE
INNER_CHAOS      = wd.INNER_CHAOS
INNER_RELEASE    = wd.INNER_RELEASE
PRIMAL_REND      = wd.PRIMAL_REND
PRIMAL_RUINATION = wd.PRIMAL_RUINATION
PRIMAL_WRATH     = wd.PRIMAL_WRATH
INFURIATE        = wd.INFURIATE
UPHEAVAL         = wd.UPHEAVAL
ONSLAUGHT        = wd.ONSLAUGHT
# AoE line (cast in multi-target windows; gauge-equivalent to the ST counterparts).
OVERPOWER        = wd.OVERPOWER
MYTHRIL_TEMPEST  = wd.MYTHRIL_TEMPEST
DECIMATE         = wd.DECIMATE
CHAOTIC_CYCLONE  = wd.CHAOTIC_CYCLONE
OROGENY          = wd.OROGENY

# Single-target -> AoE-counterpart spender substitutions (Beast-equivalent, so the
# choice is closed-form on direct potency). The 3-step ST combo maps to the 2-step
# AoE combo (Overpower -> Mythril Tempest) separately, by per-GCD average.
_AOE_GCD_SWAP: dict[int, int] = {
    FELL_CLEAVE: DECIMATE,
    INNER_CHAOS: CHAOTIC_CYCLONE,
}


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S        = 2.5     # WAR base GCD (no SkS gear-aware adjustment in v1)
# Refresh Surging Tempest when its remaining time drops to this. Generous so the
# idealized rotation never lets the buff fall off (the ceiling assumes full
# coverage, so a sim that dropped it would understate the ceiling). The refresh
# can only happen at a combo finisher, so the headroom must exceed a full combo.
SURGING_REFRESH_AT_S = 14.0


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """WAR picker tunables, on top of the shared knobs (max_weaves_per_gcd /
    triple_weave_clip_s / forbidden_windows):
      * `hold_gauge_for_burst` — bank Beast Gauge for the Inner Release window
        (True) instead of greedily dumping Fell Cleave between combos as soon as
        50 is available (False). Both are valid; the sweep picks the higher-
        scoring per duration."""
    hold_gauge_for_burst: bool = True


@dataclass
class SimState(SimStateBase):
    beast: int = 0
    # Main combo: 0 expects Heavy Swing, 1 Maim, 2 a finisher (Eye/Path).
    combo_step: int = 0
    # AoE combo: 0 expects Overpower, 1 Mythril Tempest.
    aoe_combo_step: int = 0
    # Surging Tempest expiry (fight-relative seconds); drives Eye-vs-Path.
    surging_end: float = 0.0
    # Inner Release: free guaranteed-crit-DH weaponskill stacks (0-3).
    inner_release: int = 0
    nascent_chaos: bool = False        # next Fell Cleave -> Inner Chaos (from Infuriate)
    primal_rend_ready: bool = False    # granted by Inner Release
    primal_ruination_ready: bool = False
    wrathful: bool = False             # Primal Wrath available (after 3 IR weaponskills)


# --- Refinement / canonical anchors ---------------------------------------
# The greedy picker fires the burst (Inner Release / Infuriate) as soon as it's
# available; the refinement nudges these into raid-buff windows.
_ANCHORS: tuple[int, ...] = (INNER_RELEASE, INFURIATE)

# Sweep axes (kept here so the shape stays job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_SWEEP_HOLD_GAUGE: tuple[bool, ...] = (True, False)


# --- The WAR rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    wd.JOB_DATA.tincture_main_stat, wd.JOB_DATA.tincture_role_coeff)


class WarriorRotationModel(engine.BaseRotationModel):
    cooldowns = wd.COOLDOWNS
    timing = InstantGCD(base_s=GCD_BASE_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, entry: EntryState | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()) -> None:
        self.entry = entry
        # Per-player Skill Speed: a faster-than-constant effective GCD (threaded only
        # when the player has it) speeds the whole rotation; None keeps the tier
        # constant, byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=2 the picker
        # swaps in AoE buttons (Mythril Tempest maintains Surging Tempest, like
        # Storm's Eye). Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_aoe(self, state: "SimState", gcd: int) -> int:
        """Substitute the AoE counterpart of `gcd` when the target count makes it
        win. Beast-gauge-equivalent, so the per-slot spender choice is closed-form;
        the 3-step ST combo maps to the 2-step AoE combo (Overpower -> Mythril
        Tempest) by per-GCD average. N<2 -> unchanged (byte-identical)."""
        n = self._n(state.t)
        if n < 2:
            return gcd
        jd = wd.JOB_DATA
        if gcd in (HEAVY_SWING, MAIM, STORMS_EYE, STORMS_PATH):
            st_avg = (jd.potencies[HEAVY_SWING] + jd.potencies[MAIM]
                      + jd.potencies[STORMS_PATH]) / 3.0
            aoe_avg = (potency_for(OVERPOWER, n, jd)
                       + potency_for(MYTHRIL_TEMPEST, n, jd)) / 2.0
            if aoe_avg > st_avg:
                return (OVERPOWER if state.aoe_combo_step == 0
                        else MYTHRIL_TEMPEST)
            return gcd
        alt = _AOE_GCD_SWAP.get(gcd)
        if alt is not None and potency_for(alt, n, jd) > potency_for(gcd, n, jd):
            return alt
        return gcd

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {INNER_RELEASE: 0.0, UPHEAVAL: 0.0}
        state.charges = {INFURIATE: 2.0, ONSLAUGHT: 3.0}
        # Phase-continuation: seed carried Beast gauge (name == SimState field).
        if self.entry is not None:
            seed_entry_gauge(state, self.entry.gauge_map, wd.JOB_DATA.gauges)
        return state

    def prepull(self, state: SimState, params) -> None:
        # Tank: already on the boss at the pull (holds aggro), no run-in — the
        # in-fight loop starts at t=0 (MELEE_TANK engage_delay is 0.0). Surging
        # Tempest is NOT pre-applied (Storm's Eye is a combo finisher), so it
        # ramps over the first GCDs; the ceiling's full-coverage overlay accounts
        # for that asymmetry in the safe direction.
        state.t = wd.JOB_DATA.role_policy.engage_delay_s

    def pick_gcd(self, state: SimState, params) -> int:
        """Greedy GCD pick, target-aware (AoE combo / spenders substituted where
        they win; N==1 -> the ST pick, byte-identical)."""
        return self._maybe_aoe(state, self._pick_gcd_st(state, params))

    def _pick_gcd_st(self, state: SimState, params) -> int:
        t = state.t

        # 1. Inner Release: spend the free guaranteed-crit-DH Fell Cleaves. If
        #    Nascent Chaos is up, spend it as Inner Chaos first (innately crit-DH;
        #    costs 50 gauge — NC always rides a fresh Infuriate's +50, so gauge is
        #    on hand — and does NOT consume an IR stack), else a free Fell Cleave.
        if state.inner_release > 0:
            if state.nascent_chaos and state.beast >= 50:
                return INNER_CHAOS
            return FELL_CLEAVE

        # 2. Primal combo follow-ups from the Inner Release burst (highest value).
        if state.primal_ruination_ready:
            return PRIMAL_RUINATION
        if state.primal_rend_ready:
            return PRIMAL_REND

        # 3. Don't waste a Nascent Chaos proc: spend gauge into Inner Chaos.
        if state.nascent_chaos and state.beast >= 50:
            return INNER_CHAOS

        # 4. Advance / finish the main combo.
        if state.combo_step == 1:
            return MAIM
        if state.combo_step == 2:
            # Refresh Surging Tempest with Storm's Eye when it's about to drop;
            # otherwise Storm's Path for the extra gauge (and self-heal).
            if state.surging_end - t <= SURGING_REFRESH_AT_S:
                return STORMS_EYE
            return STORMS_PATH

        # combo_step == 0 (between combos).
        # 5. Dump gauge into Fell Cleave to avoid overcap. With hold_gauge_for_burst
        #    we only spend when near the cap (banking the rest for Inner Release);
        #    otherwise spend as soon as 50 is available.
        spend_floor = (wd.BEAST_CAP - 20) if params.hold_gauge_for_burst else 50
        if state.beast >= spend_floor:
            return FELL_CLEAVE

        # 6. Start a new combo.
        return HEAVY_SWING

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Inner Release — open the burst window (the alignment anchor).
        if state.cd_ready.get(INNER_RELEASE, 0) <= t \
                and not is_forbidden(INNER_RELEASE, t, fw):
            return INNER_RELEASE

        # Primal Wrath — the granted finisher (after the 3 Inner Release
        # weaponskills), fire ASAP.
        if state.wrathful:
            return PRIMAL_WRATH

        # Infuriate — +50 gauge + Nascent Chaos (-> Inner Chaos). Use a charge
        # when it won't overcap the gauge, so the granted Inner Chaos lands.
        if state.charges.get(INFURIATE, 0) >= 1 and state.beast <= 50 \
                and not state.nascent_chaos and not is_forbidden(INFURIATE, t, fw):
            return INFURIATE

        # Upheaval — direct-damage oGCD (Orogeny, its AoE form, at high N).
        if state.cd_ready.get(UPHEAVAL, 0) <= t:
            n = self._n(t)
            if n >= 2 and potency_for(OROGENY, n, wd.JOB_DATA) > potency_for(
                    UPHEAVAL, n, wd.JOB_DATA):
                return OROGENY
            return UPHEAVAL

        # Onslaught — gap-closer used as a low-priority damage oGCD on cooldown.
        if state.charges.get(ONSLAUGHT, 0) >= 1:
            return ONSLAUGHT

        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Beast Gauge (generators / spenders). Inner Release frees the Beast cost
        # of Fell Cleave / Decimate ONLY, and only those consume an IR stack. Its
        # Nascent Chaos upgrades — Inner Chaos / Chaotic Cyclone — ALWAYS cost 50
        # gauge in-game and do NOT ride an IR stack (IR applies to Fell Cleave), so
        # they fall through to the generic spender subtraction below.
        free_ir_weaponskill = (
            ability_id in (FELL_CLEAVE, DECIMATE)
            and state.inner_release > 0
        )
        if ability_id in wd.BEAST_GENERATORS:
            state.beast = min(wd.BEAST_CAP,
                              state.beast + wd.BEAST_GENERATORS[ability_id])
        if ability_id in wd.BEAST_SPENDERS and not free_ir_weaponskill:
            state.beast = max(0, state.beast - wd.BEAST_SPENDERS[ability_id])

        # Cooldown / charges (generic).
        apply_cooldown(state, self.cooldowns, ability_id)
        # Orogeny (the AoE Upheaval) SHARES Upheaval's recast; under its own id the
        # generic apply_cooldown is a no-op (only Upheaval is in COOLDOWNS), so set
        # the shared timer explicitly — otherwise the picker (which gates on
        # Upheaval's cd_ready) re-fires Orogeny on every weave slot.
        if ability_id == OROGENY:
            apply_cooldown(state, self.cooldowns, UPHEAVAL)

        # Infuriate cooldown reduction: every Beast-gauge weaponskill (free or
        # paid) cuts Infuriate's recast by 5s. In the engine's fractional-charge
        # model that's +(5 / recast) of a charge — the load-bearing mechanic that
        # lets the rotation fire far more Inner Chaos than the bare 2-charge rate.
        if ability_id in (FELL_CLEAVE, INNER_CHAOS, DECIMATE, CHAOTIC_CYCLONE):
            recast = self.cooldowns[INFURIATE][0]
            _, max_ch = self.cooldowns[INFURIATE]
            state.charges[INFURIATE] = min(
                float(max_ch),
                state.charges.get(INFURIATE, float(max_ch))
                + wd.INFURIATE_CDR_S / recast)

        # Inner Release stack consumption: the free weaponskills tick it down;
        # the 3rd (hitting 0) grants Wrathful -> Primal Wrath.
        if free_ir_weaponskill:
            state.inner_release -= 1
            if state.inner_release == 0:
                state.wrathful = True

        # Per-ability state transitions.
        if ability_id == HEAVY_SWING:
            state.combo_step = 1
            state.aoe_combo_step = 0
        elif ability_id == MAIM:
            state.combo_step = 2 if state.combo_step == 1 else 0
            state.aoe_combo_step = 0
        elif ability_id == OVERPOWER:
            state.combo_step = 0
            state.aoe_combo_step = 1
        elif ability_id in (STORMS_PATH, STORMS_EYE, MYTHRIL_TEMPEST):
            state.combo_step = 0
            state.aoe_combo_step = 0
            if ability_id in (STORMS_EYE, MYTHRIL_TEMPEST):
                # Apply / extend Surging Tempest (30s, bankable to 60s). Mythril
                # Tempest is the AoE combo finisher that maintains the buff.
                base = max(t, state.surging_end)
                state.surging_end = min(t + wd.SURGING_TEMPEST_MAX_S,
                                        base + wd.SURGING_TEMPEST_DURATION_S)
        elif ability_id in (INNER_CHAOS, CHAOTIC_CYCLONE):
            state.nascent_chaos = False
        elif ability_id == INNER_RELEASE:
            state.inner_release = wd.INNER_RELEASE_STACKS
            state.primal_rend_ready = True
            state.wrathful = False
            # Inner Release extends Surging Tempest (capped at 60s).
            if state.surging_end > t:
                state.surging_end = min(t + wd.SURGING_TEMPEST_MAX_S,
                                        state.surging_end + wd.INNER_RELEASE_EXTEND_S)
        elif ability_id == INFURIATE:
            state.nascent_chaos = True
        elif ability_id == PRIMAL_REND:
            state.primal_rend_ready = False
            state.primal_ruination_ready = True
        elif ability_id == PRIMAL_RUINATION:
            state.primal_ruination_ready = False
        elif ability_id == PRIMAL_WRATH:
            state.wrathful = False

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            for hg in _SWEEP_HOLD_GAUGE:
                yield SimParams(max_weaves_per_gcd=mw,
                                hold_gauge_for_burst=hg,
                                forbidden_windows=extra_forbidden)


_MODEL = WarriorRotationModel()


def _model_for(sim_context):
    """Model bound to this pull's context: a per-player effective GCD (CeilingContext)
    and/or phase-continuation entry state. The shared cold-start singleton when neither
    (byte-identical)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    if gcd is None and entry is None and not mt_schedule:
        return _MODEL
    return WarriorRotationModel(entry=entry, gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn bound to a multi-target N(t) `schedule`
    (each cast valued per-target via `aoe_potency.potency_for`). Surging Tempest is
    a constant x1.10 on every candidate (folded into score_delivered_potency);
    buff-aware when given raid `buff_intervals`. Guaranteed crit-DH is derived from
    the timeline's own Inner Release / Inner Chaos casts. Empty schedule -> single
    target, byte-identical."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.warrior.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — the tuple shape
    mirrors the other sims (whose 2nd element is an opaque scalar); WAR has none."""
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
