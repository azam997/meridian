"""Idealized WHM rotation — the White Mage `RotationModel` for the shared engine.

The first **healer** simulator. The time loop, downtime/weave handling, sweep,
refinement, beam search and canonical alignment all live in
`jobs/_core/sim/engine.py`; this module supplies only the WHM-specific rotation:

- **Filler** — hardcast Glare III (1.5 s cast < 2.5 s recast, so the slot is
  always recast-bound; the cast only costs a weave slot). Dia is refreshed on a
  30 s cadence (scored per cast by time-to-next, so clipping self-penalizes).
- **Presence of Mind** — the 2-minute anchor: 20% cast/recast haste for 15 s
  (modeled in `gcd_duration`, so the window genuinely fits more GCDs) plus 3
  Sacred Sight stacks, each converting a filler slot into an instant 640p
  Glare IV within 30 s.
- **The Lily economy** (the healer's dual-purpose mechanic). One Healing Lily
  accrues every 20 s in combat (cap 3; the timer keeps running while capped, so
  capped time wastes lilies). Spending one is an Afflatus Solace/Rapture — an
  instant 0-damage heal GCD that nourishes the Blood Lily; three nourishes
  bloom a 1,400p Afflatus Misery. Misery exactly replaces the four Glare III
  it displaces (4 × 350 = 1,400), so the line is potency-neutral in flat
  uptime — its value is (a) lily heals spent **during downtime** are free
  (`on_downtime_window`, what real WHMs do on every disconnect) and (b) Misery
  banked into raid-buff windows. Both decisions are real GCD forks, exposed to
  `beam_search` via `gcd_candidates` + `beam_signature`.

Out of scope for v1 (documented, intentionally not modeled):
- AoE (Holy III / Rapture-vs-Solace targeting): single-target sim; free-splash
  crediting handles confirmed multi-target windows.
- MP (never binds: lily heals are free, Assize/Lucid refund).
- Thin Air / Swiftcast as DPS (no potency effect on this kit).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.whitemage import data as wd


# --- Ability IDs (aliased from data for readability) ------------------------
GLARE_III  = wd.GLARE_III
GLARE_IV   = wd.GLARE_IV
DIA        = wd.DIA
ASSIZE     = wd.ASSIZE
POM        = wd.PRESENCE_OF_MIND
MISERY     = wd.AFFLATUS_MISERY
SOLACE     = wd.AFFLATUS_SOLACE
RAPTURE    = wd.AFFLATUS_RAPTURE
HOLY_III   = wd.HOLY_III   # AoE filler (multi-target only; gauge-free)

WHM_GCD_S: float = 2.5


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """WHM picker tunables. No axis beyond the shared knobs — the kit has only
    two weavable oGCDs, so even the weave sweep is a single point."""
    pass


@dataclass
class SimState(SimStateBase):
    lilies: int = 0
    blood: int = 0                  # Blood Lily nourishment (3 = Misery ready)
    next_lily_t: float = wd.LILY_INTERVAL_S
    sacred_sight: int = 0           # Glare IV stacks (from PoM)
    sacred_until: float = 0.0
    pom_until: float = 0.0          # haste window end
    dia_end: float = 0.0            # current Dia application expires here
    # Incremental running score (pot-aware, buff-agnostic) so `beam_prune` is
    # O(1) instead of a full timeline re-scan per beam expansion (the measured
    # beam bottleneck on SAM). `_score_flat` banks each cast's table potency ×
    # the pot multiplier at its snapshot; `_score_fin_dot` banks each FINALIZED
    # Dia application (credited by time-to-next when the refresh lands);
    # `_last_dia_t` is the trailing, not-yet-finalized application.
    _score_flat: float = 0.0
    _score_fin_dot: float = 0.0
    _last_dia_t: float | None = None


@dataclass(frozen=True)
class WhmContext:
    """Per-pull phase-continuation entry state (M12S-P2 style): the lily gauge
    the player carried into the logged phase. Falsy/None = cold start -> the
    sim stays byte-identical. Hashable -> joins the perfect-sim cache key."""
    entry_lilies: int = 0
    entry_blood: int = 0

    def __bool__(self) -> bool:
        return self.entry_lilies > 0 or self.entry_blood > 0


# --- Refinement / canonical anchors -----------------------------------------
# PoM is the lone 2-minute burst enabler; the refinement nudges it into the
# raid-buff windows. Misery/Glare IV alignment is a GCD fork, owned by the beam.
_ANCHORS: tuple[int, ...] = (POM,)

# Sweep axes: the kit weaves at most PoM + Assize + the pot around one burst
# GCD, so a 2-weave budget is never binding — a single sweep point.
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2,)

# Beam width for the GCD-perfect search. The fork set is small (hold-vs-fire
# Misery, Glare IV placement, lily-spend slots, Dia refresh timing), so a
# moderate width converges; raise only if live calibration finds a search gap.
_BEAM_WIDTH = 64

# Admissible pending-value credits for `beam_prune`: a lily line is behind a
# pure-Glare line by one filler per uptime spend until its Misery lands, so
# credit banked nourishment at the net Misery payoff per spend; an unspent
# Sacred Sight stack is a pending Glare IV upgrade.
_BLOOD_PRUNE_VALUE = (wd.POTENCIES[MISERY] - wd.POTENCIES[GLARE_III]) / wd.BLOOD_LILY_CAP
_SACRED_PRUNE_VALUE = wd.POTENCIES[GLARE_IV] - wd.POTENCIES[GLARE_III]
# Full-duration Dia DoT (the trailing application's admissible prune credit).
_DIA_FULL_DOT_P = (wd.DIA_DOT_DURATION_S / wd.DIA_DOT_TICK_S) * wd.DIA_DOT_TICK_P

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    wd.JOB_DATA.tincture_main_stat, wd.JOB_DATA.tincture_role_coeff)


class WhiteMageRotationModel(engine.BaseRotationModel):
    cooldowns = wd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=WHM_GCD_S, cast_times=wd.CAST_TIMES)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, entry_lilies: int = 0, entry_blood: int = 0,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = (),
                 locked_gcd_windows: tuple = ()):
        self.entry_lilies = max(0, min(wd.LILY_CAP, int(entry_lilies)))
        self.entry_blood = max(0, min(wd.BLOOD_LILY_CAP, int(entry_blood)))
        # Mit-plan locked heal-GCD windows (jobs/_core/heal_locks). Empty ()
        # -> the engine's lock scheduler never runs, byte-identical.
        self.locked_gcd_windows = tuple(locked_gcd_windows)
        # Multi-target N(t) schedule: where N makes Holy III out-potency Glare III
        # the filler swaps (gauge-free). Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed (threaded only when faster than the constant):
        # SpS scales BOTH the GCD recast AND cast times by the same haste
        # factor. None keeps the tier constant, byte-identical.
        if gcd_base_s is not None:
            from dataclasses import replace
            factor = gcd_base_s / WHM_GCD_S
            self.timing = replace(
                WhiteMageRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in wd.CAST_TIMES.items()})

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_filler(self, state: SimState) -> int:
        """Holy III replaces the Glare III filler when it out-potencies it at the
        live target count (gauge-free, so the choice is closed-form). N<3 (or no
        schedule) -> Glare III, byte-identical."""
        n = self._n(state.t)
        if n >= 2 and potency_for(HOLY_III, n, wd.JOB_DATA) > potency_for(
                GLARE_III, n, wd.JOB_DATA):
            return HOLY_III
        return GLARE_III

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {ASSIZE: 0.0, POM: 0.0}
        state.lilies = self.entry_lilies
        state.blood = self.entry_blood
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull channel: hardcast Glare III during the countdown so it
        # resolves at the pull (t=0) — the standard WHM opener. Begincast-
        # anchored at -cast_time (matching the player's own precast in
        # norm_casts); the recast rolls from the begincast, so the first
        # in-fight GCD presses at (recast - cast_time).
        cast_s = self.timing._cast_time(GLARE_III)
        state.timeline.append((-cast_s, GLARE_III))
        state.t = self.timing.gcd_recast_s - cast_s

    # --- Lily timer ----------------------------------------------------------

    def _accrue_lilies(self, state: SimState) -> None:
        """Materialize time-based lily gains up to `state.t` (1 / 20 s, cap 3).
        The timer keeps running while capped, so capped marks are lost — the
        in-game waste the ceiling's spend rules exist to avoid. Idempotent and
        monotone in `t`, so safe to call from pickers and `apply_cast` alike."""
        while state.next_lily_t <= state.t:
            if state.lilies < wd.LILY_CAP:
                state.lilies += 1
            state.next_lily_t += wd.LILY_INTERVAL_S

    def _misery_still_fits(self, state: SimState) -> bool:
        """A lily spend only buys damage if the Misery it feeds still lands
        before the fight ends (otherwise the spend is a free heal at best —
        never worth an uptime GCD on the ceiling)."""
        spends_needed = wd.BLOOD_LILY_CAP - state.blood
        gcds = spends_needed + 1
        return state.t + gcds * self.timing.gcd_recast_s <= state.fight_duration_s

    # --- Timing --------------------------------------------------------------

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Presence of Mind: 20% cast + recast reduction while active (snapshot
        # at slot start). Glare III's hasted 1.2 s cast stays under the hasted
        # 2.0 s recast, so the slot is recast-bound throughout.
        cast = self.timing._cast_time(gcd_id)
        recast = self.timing.gcd_recast_s
        if state.t < state.pom_until:
            cast *= wd.POM_HASTE
            recast *= wd.POM_HASTE
        return max(cast, recast)

    # weave_budget: the HardcastGCD default is already right — Glare III (the
    # only cast-time GCD) yields one weave, every instant yields two.

    # --- Pickers --------------------------------------------------------------

    def pick_gcd(self, state: SimState, params) -> int:
        self._accrue_lilies(state)
        t = state.t
        fw = params.forbidden_windows
        # 1. Dia upkeep — dropping the DoT is the one real throughput cliff.
        if state.dia_end - t <= self.gcd_duration(state, DIA, params) \
                and not is_forbidden(DIA, t, fw):
            return DIA
        # 2. Misery when bloomed (greedy; the beam owns the buff-window hold).
        if state.blood >= wd.BLOOD_LILY_CAP and not is_forbidden(MISERY, t, fw):
            return MISERY
        # 3. Glare IV under Sacred Sight.
        if state.sacred_sight > 0 and t < state.sacred_until:
            return GLARE_IV
        # 4. Lily spend only when forced (at cap, about to waste the timer) and
        #    only if the Misery it feeds still fits. Downtime spends — the free
        #    ones — happen in on_downtime_window instead.
        if (state.lilies >= wd.LILY_CAP and state.blood < wd.BLOOD_LILY_CAP
                and self._misery_still_fits(state)):
            return SOLACE
        # 5. Filler (Holy III at high target counts).
        return self._maybe_filler(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """Every strategic GCD at this slot — the beam's fork set. Glare III is
        always offered, so holding Misery / Glare IV / a lily spend for a buff
        window is explorable; Dia is offered a few GCDs early (the per-cast
        time-to-next scoring already penalizes a wasteful refresh)."""
        self._accrue_lilies(state)
        t = state.t
        fw = params.forbidden_windows
        moves: list[int] = []
        if state.blood >= wd.BLOOD_LILY_CAP and not is_forbidden(MISERY, t, fw):
            moves.append(MISERY)
        if state.dia_end - t <= 3 * self.timing.gcd_recast_s \
                and not is_forbidden(DIA, t, fw):
            moves.append(DIA)
        if state.sacred_sight > 0 and t < state.sacred_until:
            moves.append(GLARE_IV)
        if (state.lilies > 0 and state.blood < wd.BLOOD_LILY_CAP
                and self._misery_still_fits(state)):
            moves.append(SOLACE)
        moves.append(GLARE_III)
        if self._n(t) >= 2:
            moves.append(HOLY_III)   # AoE filler fork (gauge-free, beam confirms)
        return moves

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """Top-K ranking key, computed O(1) from the incremental running score
        (no timeline re-scan): banked potency + finalized Dia DoTs + the
        trailing Dia credited the full 30 s (admissible — its exact time-to-
        next credit can only be smaller), plus admissible credit for banked-
        but-unpaid value (Blood Lily nourishment heading into a Misery, unspent
        Sacred Sight stacks) so an investing line isn't pruned the slot before
        its payoff. Buff-agnostic by design — the engine's final selection
        re-scores the survivors under `buff_intervals` exactly."""
        base = state._score_flat + state._score_fin_dot
        if state._last_dia_t is not None:
            base += _DIA_FULL_DOT_P
        credit = state.blood * _BLOOD_PRUNE_VALUE
        if state.sacred_sight > 0 and state.t < state.sacred_until:
            credit += state.sacred_sight * _SACRED_PRUNE_VALUE
        return base + credit

    def beam_signature(self, state: SimState):
        """Lossless diversity-dedup key: the full future-relevant state, so two
        beams that reached the same position collapse and the width holds
        genuinely distinct lines (hold-vs-fire Misery, lily-spend placement)."""
        t = state.t
        return (
            round(t, 2), state.lilies, state.blood, state.sacred_sight,
            round(max(0.0, state.sacred_until - t), 2),
            round(max(0.0, state.pom_until - t), 2),
            round(max(0.0, state.dia_end - t), 2),
            round(state.next_lily_t - t, 2),
            round(max(0.0, state.cd_ready.get(ASSIZE, 0.0) - t), 2),
            round(max(0.0, state.cd_ready.get(POM, 0.0) - t), 2),
            state.tincture_used,
        )

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        if state.cd_ready.get(POM, 0) <= t and not is_forbidden(POM, t, fw):
            return POM
        if state.cd_ready.get(ASSIZE, 0) <= t and not is_forbidden(ASSIZE, t, fw):
            return ASSIZE
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        self._accrue_lilies(state)
        t = state.t
        state.timeline.append((t, ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

        # Incremental running score (pot-aware; see beam_prune). The pot
        # multiplier is read at this cast's snapshot time — `pot_mult_at`
        # reconstructs the latest in-sim pot, the only one that can cover it.
        base_p = potency_for(ability_id, self._n(t), wd.JOB_DATA)
        if base_p > 0:
            state._score_flat += base_p * self.pot_mult_at(state, t)

        if ability_id == DIA:
            # Finalize the previous application: credited by time-to-next
            # (capped at the DoT duration), snapshotting the pot at ITS cast.
            if state._last_dia_t is not None:
                covered = min(wd.DIA_DOT_DURATION_S, t - state._last_dia_t)
                state._score_fin_dot += (
                    (covered / wd.DIA_DOT_TICK_S) * wd.DIA_DOT_TICK_P
                    * self.pot_mult_at(state, state._last_dia_t))
            state._last_dia_t = t
            state.dia_end = t + wd.DIA_DOT_DURATION_S
        elif ability_id == MISERY:
            state.blood = 0
        elif ability_id in (SOLACE, RAPTURE):
            if state.lilies > 0:
                state.lilies -= 1
                if state.blood < wd.BLOOD_LILY_CAP:
                    state.blood += 1
        elif ability_id == GLARE_IV:
            state.sacred_sight = max(0, state.sacred_sight - 1)
        elif ability_id == POM:
            state.pom_until = t + wd.POM_DURATION_S
            state.sacred_sight = wd.SACRED_SIGHT_STACKS
            state.sacred_until = t + wd.SACRED_SIGHT_DURATION_S

    # --- Mit-plan lock hooks (engine lock scheduler) --------------------------

    def resolve_locked_gcd(self, state: SimState, ability_id: int) -> int:
        """A locked Rapture needs a lily; when none is banked at the moment the
        lock must fire, the plan's heal is paid with a hardcast Medica III
        instead (the planner's own overflow choice)."""
        if ability_id == RAPTURE:
            self._accrue_lilies(state)
            if state.lilies < 1:
                return wd.MEDICA_III
        return ability_id

    def lock_satisfiers(self, ability_id: int) -> frozenset:
        """A planned Rapture is a lily spend + party heal — the sim's own
        Solace spends (incl. the free downtime ones) cover it, and a Medica III
        (the costlier substitute) over-covers it. A planned Medica III is only
        satisfied by itself."""
        if ability_id == RAPTURE:
            return frozenset((RAPTURE, SOLACE, wd.MEDICA_III))
        return frozenset((ability_id,))

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # Spend lily heals during downtime: instant, party-targeted, so they
        # cost ZERO damage GCDs — the free Blood Lily progress every real WHM
        # banks on a disconnect. The ceiling must do it too, or a player who
        # does beats the ceiling. Slots are paced at the GCD; new lilies that
        # accrue mid-window are spent as they arrive; each heal must finish by
        # the window end so the first uptime GCD is never delayed.
        gcd = self.timing.gcd_recast_s
        t = max(state.t, win_start)
        while t + gcd <= win_end:
            # Accrue up to this slot (the lily timer runs through downtime).
            saved_t = state.t
            state.t = t
            self._accrue_lilies(state)
            state.t = saved_t
            if state.lilies > 0 and state.blood < wd.BLOOD_LILY_CAP:
                state.timeline.append((t, SOLACE))
                state.lilies -= 1
                state.blood += 1
                t += gcd
            elif state.next_lily_t <= win_end - gcd \
                    and state.blood < wd.BLOOD_LILY_CAP:
                t = max(t, state.next_lily_t)
            else:
                break

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _model_for(duration_s: float, sim_context) -> WhiteMageRotationModel:
    """Build a model bound to this run's per-pull context. Canonical unwrap
    order: CeilingContext (per-player effective GCD) -> MultiTargetContext
    (N(t) schedule) -> HealLockContext (mit-plan locked heal GCDs) -> the
    phase-continuation entry lily state (`WhmContext`); None = cold start."""
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
    if isinstance(payload, WhmContext):
        return WhiteMageRotationModel(entry_lilies=payload.entry_lilies,
                                      entry_blood=payload.entry_blood,
                                      gcd_base_s=gcd, mt_schedule=mt_schedule,
                                      locked_gcd_windows=locks)
    return WhiteMageRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule,
                                  locked_gcd_windows=locks)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to
    a multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy scoring import avoids a scoring<->simulator cycle
    at module load. The model's incremental `_score_flat` uses the SAME schedule
    (via `self._n`), so `beam_prune` ranks AoE lines consistently."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.whitemage.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests call `_score`).
_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) ----------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — WHM has no
    pet/payload scalar, so aux is always 0."""
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
    """The beam-refined optimum (buff-aware when given)."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.beam_perfect(model, _make_score(model.mt_schedule),
                               fight_duration_s, downtime_windows or [],
                               buff_intervals, width=_BEAM_WIDTH)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: burst-timing refinement + a beam search over
    the Misery/Glare IV/lily/Dia forks (guarded never to fall below the refined
    greedy ceiling). Buff-aware when `buff_intervals` is given."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.beam_perfect(model, _make_score(model.mt_schedule),
                               fight_duration_s, downtime_windows or [],
                               buff_intervals, width=_BEAM_WIDTH)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff
    windows. Falls back to the throughput optimum when there are no party buffs."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
