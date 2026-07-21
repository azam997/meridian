"""Idealized AST rotation — the Astrologian `RotationModel` for the shared engine.

The analyzer's second **healer** simulator (after WHM), and its simplest. The
time loop, downtime/weave handling, sweep, refinement and canonical alignment
all live in `jobs/_core/sim/engine.py`; this module supplies only the AST-specific
rotation:

- **Filler** — hardcast Fall Malefic (1.5s cast < 2.5s recast, so the slot is
  always recast-bound; the cast only costs a weave slot). Combust III is refreshed
  on a ~30s cadence (scored per cast by time-to-next, so clipping self-penalizes).
  There is no Misery-analog damage GCD — the GCD line is nearly pure filler.
- **The oGCD economy** — Divination (the 2-minute party-buff anchor, also
  unlocking one Oracle), Oracle (the burst payoff), Earthly Star (a placed 60s
  oGCD, its Stellar Explosion collapsed to one damage cast for v1), and Lord of
  Crowns (drawn by Minor Arcana). Oracle and Lord ride state flags, not cooldowns.

There is **no GCD fork** (no banking resource, no hold-vs-fire choice), so the
ceiling routes through `engine.perfect` + `canonical_aligned_max_guard` (the RDM
pattern) — NOT the beam. `demonstrated_cadence_anchor` (scoring.py) handles the
flat-GCD sub-band; `canonical_aligned_max_guard` handles oGCD-burst alignment in
the strict scenario. Both can only RAISE the ceiling, so the <=100% guard holds.

The healer mit-plan lock hooks are inherited unchanged from `BaseRotationModel`:
AST's locked heal (Helios Conjunction) is always castable and the damage line
never voluntarily heals, so the base identity `resolve_locked_gcd` /
`lock_satisfiers` / `on_downtime_window` is correct.

Out of scope for v1 (documented, intentionally not modeled): AoE beyond
free-splash; MP; the Earthly Star place/detonate two-step (collapsed to one 60s
damage oGCD); self-cards (AST gives damage cards to DPS, not itself);
phase-continuation carryover (Combust remaining / drawn cards — `AstContext` is a
stubbed empty payload for v1).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.astrologian import data as ad


# --- Ability IDs (aliased from data for readability) ------------------------
FALL_MALEFIC   = ad.FALL_MALEFIC
COMBUST_III    = ad.COMBUST_III
GRAVITY_II     = ad.GRAVITY_II
DIVINATION     = ad.DIVINATION
ORACLE         = ad.ORACLE
EARTHLY_STAR   = ad.EARTHLY_STAR
LORD_OF_CROWNS = ad.LORD_OF_CROWNS

AST_GCD_S: float = ad.AST_GCD_S


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """AST picker tunables. No axis beyond the shared knobs — the kit weaves a
    handful of oGCDs with no strategic GCD choice, so the weave sweep is a single
    point."""
    pass


@dataclass
class SimState(SimStateBase):
    combust_end: float = 0.0        # current Combust III application expires here
    divining_ready: bool = False    # set by Divination -> unlocks one Oracle


@dataclass(frozen=True)
class AstContext:
    """Per-pull phase-continuation entry state (M12S-P2 style). Stubbed empty for
    v1 — AST has no offensive gauge, and Combust-remaining / drawn-card carryover
    are deferred calibration levers. Falsy => cold start => byte-identical."""
    entry_combust_remaining_s: float = 0.0

    def __bool__(self) -> bool:
        return self.entry_combust_remaining_s > 0.0


# --- Refinement / canonical anchors -----------------------------------------
# Divination is the lone 2-minute burst anchor; the refinement nudges it into the
# raid-buff windows. Oracle rides it (fired the weave after Divining is granted),
# so aligning Divination aligns the burst.
_ANCHORS: tuple[int, ...] = (DIVINATION,)

# Sweep axes: the kit weaves at most Divination + Oracle + Star + a draw + the pot
# around one burst GCD, so a 2-weave budget is never binding — a single point.
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2,)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    ad.JOB_DATA.tincture_main_stat, ad.JOB_DATA.tincture_role_coeff)


class AstrologianRotationModel(engine.BaseRotationModel):
    cooldowns = ad.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=AST_GCD_S, cast_times=ad.CAST_TIMES)
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
        # Multi-target N(t) schedule: where N makes Gravity II out-potency Fall
        # Malefic the filler swaps. Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed (threaded only when faster than the constant):
        # SpS scales BOTH the GCD recast AND cast times by the same haste factor.
        # None keeps the tier constant, byte-identical. AST has no recast-haste
        # self-buff (Lightspeed is cast-time only), so the GCD stays flat.
        if gcd_base_s is not None:
            from dataclasses import replace
            factor = gcd_base_s / AST_GCD_S
            self.timing = replace(
                AstrologianRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in ad.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_filler(self, state: SimState) -> int:
        """Gravity II replaces the Fall Malefic filler when it out-potencies it at
        the live target count. N<2 (or no schedule) -> Fall Malefic, byte-identical."""
        n = self._n(state.t)
        if n >= 2 and potency_for(GRAVITY_II, n, ad.JOB_DATA) > potency_for(
                FALL_MALEFIC, n, ad.JOB_DATA):
            return GRAVITY_II
        return FALL_MALEFIC

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {DIVINATION: 0.0, EARTHLY_STAR: 0.0, LORD_OF_CROWNS: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull channel: hardcast Fall Malefic during the countdown so it
        # resolves at the pull (t=0). Begincast-anchored at -cast_time (matching
        # the player's own precast in norm_casts); the recast rolls from the
        # begincast, so the first in-fight GCD presses at (recast - cast_time).
        cast_s = self.timing._cast_time(FALL_MALEFIC)
        state.timeline.append((-cast_s, FALL_MALEFIC))
        state.t = self.timing.gcd_recast_s - cast_s

    # --- Pickers --------------------------------------------------------------

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        fw = params.forbidden_windows
        # 1. Combust upkeep — dropping the DoT is the one real throughput cliff.
        if state.combust_end - t <= self.gcd_duration(state, COMBUST_III, params) \
                and not is_forbidden(COMBUST_III, t, fw):
            return COMBUST_III
        # 2. Filler (Gravity II at high target counts, else Fall Malefic).
        return self._maybe_filler(state)

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        # 1. Oracle — the burst payoff, fired the weave after Divining is granted
        #    (so it lands inside Divination's party window that refine aligned).
        if state.divining_ready and not is_forbidden(ORACLE, t, fw):
            return ORACLE
        # 2. Divination — the 2-minute anchor (refine holds it to the raid window).
        if state.cd_ready.get(DIVINATION, 0) <= t and not is_forbidden(DIVINATION, t, fw):
            return DIVINATION
        # 3. Lord of Crowns — the drawn damage card. Modeled on a ~120s effective
        #    cadence (measured: ~1 per 2 draws on live top parses; the Minor Arcana
        #    60s-draw model over-produced it), fired directly.
        if state.cd_ready.get(LORD_OF_CROWNS, 0) <= t and not is_forbidden(LORD_OF_CROWNS, t, fw):
            return LORD_OF_CROWNS
        # 4. Earthly Star on its 60s cooldown (the Stellar Explosion damage).
        if state.cd_ready.get(EARTHLY_STAR, 0) <= t and not is_forbidden(EARTHLY_STAR, t, fw):
            return EARTHLY_STAR
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

        if ability_id == COMBUST_III:
            state.combust_end = t + ad.COMBUST_DOT_DURATION_S
        elif ability_id == DIVINATION:
            state.divining_ready = True
        elif ability_id == ORACLE:
            state.divining_ready = False

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _model_for(duration_s: float, sim_context) -> AstrologianRotationModel:
    """Build a model bound to this run's per-pull context. Canonical unwrap
    order: CeilingContext (per-player effective GCD) -> MultiTargetContext (N(t)
    schedule) -> HealLockContext (mit-plan locked heal GCDs) -> the
    phase-continuation entry state (`AstContext`, empty in v1); None = cold start."""
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
    # AstContext carries no offensive state in v1, so it needs no branch here —
    # the model ignores it. Kept in the unwrap chain for forward-compatibility.
    return AstrologianRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule,
                                    locked_gcd_windows=locks)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to
    a multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy scoring import avoids a scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.astrologian.scoring import score_delivered_potency
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
    """Run the idealized rotation once. Returns (timeline, 0) — AST has no pet/
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
    """Sweep + local-search refinement + the raid-window burst max-guard (the same
    GCD-perfect ceiling as `simulate_idealized_perfect` — AST has no beam fork)."""
    return simulate_idealized_perfect(fight_duration_s, downtime_windows,
                                      buff_intervals, sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: sweep + local-search refinement (buff-aware when given),
    then the shared raid-window burst max-guard (Divination + Oracle forced into
    the party window; max-guarded so it never regresses the refined greedy ceiling).
    No beam — AST has no strategic GCD fork."""
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
