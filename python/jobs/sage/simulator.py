"""Idealized SGE rotation — the Sage `RotationModel` for the shared engine.

The analyzer's fourth **healer** simulator (after WHM + AST + SCH). The time loop,
downtime/weave handling, sweep, refinement and canonical alignment all live in
`jobs/_core/sim/engine.py`; this module supplies only the SGE-specific rotation:

- **Filler** — hardcast Dosis III (1.5s cast < 2.5s recast, so the slot is always
  recast-bound; the cast only costs a weave slot). Dyskrasia II replaces the filler
  in AoE windows.
- **The DoT** — Eukrasian Dosis III, applied by a **2-GCD sequence**: Eukrasia (a
  0-potency instant GCD) sets `eukrasia_active`, and the next GCD casts Eukrasian
  Dosis III (which clears the flag and applies the ~30s DoT, scored per cast by
  time-to-next so clipping self-penalizes). The id carries the "eukrasified" state;
  `pick_gcd` stays pure. Nothing preempts the follow-up (it's the top GCD priority).
- **Phlegma III** — a 2-charge GCD (~40s/charge) that out-potencies the filler, so
  the ceiling dumps a charge whenever one is ready (regen is never wasted — dumping
  ASAP maximizes total casts and can never overcap). Engine multi-charge regen
  (`state.charges`) runs it, including through downtime.
- **Psyche** — the lone damage oGCD (~60s), fired on cooldown.

**No gauge int** (simpler than SCH's Aetherflow): Addersgall is heal-only and
Addersting gates only Toxikon II (which sits below Dosis III → the ceiling never
casts it, so it needs no state). Toxikon II and Pneuma are delivered-only fillers
(below the filler potency), never produced by the sim. Aux is always 0 (no pet).

There is **no GCD fork** — the one plausible choice, holding Phlegma charges for a
burst, only helps the buff-aligned lenses (floored at delivered), and the strict
gate ceiling has no raid windows, so greedy Phlegma-dump is optimal. The ceiling
therefore routes through `engine.perfect` + `canonical_aligned_max_guard` (the
AST/SCH/RDM pattern), NOT the beam. `demonstrated_cadence_anchor` (scoring.py)
handles the flat-GCD sub-band. Both can only RAISE the ceiling, so the <=100% guard
holds.

The healer mit-plan lock hooks are inherited unchanged from `BaseRotationModel`:
SGE's locked heal (Eukrasian Prognosis II) is always castable and the damage line
never voluntarily heals, so the base identity `resolve_locked_gcd` /
`lock_satisfiers` / `on_downtime_window` is correct.

Out of scope for v1 (documented, intentionally not modeled): AoE beyond the filler
swap (Eukrasian Dyskrasia AoE DoT, Phlegma/Pneuma cleave); MP; phase-continuation
carryover (Eukrasian Dosis remaining — `SageContext` is a stubbed empty payload).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.sage import data as gd


# --- Ability IDs (aliased from data for readability) ------------------------
DOSIS_III           = gd.DOSIS_III
EUKRASIA            = gd.EUKRASIA
EUKRASIAN_DOSIS_III = gd.EUKRASIAN_DOSIS_III
PHLEGMA_III         = gd.PHLEGMA_III
PSYCHE              = gd.PSYCHE
DYSKRASIA_II        = gd.DYSKRASIA_II

SGE_GCD_S: float = gd.SGE_GCD_S


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """SGE picker tunables. No axis beyond the shared knobs — the kit weaves only
    Psyche + the pot, with no strategic GCD choice (Phlegma is a pure dump), so the
    weave sweep is a single point."""
    pass


@dataclass
class SimState(SimStateBase):
    dosis_dot_end: float = 0.0     # current Eukrasian Dosis III application expires here
    eukrasia_active: bool = False  # set by Eukrasia -> next GCD is Eukrasian Dosis III


@dataclass(frozen=True)
class SageContext:
    """Per-pull phase-continuation entry state (M12S-P2 style). Stubbed empty for
    v1 — SGE has no offensive gauge, and Eukrasian-Dosis-remaining carryover is a
    deferred calibration lever. Falsy => cold start => byte-identical."""
    entry_dosis_remaining_s: float = 0.0

    def __bool__(self) -> bool:
        return self.entry_dosis_remaining_s > 0.0


# --- Refinement / canonical anchors -----------------------------------------
# SGE has no self-buff burst window; Phlegma III + Psyche are the high-potency casts
# a good SGE aligns into the party's raid windows. They're the refinement anchors so
# canonical alignment nudges them toward the buff windows (max-guarded).
_ANCHORS: tuple[int, ...] = (PSYCHE, PHLEGMA_III)

# Sweep axes: the kit weaves at most Psyche + the pot around a burst GCD, so a
# 2-weave budget is never binding — a single point.
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2,)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    gd.JOB_DATA.tincture_main_stat, gd.JOB_DATA.tincture_role_coeff)


class SageRotationModel(engine.BaseRotationModel):
    cooldowns = gd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=SGE_GCD_S, cast_times=gd.CAST_TIMES)
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
        # Multi-target N(t) schedule: where N makes Dyskrasia II out-potency Dosis
        # III the filler swaps. Empty () -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Spell Speed (threaded only when faster than the constant):
        # SpS scales BOTH the GCD recast AND cast times by the same haste factor.
        # None keeps the tier constant, byte-identical. SGE has no recast-haste
        # self-buff, so the GCD stays flat.
        if gcd_base_s is not None:
            from dataclasses import replace
            factor = gcd_base_s / SGE_GCD_S
            self.timing = replace(
                SageRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in gd.CAST_TIMES.items()})

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        """The Eukrasia DoT sequence runs at FIXED, speed-immune recasts (the NIN
        mudra+ninjutsu pattern): Eukrasia 1.0s, Eukrasian Dosis III 1.5s (wiki:
        "Instant / 1.5s (GCD)"; 940 live samples read a rock-stable 1.51s). The pair
        = 2.5s = exactly one base GCD. Everything else runs at the (possibly
        SpS-hasted) normal GCD from `timing`. Modeling Eukrasian Dosis at the full
        2.5s over-charges every refresh by ~1.0s and pushes the ceiling too low."""
        fixed = gd.FIXED_RATE_GCDS.get(gcd_id)
        if fixed is not None:
            return fixed
        return self.timing.duration(state, gcd_id, params)

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        """Fixed-rate slots weave by their length: the 1.0s Eukrasia is too short for
        any oGCD (0), the 1.5s Eukrasian Dosis fits one (matching a real SGE weaving
        Psyche in that slot). Normal GCDs keep the HardcastGCD budget."""
        fixed = gd.FIXED_RATE_GCDS.get(gcd_id)
        if fixed is not None:
            return min(1 if fixed >= 1.5 else 0, params.max_weaves_per_gcd)
        return self.timing.weave_budget(state, gcd_id, params)

    def _n(self, t: float) -> int:
        """Target count active at time `t` (1 with no schedule)."""
        return n_at(t, self.mt_schedule)

    def _maybe_filler(self, state: SimState) -> int:
        """Dyskrasia II replaces the Dosis III filler when it out-potencies it at the
        live target count. N<2 (or no schedule) -> Dosis III, byte-identical."""
        n = self._n(state.t)
        if n >= 2 and potency_for(DYSKRASIA_II, n, gd.JOB_DATA) > potency_for(
                DOSIS_III, n, gd.JOB_DATA):
            return DYSKRASIA_II
        return DOSIS_III

    def init_state(self) -> SimState:
        state = SimState()
        # Phlegma III opens with both charges; Psyche is a single-recast oGCD.
        state.charges = {PHLEGMA_III: float(gd.PHLEGMA_CHARGES)}
        state.cd_ready = {PSYCHE: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull channel: hardcast Dosis III during the countdown so it resolves at
        # the pull (t=0). Begincast-anchored at -cast_time (matching the player's own
        # precast in norm_casts); the recast rolls from the begincast, so the first
        # in-fight GCD presses at (recast - cast_time).
        cast_s = self.timing._cast_time(DOSIS_III)
        state.timeline.append((-cast_s, DOSIS_III))
        state.t = self.timing.gcd_recast_s - cast_s

    # --- Pickers --------------------------------------------------------------

    def pick_gcd(self, state: SimState, params) -> int:
        t = state.t
        fw = params.forbidden_windows
        # 1. Eukrasia sequence follow-up — once Eukrasia is up, the very next GCD is
        #    always Eukrasian Dosis III (nothing preempts the DoT application).
        if state.eukrasia_active and not is_forbidden(EUKRASIAN_DOSIS_III, t, fw):
            return EUKRASIAN_DOSIS_III
        # 2. DoT upkeep — start the Eukrasia setup so its Eukrasian Dosis III follow-up
        #    lands before the DoT drops (the one real throughput cliff). The lead is one
        #    FILLER GCD (the slot I'd otherwise skip into) plus the 1.0s Eukrasia itself,
        #    so a filler is never cast into a gap; the follow-up lands in (t, dot_end].
        #    (Uses the filler duration, NOT Eukrasian Dosis's own 1.5s recast — the
        #    latter is the fast payoff GCD, not the slot the refresh must pre-empt.)
        lead = (self.gcd_duration(state, DOSIS_III, params)
                + self.gcd_duration(state, EUKRASIA, params))
        if state.dosis_dot_end - t <= lead and not is_forbidden(EUKRASIA, t, fw):
            return EUKRASIA
        # 3. Phlegma III — dump a charge whenever one is ready (out-potencies filler;
        #    dumping ASAP never overcaps and maximizes total casts).
        if state.charges.get(PHLEGMA_III, 0.0) >= 1.0 \
                and not is_forbidden(PHLEGMA_III, t, fw):
            return PHLEGMA_III
        # 4. Filler (Dyskrasia II at high target counts, else Dosis III).
        return self._maybe_filler(state)

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        # Psyche — the lone damage oGCD, fired on its 60s cooldown.
        if state.cd_ready.get(PSYCHE, 0) <= t and not is_forbidden(PSYCHE, t, fw):
            return PSYCHE
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

        if ability_id == EUKRASIA:
            state.eukrasia_active = True
        elif ability_id == EUKRASIAN_DOSIS_III:
            state.dosis_dot_end = t + gd.EUKRASIAN_DOSIS_DOT_DURATION_S
            state.eukrasia_active = False

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _model_for(duration_s: float, sim_context) -> SageRotationModel:
    """Build a model bound to this run's per-pull context. Canonical unwrap order:
    CeilingContext (per-player effective GCD) -> MultiTargetContext (N(t) schedule)
    -> HealLockContext (mit-plan locked heal GCDs) -> the phase-continuation entry
    state (`SageContext`, empty in v1); None = cold start."""
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
    # SageContext carries no offensive state in v1, so it needs no branch here — the
    # model ignores it. Kept in the unwrap chain for forward-compatibility.
    return SageRotationModel(gcd_base_s=gcd, mt_schedule=mt_schedule,
                             locked_gcd_windows=locks)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy scoring import avoids a scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.sage.scoring import score_delivered_potency
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
    """Run the idealized rotation once. Returns (timeline, 0) — SGE has no pet, so
    there is no payload scalar and aux is always 0."""
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
    GCD-perfect ceiling as `simulate_idealized_perfect` — SGE has no beam fork)."""
    return simulate_idealized_perfect(fight_duration_s, downtime_windows,
                                      buff_intervals, sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: sweep + local-search refinement (buff-aware when given),
    then the shared raid-window burst max-guard (Phlegma III + Psyche forced into the
    party window; max-guarded so it never regresses the refined greedy ceiling). No
    beam — SGE has no strategic GCD fork."""
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
    """Idealized rotation with the burst forced into the raid-buff windows. Falls
    back to the throughput optimum when there are no party buffs."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.canonical_aligned(model, _make_score(_schedule_of(sim_context)),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
