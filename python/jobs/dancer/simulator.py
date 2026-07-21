"""Idealized DNC rotation — the Dancer `RotationModel` for the shared engine.

The first **physical-ranged proc job**. The time loop, downtime/weave/charge
handling, parameter sweep, local-search refinement and canonical buff alignment
all live in `jobs/_core/sim/engine.py`. This module supplies only the DNC-specific
rotation: the Cascade/Fountain combo, the budgeted proc/feather/esprit economy,
the Standard/Technical step dances, the burst enablers (Flourish / Devilment) and
the burst GCDs (Tillana / Last Dance / Starfall / Dance of the Dawn). The four
`simulate_*` shims at the bottom bind the model to the engine (kept under their
original names so the sidecar, the scorer and the tests call them unchanged).

DNC-specific rotation encoded:
- **All-instant GCDs** (`InstantGCD`, physical-ranged) at the 2.5s global, with
  the dance steps overlaid at a faster ~1.0s step-GCD (a `gcd_duration` override,
  the RPR Enshroud-Reaping analog).
- **Budgeted RNG/external resources** (the RDM proc trick generalized). The model
  is parameterized by three measured counts (`sim_context`): the player's proc
  spells (Reverse Cascade + Fountainfall), feather spends (Fan Dance) and esprit
  spends (Saber Dance + Dance of the Dawn). It spends *exactly* those counts, so
  the ceiling tracks the player's own luck/party-feed and only *misuse* costs
  efficiency. Procs are paced linearly in time (so the opener can't open on a
  proc), with a buff-aware override (spend ASAP inside a raid window).
- **Buff-aware banking** (the shared `engine.reachable_richer_window` /
  `in_top_window` primitives — the MCH `_queen_should_bank` pattern). When the
  picker sees `state.buff_intervals`, it holds esprit spends toward a reachable
  higher-multiplier window instead of dumping them flat. Gated behind
  `if state.buff_intervals:` so the buff-agnostic ceiling is byte-identical.
- **Burst** — Technical Step (the 2-min) + Devilment + Flourish are the alignment
  anchors the refinement nudges into the raid windows.

Out of scope for v1 (documented, intentionally not modeled):
- The AoE line (Windmill / Bladeshower / Bloodshower, Fan Dance II, AoE finishes).
- Exact pre-pull dance timing (the opener pre-builds Standard Finish during the
  countdown); the sim primes Last Dance Ready from it but doesn't credit a
  pre-pull damage cast (keeps the ceiling honest). Refine live.
- Frame-perfect step/cast timing; proc *timing* divergence from the player.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.dancer import data as dd


# --- Ability IDs (aliased from data for readability) ----------------------
CASCADE          = dd.CASCADE
FOUNTAIN         = dd.FOUNTAIN
REVERSE_CASCADE  = dd.REVERSE_CASCADE
FOUNTAINFALL     = dd.FOUNTAINFALL
SABER_DANCE      = dd.SABER_DANCE
DANCE_OF_THE_DAWN = dd.DANCE_OF_THE_DAWN
STANDARD_STEP    = dd.STANDARD_STEP
TECHNICAL_STEP   = dd.TECHNICAL_STEP
STEP             = dd.EMBOITE              # generic step action (cosmetic; 0p)
STANDARD_FINISH  = dd.STANDARD_FINISH
TECHNICAL_FINISH = dd.TECHNICAL_FINISH
FINISHING_MOVE   = dd.FINISHING_MOVE
_STANDARD_STEP_RECAST_S = dd.COOLDOWNS[dd.STANDARD_STEP][0]
TILLANA          = dd.TILLANA
LAST_DANCE       = dd.LAST_DANCE
STARFALL_DANCE   = dd.STARFALL_DANCE
FAN_DANCE        = dd.FAN_DANCE
FAN_DANCE_III    = dd.FAN_DANCE_III
FAN_DANCE_IV     = dd.FAN_DANCE_IV
FLOURISH         = dd.FLOURISH
DEVILMENT        = dd.DEVILMENT
# AoE line (cast in multi-target windows; gauge-equivalent to the ST combo/procs).
WINDMILL         = dd.WINDMILL
BLADESHOWER      = dd.BLADESHOWER
RISING_WINDMILL  = dd.RISING_WINDMILL
BLOODSHOWER      = dd.BLOODSHOWER
FAN_DANCE_II     = dd.FAN_DANCE_II

# Single-target -> AoE-counterpart swaps for the combo + procs (same esprit /
# feathers / Silken procs, so the per-slot choice is closed-form). The cleaving
# burst (Saber Dance / Tillana / Dawn / Fan Dance IV / Technical Finish / Starfall)
# is NOT swapped — it cleaves inherently and scales via AOE_POTENCIES.
_AOE_GCD_SWAP: dict[int, int] = {
    CASCADE:         WINDMILL,
    FOUNTAIN:        BLADESHOWER,
    REVERSE_CASCADE: RISING_WINDMILL,
    FOUNTAINFALL:    BLOODSHOWER,
}


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S = 2.50            # DNC physical-ranged global (no SkS adjustment in v1)
STANDARD_STEPS = 2           # step actions before Standard Finish
TECHNICAL_STEPS = 4          # step actions before Technical Finish
# How far ahead a richer raid window may be before the picker banks an esprit
# spend toward it (the MCH `_queen_should_bank` reach budget; bounded so a spend
# is never stranded waiting for a window that's too far out).
_BANK_LEAD_S = 20.0
# Fallback budgets when no sim_context is supplied (≈ steady-state rates). The
# real values are the player's measured counts, threaded as sim_context; these
# only bite on a direct sim call without one.
_DEFAULT_PROC_RATE_S = 5.0
_DEFAULT_FEATHER_RATE_S = 12.0
_DEFAULT_SABER_RATE_S = 9.0


@dataclass(frozen=True)
class DancerCtx:
    """Per-pull context threaded as `sim_context` (hashable -> joins the perfect-sim
    cache key). The three budgets are the player's MEASURED counts, so the ceiling
    spends the same number of luck/party-fed resources; `opener_start_s` is the
    phase-continuation opener start (None on a fresh pull)."""
    proc_budget: int = 0          # Reverse Cascade + Fountainfall
    feather_budget: int = 0       # Fan Dance (feather spends)
    saber_budget: int = 0         # Saber Dance + Dance of the Dawn (esprit spends)
    opener_start_s: float | None = None

    def __bool__(self) -> bool:
        return bool(self.proc_budget or self.feather_budget or self.saber_budget
                    or self.opener_start_s is not None)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """DNC picker tunables. v1 adds no axis beyond the shared knobs
    (max_weaves_per_gcd / triple_weave_clip_s / forbidden_windows); the budgets
    are per-pull model parameters (sim_context), not sweep axes."""
    pass


@dataclass
class SimState(SimStateBase):
    # Combo: 0 expects Cascade, 1 expects Fountain.
    combo_step: int = 0
    # Proc availability (set by the combo / Flourish; consumed by the proc GCDs).
    silken_symmetry: bool = False   # -> Reverse Cascade
    silken_flow: bool = False       # -> Fountainfall
    threefold: bool = False         # -> Fan Dance III
    fourfold: bool = False          # -> Fan Dance IV
    # Burst follow-ups (granted by the finishes / Devilment).
    flourishing_finish: bool = False  # -> Tillana (from Technical Finish)
    last_dance_ready: bool = False    # -> Last Dance (from Standard/Technical Finish)
    starfall_ready: bool = False      # -> Starfall Dance (from Devilment)
    dawn_ready: bool = False          # -> Dance of the Dawn (from Devilment)
    finishing_move_ready: bool = False  # Standard Step -> Finishing Move (from Flourish)
    # Step dances.
    in_dance: int = 0                 # 0 none, else the dance's Finish id
    steps_remaining: int = 0
    # Budgets remaining (matched to the player's counts).
    procs_remaining: int = 0
    feathers_remaining: int = 0
    sabers_remaining: int = 0


# --- Refinement / canonical anchors ---------------------------------------
# The greedy picker fires burst as soon as it's available; the refinement nudges
# the 2-minute enablers into the buff windows.
_PERFECT_ANCHORS: tuple[int, ...] = (TECHNICAL_STEP, DEVILMENT, FLOURISH)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (TECHNICAL_STEP, DEVILMENT)

# Sweep axes (kept job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)


# --- The DNC rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)


class DancerRotationModel(engine.BaseRotationModel):
    cooldowns = dd.COOLDOWNS
    timing = InstantGCD(base_s=GCD_BASE_S)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, ctx: DancerCtx | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()) -> None:
        self.ctx = ctx or DancerCtx()
        self.gcd_base_s = GCD_BASE_S if gcd_base_s is None else gcd_base_s
        # The dance GCD is a fixed ~1.0s special cooldown — it does NOT scale with
        # Skill Speed (unlike the global), so it stays constant across gear.
        self.step_recast_s = dd.STEP_RECAST_S
        if self.gcd_base_s != GCD_BASE_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)
        # Multi-target N(t) schedule: combo/procs swap to their AoE form, the burst
        # cleaves via AOE_POTENCIES. Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_aoe(self, state: "SimState", gcd: int) -> int:
        """Swap the AoE counterpart of a combo/proc GCD when it out-potencies at the
        target count (gauge-equivalent). N<2 -> unchanged (byte-identical)."""
        n = self._n(state.t)
        if n < 2:
            return gcd
        alt = _AOE_GCD_SWAP.get(gcd)
        if alt is not None and potency_for(alt, n, dd.JOB_DATA) > potency_for(
                gcd, n, dd.JOB_DATA):
            return alt
        return gcd

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {
            STANDARD_STEP:  0.0,
            TECHNICAL_STEP: 0.0,
            FLOURISH:       0.0,
            DEVILMENT:      0.0,
        }
        state.procs_remaining = self.ctx.proc_budget
        state.feathers_remaining = self.ctx.feather_budget
        state.sabers_remaining = self.ctx.saber_budget
        return state

    def prepull(self, state: SimState, params) -> None:
        # The pre-pull Standard Step -> Standard Finish completes AT the pull (t=0):
        # a free 850 GCD (the steps were danced during the countdown) that, crucially,
        # opens the Standard Finish self-buff window from t=0 — so the idealized
        # opener burst (Technical + Devilment) is buffed exactly as a real DNC's is.
        # It primes Last Dance Ready and consumes the Standard Step cooldown (the next
        # Standard Step is 30s out). Emitted at t=0 so it's scored symmetrically with
        # the player's pre-pull finish (which lands at t~=0 in their cast stream).
        state.timeline.append((0.0, STANDARD_FINISH))
        state.last_dance_ready = True
        state.cd_ready[STANDARD_STEP] = _STANDARD_STEP_RECAST_S
        if self.ctx.opener_start_s is not None:
            state.t = max(0.0, self.ctx.opener_start_s)

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # The whole dance — the Standard/Technical Step initiator AND the step
        # actions — runs at the faster ~1.0s dance GCD (pressing Step immediately
        # enters 1.0s dance mode); the Finishes and everything else are the normal
        # global. Keyed on the picked GCD so the beam / exact paths (which call
        # gcd_duration directly) get identical timing.
        if gcd_id in dd.STEP_IDS or gcd_id in (STANDARD_STEP, TECHNICAL_STEP):
            return self.step_recast_s
        return self.gcd_base_s

    # --- buff-aware spend gate (shared primitives) -------------------------
    def _due(self, total: int, state: SimState) -> float:
        d = state.fight_duration_s
        return total * state.t / d if d > 0 else 0.0

    def _spend_now(self, state: SimState, used: int, total: int,
                   *, use_bank: bool) -> bool:
        """RDM-style linear pacing (spend only when behind the time schedule, so the
        opener can't front-load luck), with the buff-aware overrides: always spend
        inside a top raid window, and — for the big esprit spend — BANK toward a
        reachable richer window instead of dumping flat (the MCH `_queen_should_bank`
        reuse). Agnostic path (no buff_intervals) = the plain RDM pacing."""
        if total <= 0:
            return False
        if used < self._due(total, state):
            return True                       # behind schedule -> must spend
        bi = state.buff_intervals
        if not bi:
            return False                      # ahead, agnostic -> hold (spread)
        if engine.in_top_window(state.t, bi):
            return True                       # in the burst -> dump
        if use_bank and engine.reachable_richer_window(
                state.t, bi, _BANK_LEAD_S) is not None:
            return False                      # bank toward the reachable window
        return True                           # ahead, no richer window -> spend

    def pick_gcd(self, state: SimState, params) -> int:
        """Greedy GCD pick, target-aware (combo/procs swap to their AoE form where
        they win; the burst cleaves via AOE_POTENCIES). N==1 -> ST, byte-identical."""
        return self._maybe_aoe(state, self._pick_gcd_st(state, params))

    def _pick_gcd_st(self, state: SimState, params) -> int:
        t = state.t
        fw = params.forbidden_windows

        # 1. Finish / continue an in-progress step dance (forced).
        if state.in_dance:
            if state.steps_remaining > 0:
                return STEP
            return state.in_dance             # the dance's Finish id

        # 2. Start a step dance on cooldown (the 2-min Technical first). On the
        #    Standard Step cooldown, fire Finishing Move instead when Flourish has
        #    granted it (a single 850 GCD, no step actions — strictly better, and
        #    what top DNCs do every other 30s window).
        if (state.cd_ready.get(TECHNICAL_STEP, 0) <= t
                and not is_forbidden(TECHNICAL_STEP, t, fw)):
            return TECHNICAL_STEP
        if (state.cd_ready.get(STANDARD_STEP, 0) <= t
                and not is_forbidden(STANDARD_STEP, t, fw)):
            return FINISHING_MOVE if state.finishing_move_ready else STANDARD_STEP

        # 3. Burst / use-or-lose GCD procs that must not be wasted (Starfall and
        #    Last Dance are single-charge, overwritten if a later finish re-grants
        #    them — fire them before the budgeted filler so none are lost).
        if state.flourishing_finish:
            return TILLANA
        if state.dawn_ready and state.sabers_remaining > 0:
            return DANCE_OF_THE_DAWN
        if state.starfall_ready:
            return STARFALL_DANCE
        if state.last_dance_ready:
            return LAST_DANCE

        # 4. Proc GCDs (budgeted, linearly paced; ASAP in a buff window).
        procs_used = self.ctx.proc_budget - state.procs_remaining
        if state.procs_remaining > 0 and self._spend_now(
                state, procs_used, self.ctx.proc_budget, use_bank=False):
            if state.silken_flow:
                return FOUNTAINFALL
            if state.silken_symmetry:
                return REVERSE_CASCADE

        # 5. Saber Dance (esprit) — budgeted, buff-aware banked.
        sabers_used = self.ctx.saber_budget - state.sabers_remaining
        if state.sabers_remaining > 0 and self._spend_now(
                state, sabers_used, self.ctx.saber_budget, use_bank=True):
            return SABER_DANCE

        # 6. Basic combo filler.
        return FOUNTAIN if state.combo_step == 1 else CASCADE

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Devilment — crit/DH burst window (alignment anchor).
        if (state.cd_ready.get(DEVILMENT, 0) <= t
                and not is_forbidden(DEVILMENT, t, fw)):
            return DEVILMENT
        # Flourish — free Fan Dance III + IV + Silken procs (alignment anchor).
        if (state.cd_ready.get(FLOURISH, 0) <= t
                and not is_forbidden(FLOURISH, t, fw)):
            return FLOURISH
        # Proc Fan Dances (free, fire ASAP).
        if state.fourfold:
            return FAN_DANCE_IV
        if state.threefold:
            return FAN_DANCE_III
        # Fan Dance — spend a feather (budgeted, linearly paced; ASAP in a window).
        feathers_used = self.ctx.feather_budget - state.feathers_remaining
        if state.feathers_remaining > 0 and self._spend_now(
                state, feathers_used, self.ctx.feather_budget, use_bank=True):
            n = self._n(t)
            if n >= 2 and potency_for(FAN_DANCE_II, n, dd.JOB_DATA) > potency_for(
                    FAN_DANCE, n, dd.JOB_DATA):
                return FAN_DANCE_II       # AoE feather spender
            return FAN_DANCE
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Generic cooldown / charges (Standard/Technical Step, Flourish, Devilment).
        apply_cooldown(state, self.cooldowns, ability_id)

        # --- Combo + proc generation ---
        if ability_id in (CASCADE, WINDMILL):
            state.combo_step = 1
            state.silken_symmetry = True       # 50% in game; budget caps the spend
        elif ability_id in (FOUNTAIN, BLADESHOWER):
            state.combo_step = 0
            state.silken_flow = True
        elif ability_id in (REVERSE_CASCADE, RISING_WINDMILL):
            state.silken_symmetry = False
            state.procs_remaining = max(0, state.procs_remaining - 1)
        elif ability_id in (FOUNTAINFALL, BLOODSHOWER):
            state.silken_flow = False
            state.procs_remaining = max(0, state.procs_remaining - 1)

        # --- Esprit spenders ---
        elif ability_id == SABER_DANCE:
            state.sabers_remaining = max(0, state.sabers_remaining - 1)
        elif ability_id == DANCE_OF_THE_DAWN:
            state.sabers_remaining = max(0, state.sabers_remaining - 1)
            state.dawn_ready = False

        # --- Step dances ---
        elif ability_id == STANDARD_STEP:
            state.in_dance = STANDARD_FINISH
            state.steps_remaining = STANDARD_STEPS
        elif ability_id == TECHNICAL_STEP:
            state.in_dance = TECHNICAL_FINISH
            state.steps_remaining = TECHNICAL_STEPS
        elif ability_id == STEP:
            state.steps_remaining = max(0, state.steps_remaining - 1)
        elif ability_id == FINISHING_MOVE:
            # Replaces Standard Step: a single GCD (no dance), same Standard Finish
            # buff + Last Dance Ready, and it consumes the shared Standard Step cd.
            state.finishing_move_ready = False
            state.last_dance_ready = True
            state.cd_ready[STANDARD_STEP] = t + _STANDARD_STEP_RECAST_S
        elif ability_id == STANDARD_FINISH:
            state.in_dance = 0
            state.last_dance_ready = True
        elif ability_id == TECHNICAL_FINISH:
            state.in_dance = 0
            state.flourishing_finish = True
            state.last_dance_ready = True

        # --- Burst follow-ups ---
        elif ability_id == TILLANA:
            state.flourishing_finish = False
        elif ability_id == LAST_DANCE:
            state.last_dance_ready = False
        elif ability_id == STARFALL_DANCE:
            state.starfall_ready = False
        elif ability_id in (FAN_DANCE, FAN_DANCE_II):
            state.feathers_remaining = max(0, state.feathers_remaining - 1)
            state.threefold = True             # 50% in game; modeled deterministically
        elif ability_id == FAN_DANCE_III:
            state.threefold = False
        elif ability_id == FAN_DANCE_IV:
            state.fourfold = False

        # --- Burst enablers ---
        elif ability_id == FLOURISH:
            state.threefold = True
            state.fourfold = True
            state.silken_symmetry = True
            state.silken_flow = True
            state.finishing_move_ready = True
        elif ability_id == DEVILMENT:
            state.starfall_ready = True
            state.dawn_ready = True

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction ----------------------------------------------------

def _default_ctx(duration_s: float) -> DancerCtx:
    return DancerCtx(
        proc_budget=max(0, int(duration_s / _DEFAULT_PROC_RATE_S)),
        feather_budget=max(0, int(duration_s / _DEFAULT_FEATHER_RATE_S)),
        saber_budget=max(0, int(duration_s / _DEFAULT_SABER_RATE_S)),
    )


def _model_for(duration_s: float, sim_context) -> DancerRotationModel:
    """Build a model bound to this run's per-pull context. After unwrapping any
    per-player effective GCD (CeilingContext), the payload is the DancerCtx of
    measured budgets; falls back to duration estimates when absent."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    ctx = payload if isinstance(payload, DancerCtx) else _default_ctx(duration_s)
    return DancerRotationModel(ctx=ctx, gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn bound to a multi-target N(t) `schedule`
    (each cast valued per-target via `aoe_potency.potency_for` — the cleaving burst
    scales automatically). Buff-aware when given; `aux` is unused (no pet). Empty
    schedule -> single target, byte-identical. Lazy scoring import avoids a
    scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.dancer.scoring import score_delivered_potency
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
    """Run the idealized rotation once. Returns (timeline, 0) — DNC has no pet/
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
    """The GCD-perfect ceiling: sweep + local-search burst-timing refinement,
    routed through `beam_perfect` (width 1 -> identical to `perfect` until DNC
    opts into GCD forks via `gcd_candidates`). Buff-aware when `buff_intervals`
    is given."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.beam_perfect(model, _make_score(model.mt_schedule),
                               fight_duration_s, downtime_windows or [],
                               buff_intervals)


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
