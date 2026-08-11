"""Idealized SCH rotation — the Scholar `RotationModel` for the shared engine.

The analyzer's third **healer** simulator (after WHM + AST), and nearly as simple.
The time loop, downtime/weave handling, sweep, refinement and canonical alignment
all live in `jobs/_core/sim/engine.py`; this module supplies only the SCH-specific
rotation:

- **Filler** — hardcast Broil IV (1.5s cast < 2.5s recast, so the slot is always
  recast-bound; the cast only costs a weave slot). Biolysis is refreshed on a ~30s
  cadence (scored per cast by time-to-next, so clipping self-penalizes). Art of War
  replaces the filler in AoE windows. There is no Misery-analog damage GCD.
- **The oGCD economy** — Chain Stratagem (the 2-minute party-buff anchor, also
  unlocking one Baneful Impaction), Baneful Impaction (a short DoT oGCD, folded to
  one cast), and the **Aetherflow gauge** (3 stacks / 60s, refilled by Aetherflow,
  spent one-per-cast by Energy Drain). Baneful rides a state flag; Energy Drain
  rides the gauge; neither is cooldown-gated.

**Aetherflow is a plain SimState int** (`state.aetherflow`), not a GaugeModel —
Energy Drain is gated on `> 0` and Aetherflow refills to 3 when the counter is
empty. The ceiling therefore spends ALL Aetherflow on Energy Drain (the honest
damage max); a real SCH diverts some to oGCD heals (a documented lever, not tuned).

There is **no GCD fork** (no hold-vs-fire choice), so the ceiling routes through
`engine.perfect` + `canonical_aligned_max_guard` (the AST/RDM pattern) — NOT the
beam. `demonstrated_cadence_anchor` (scoring.py) handles the flat-GCD sub-band;
`canonical_aligned_max_guard` handles oGCD-burst alignment in the strict scenario.
Both can only RAISE the ceiling, so the <=100% guard holds.

The healer mit-plan lock hooks are inherited unchanged from `BaseRotationModel`:
SCH's locked heal (Concitation) is always castable and the damage line never
voluntarily heals, so the base identity `resolve_locked_gcd` / `lock_satisfiers` /
`on_downtime_window` is correct.

Out of scope for v1 (documented, intentionally not modeled): AoE beyond free-splash;
MP; the heal-only fairy (Eos/Selene — no pet damage folded, aux is always 0);
phase-continuation carryover (Biolysis remaining / Aetherflow stacks — `SchContext`
is a stubbed empty payload for v1).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.scholar import data as sd


# --- Ability IDs (aliased from data for readability) ------------------------
BROIL_IV          = sd.BROIL_IV
BIOLYSIS          = sd.BIOLYSIS
ART_OF_WAR        = sd.ART_OF_WAR
CHAIN_STRATAGEM   = sd.CHAIN_STRATAGEM
BANEFUL_IMPACTION = sd.BANEFUL_IMPACTION
ENERGY_DRAIN      = sd.ENERGY_DRAIN
AETHERFLOW        = sd.AETHERFLOW

SCH_GCD_S: float = sd.SCH_GCD_S


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """SCH picker tunables. No axis beyond the shared knobs — the kit weaves a
    handful of oGCDs with no strategic GCD choice, so the weave sweep is a single
    point."""
    pass


@dataclass
class SimState(SimStateBase):
    biolysis_end: float = 0.0       # current Biolysis application expires here
    baneful_ready: bool = False     # set by Chain Stratagem -> unlocks one Baneful Impaction
    aetherflow: int = 0             # Aetherflow stacks (refilled to 3, spent by Energy Drain)


@dataclass(frozen=True)
class SchContext:
    """Per-pull phase-continuation entry state (M12S-P2 style). Stubbed empty for
    v1 — Biolysis-remaining / Aetherflow-carryover are deferred calibration levers
    (marginal for a healer). Falsy => cold start => byte-identical."""
    entry_biolysis_remaining_s: float = 0.0
    entry_aetherflow: int = 0

    def __bool__(self) -> bool:
        return self.entry_biolysis_remaining_s > 0.0 or self.entry_aetherflow > 0


# --- Refinement / canonical anchors -----------------------------------------
# Chain Stratagem is the lone 2-minute burst anchor; the refinement nudges it into
# the raid-buff windows. Baneful Impaction rides it (fired the weave after Chain
# Stratagem grants the enabling stack), so aligning Chain Stratagem aligns the burst.
_ANCHORS: tuple[int, ...] = (CHAIN_STRATAGEM,)

# Sweep axes: the kit weaves at most Chain Stratagem + Baneful + Aetherflow + an
# Energy Drain + the pot around one burst GCD, so a 2-weave budget is never binding
# — a single point.
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2,)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)


class ScholarRotationModel(engine.BaseRotationModel):
    cooldowns = sd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=SCH_GCD_S, cast_times=sd.CAST_TIMES)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = (),
                 locked_gcd_windows: tuple = ()):
        # Mit-plan locked heal-GCD windows (jobs/_core/heal_locks). Empty ()
        # -> the engine's lock scheduler never runs, byte-identical.
        self.locked_gcd_windows = tuple(locked_gcd_windows)
        # Multi-target N(t) schedule: where N makes Art of War out-potency Broil IV
        # the filler swaps. Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed (threaded only when faster than the constant):
        # SpS scales BOTH the GCD recast AND cast times by the same haste factor.
        # None keeps the tier constant, byte-identical. SCH has no recast-haste
        # self-buff, so the GCD stays flat.
        if gcd_base_s is not None:
            from dataclasses import replace
            factor = gcd_base_s / SCH_GCD_S
            self.timing = replace(
                ScholarRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in sd.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_filler(self, state: SimState) -> int:
        """Art of War replaces the Broil IV filler when it out-potencies it at the
        live target count. N<2 (or no schedule) -> Broil IV, byte-identical."""
        n = self._n(state.t)
        if n >= 2 and potency_for(ART_OF_WAR, n, sd.JOB_DATA) > potency_for(
                BROIL_IV, n, sd.JOB_DATA):
            return ART_OF_WAR
        return BROIL_IV

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {CHAIN_STRATAGEM: 0.0, AETHERFLOW: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull channel: hardcast Broil IV during the countdown so it resolves at
        # the pull (t=0). Begincast-anchored at -cast_time (matching the player's own
        # precast in norm_casts); the recast rolls from the begincast, so the first
        # in-fight GCD presses at (recast - cast_time).
        cast_s = self.timing._cast_time(BROIL_IV)
        state.timeline.append((-cast_s, BROIL_IV))
        state.t = self.timing.gcd_recast_s - cast_s

    # --- Pickers --------------------------------------------------------------

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        fw = params.forbidden_windows
        # 1. Biolysis upkeep — dropping the DoT is the one real throughput cliff.
        if state.biolysis_end - t <= self.gcd_duration(state, BIOLYSIS, params) \
                and not is_forbidden(BIOLYSIS, t, fw):
            return BIOLYSIS
        # 2. Filler (Art of War at high target counts, else Broil IV).
        return self._maybe_filler(state)

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        # 1. Baneful Impaction — the burst payoff, fired the weave after Chain
        #    Stratagem grants the enabling stack (so it lands inside the party window
        #    that refine aligned).
        if state.baneful_ready and not is_forbidden(BANEFUL_IMPACTION, t, fw):
            return BANEFUL_IMPACTION
        # 2. Chain Stratagem — the 2-minute anchor (refine holds it to the raid window).
        if state.cd_ready.get(CHAIN_STRATAGEM, 0) <= t and not is_forbidden(CHAIN_STRATAGEM, t, fw):
            return CHAIN_STRATAGEM
        # 3. Aetherflow — refill the gauge, but only when empty (else stacks overcap).
        if state.aetherflow <= 0 and state.cd_ready.get(AETHERFLOW, 0) <= t \
                and not is_forbidden(AETHERFLOW, t, fw):
            return AETHERFLOW
        # 4. Energy Drain — spend an Aetherflow stack for damage.
        if state.aetherflow > 0 and not is_forbidden(ENERGY_DRAIN, t, fw):
            return ENERGY_DRAIN
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

        if ability_id == BIOLYSIS:
            state.biolysis_end = t + sd.BIOLYSIS_DOT_DURATION_S
        elif ability_id == CHAIN_STRATAGEM:
            state.baneful_ready = True
        elif ability_id == BANEFUL_IMPACTION:
            state.baneful_ready = False
        elif ability_id == AETHERFLOW:
            state.aetherflow = sd.AETHERFLOW_STACKS
        elif ability_id == ENERGY_DRAIN:
            state.aetherflow = max(0, state.aetherflow - 1)

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _model_for(duration_s: float, sim_context) -> ScholarRotationModel:
    """Build a model bound to this run's per-pull context. Canonical unwrap order:
    CeilingContext (per-player effective GCD) -> MultiTargetContext (N(t) schedule)
    -> HealLockContext (mit-plan locked heal GCDs) -> the phase-continuation entry
    state (`SchContext`, empty in v1); None = cold start."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    from jobs._core.heal_locks import HealLockContext
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    locks: tuple = ()
    if isinstance(payload, HealLockContext):
        locks = payload.locks
        payload = payload.inner
    # SchContext carries no offensive state in v1, so it needs no branch here — the
    # model ignores it. Kept in the unwrap chain for forward-compatibility.
    return ScholarRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule,
                                locked_gcd_windows=locks)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy scoring import avoids a scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.scholar.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests call `_score`).
_score = _make_score()


def _schedule_of(sim_context):
    """The multi-target N(t) schedule from this run's sim_context ('()' on a
    single-target pull)."""
    from jobs._core.downtime_sources import schedule_from_context
    return schedule_from_context(sim_context)


# --- Module-level entrypoints (bind the model to the shared engine) ----------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — SCH's fairy is
    heal-only, so there is no pet/payload scalar and aux is always 0."""
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
    """Sweep + local-search refinement + the raid-window burst max-guard (the same
    GCD-perfect ceiling as `simulate_idealized_perfect` — SCH has no beam fork)."""
    return simulate_idealized_perfect(fight_duration_s, downtime_windows,
                                      buff_intervals, sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: sweep + local-search refinement (buff-aware when given),
    then the shared raid-window burst max-guard (Chain Stratagem + Baneful forced
    into the party window; max-guarded so it never regresses the refined greedy
    ceiling). No beam — SCH has no strategic GCD fork."""
    dt = downtime_windows or []
    model = _model_for(fight_duration_s, sim_context)
    score = _make_score(_schedule_of(sim_context))
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
    return engine.canonical_aligned(model, _make_score(_schedule_of(sim_context)),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
