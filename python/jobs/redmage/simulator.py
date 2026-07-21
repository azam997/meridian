"""Idealized RDM rotation — the Red Mage `RotationModel` for the shared engine.

The first **caster** simulator. The time loop, downtime/weave/charge handling,
parameter sweep, local-search refinement and canonical buff alignment all live
in `jobs/_core/sim/engine.py`. This module supplies only the RDM-specific
rotation: the two mana gauges, the Dualcast filler loop, the enchanted melee
combo + finisher chain, the proc economy, and the per-cast state transitions.
The four `simulate_*` shims at the bottom bind the model to the engine (kept
under their original names so the sidecar, the scorer and the tests call them
unchanged).

RDM-specific rotation encoded:
- **Dualcast** — a spell with a cast time grants Dualcast, making the *next*
  spell instant. The filler alternates a 2 s-cast enabler (Jolt III / Verfire /
  Verstone, hardcast → grants Dualcast) with a Dualcasted Verthunder III /
  Veraero III (5 s cast, fired instantly). Realized via the `HardcastGCD` timing
  preset + the `dualcast` / `instant_this_slot` state flags: `gcd_duration`
  captures the instant decision per slot (pre-apply), so the post-`apply_cast`
  `weave_budget` reads the right budget — and the beam / exact-solver paths,
  which call `gcd_duration` directly, get identical timing.
- **Two mana gauges** (White / Black, 0–100). Elemental spells build them;
  the enchanted melee combo (Riposte 20, Zwerchhau 15, Redoublement 15) spends
  50 of each, building 3 Mana Stacks → Verflare/Verholy → Scorch → Resolution.
- **Procs matched to the player.** The model is parameterized by `proc_budget`
  (the player's measured Verfire+Verstone count). It spends *exactly* that many
  proc spells in enabler slots (each ~20p above the Jolt III it replaces),
  substituting Jolt III once the budget is exhausted — so the ceiling tracks the
  player's own proc luck and only proc *misuse* costs efficiency.
- **Burst** — Manafication (3 free enchanted GCDs + Prefulgence) and Embolden
  (raid buff + Vice of Thorns) are the alignment anchors the refinement nudges.

Out of scope for v1 (documented, intentionally not modeled):
- AoE line (Verthunder/Veraero II, Impact, Enchanted Moulinet) — single-target.
- Gap-closers (Corps-a-corps / Engagement / Displacement) as DPS — held for
  movement, so the idealized line doesn't fire them (keeps the ceiling honest).
- Exact pre-pull mana (the opener pre-builds during the countdown); the sim
  ramps from 0/0, slightly under-rating the very first combo. Refine live.
- Frame-perfect cast/slidecast timing; proc *timing* divergence from the player
  (near-zero potency — procs are filler-tier — beyond a proc landing in/out of a
  buff window; observe live before modeling).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import HardcastGCD
from jobs._core.tincture import spec_for_job
from jobs.redmage import data as rd


# --- Ability IDs (aliased from data for readability) ----------------------
JOLT_III           = rd.JOLT_III
VERTHUNDER_III     = rd.VERTHUNDER_III
VERAERO_III        = rd.VERAERO_III
VERFIRE            = rd.VERFIRE
VERSTONE           = rd.VERSTONE
GRAND_IMPACT       = rd.GRAND_IMPACT
ENCHANTED_RIPOSTE      = rd.ENCHANTED_RIPOSTE
ENCHANTED_ZWERCHHAU    = rd.ENCHANTED_ZWERCHHAU
ENCHANTED_REDOUBLEMENT = rd.ENCHANTED_REDOUBLEMENT
VERFLARE           = rd.VERFLARE
VERHOLY            = rd.VERHOLY
SCORCH             = rd.SCORCH
RESOLUTION         = rd.RESOLUTION
FLECHE             = rd.FLECHE
CONTRE_SIXTE       = rd.CONTRE_SIXTE
ACCELERATION       = rd.ACCELERATION
MANAFICATION       = rd.MANAFICATION
EMBOLDEN           = rd.EMBOLDEN
VICE_OF_THORNS     = rd.VICE_OF_THORNS
PREFULGENCE        = rd.PREFULGENCE
ENGAGEMENT         = rd.ENGAGEMENT
CORPS_A_CORPS      = rd.CORPS_A_CORPS
SWIFTCAST          = rd.SWIFTCAST
VERCURE            = rd.VERCURE


# --- Rotation tuning ------------------------------------------------------
GCD_BASE_S = 2.5            # RDM base GCD (no SpS gear-aware adjustment in v1)
# Reduced-recast enchanted weaponskills (faster than the 2.5 s global).
ENCHANTED_RECAST: dict[int, float] = {
    ENCHANTED_RIPOSTE:      1.5,
    ENCHANTED_ZWERCHHAU:    1.5,
    ENCHANTED_REDOUBLEMENT: 2.2,
}
MANA_FOR_COMBO = 50         # White & Black each needed to start the enchanted combo
# Where the pre-pull channel marker lands on the timeline: its begincast time
# (= -cast_time), matching how the player's own hardcast precast is begincast-
# anchored in norm_casts. The 5s Verthunder III started during the countdown and
# resolves at the pull (t=0); placing it at -5 keeps it clearly in the pre-zone,
# well separated from the t=0 Dualcast. Scoring sums the whole timeline (buff
# multiplier is 1.0 pre-pull), so its potency is credited regardless of position.
PREPULL_CHANNEL_T = -rd.CAST_TIMES[VERTHUNDER_III]
# Fallback proc budget when none is supplied (≈ one proc spell per enabler slot,
# i.e. ~every other GCD). The real value is the player's measured count, threaded
# in as sim_context; this only bites on a direct sim call without one.
_DEFAULT_PROC_RATE_S = 5.0


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """RDM picker tunables. v1 adds no axis beyond the shared knobs
    (max_weaves_per_gcd / triple_weave_clip_s / forbidden_windows); the proc
    budget is a per-pull model parameter (sim_context), not a sweep axis."""
    pass


@dataclass
class SimState(SimStateBase):
    white_mana: int = 0
    black_mana: int = 0
    # Dualcast: set after a true hardcast; consumed (-> instant) by the next
    # spell. `instant_this_slot` is captured in gcd_duration (post-pick) so the
    # post-apply weave_budget reads the slot's real instant-ness.
    dualcast: bool = False
    instant_this_slot: bool = False
    # Enchanted melee combo: 0 expects Riposte/idle, 1 Zwerchhau, 2 Redoublement.
    combo_step: int = 0
    mana_stacks: int = 0        # 0–3; 3 -> Verflare/Verholy finisher
    finisher_step: int = 0      # 1 = Scorch ready, 2 = Resolution ready
    magicked_swordplay: int = 0  # free enchanted GCDs from Manafication (0–3)
    # Burst follow-ups
    grand_impact_ready: bool = False  # from Acceleration
    prefulgence_ready: bool = False   # from Manafication
    thorned_flourish: bool = False    # Vice of Thorns available (from Embolden)
    # Free instant casts (Acceleration / Swiftcast): each lets one Verthunder III
    # / Veraero III (440) be cast instantly WITHOUT a Dualcast — so a slot that
    # would otherwise be a Jolt III enabler (after a non-DC-granting instant GCD)
    # becomes a 440 instead. The 0.5% elite parses extract that my greedy filler
    # was leaving on the table.
    free_instant: int = 0
    _free_instant_use: bool = False
    # Proc economy (matched to the player's count via proc_budget).
    procs_remaining: int = 0


# --- Refinement / canonical anchors ---------------------------------------
# The greedy picker fires burst as soon as it's available; the refinement nudges
# Manafication / Embolden (the 2-minute burst enablers) into the buff windows.
_PERFECT_ANCHORS: tuple[int, ...] = (EMBOLDEN, MANAFICATION)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (EMBOLDEN, MANAFICATION)

# Sweep axes (kept job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)


# --- The RDM rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    rd.JOB_DATA.tincture_main_stat, rd.JOB_DATA.tincture_role_coeff)


class RedMageRotationModel(engine.BaseRotationModel):
    cooldowns = rd.COOLDOWNS
    timing = HardcastGCD(gcd_recast_s=GCD_BASE_S, cast_times=rd.CAST_TIMES)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, proc_budget: int = 0, gcd_base_s: float | None = None):
        self.proc_budget = max(0, int(proc_budget))
        # Per-player Spell Speed (threaded only when faster than the constant): SpS
        # scales BOTH the GCD recast AND the cast times by the same haste factor, so
        # rebuild the HardcastGCD with both scaled. None keeps the tier constant,
        # byte-identical. (Casters vary in SpS far more than ranged do in SkS.)
        if gcd_base_s is not None:
            from dataclasses import replace
            factor = gcd_base_s / GCD_BASE_S
            self.timing = replace(
                RedMageRotationModel.timing, gcd_recast_s=gcd_base_s,
                cast_times={k: v * factor for k, v in rd.CAST_TIMES.items()})

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {ACCELERATION: 2.0, ENGAGEMENT: 2.0, CORPS_A_CORPS: 2.0}
        state.cd_ready = {FLECHE: 0.0, CONTRE_SIXTE: 0.0,
                          MANAFICATION: 0.0, EMBOLDEN: 0.0, SWIFTCAST: 0.0}
        state.procs_remaining = self.proc_budget
        return state

    def prepull(self, state: SimState, params) -> None:
        # Pre-pull channel: hardcast Verthunder III during the countdown so it
        # resolves at the pull (t=0). It's a FREE 440 (the cast time was spent
        # pre-fight, not in-fight) that also grants Dualcast, so the first
        # in-fight GCD is an instant 440 rather than a hardcast enabler. This is
        # the standard RDM opener, so it's always taken (not a swept axis). The
        # 50% Verfire proc it can roll is already accounted for by the matched
        # proc budget, so only mana + Dualcast are applied here.
        state.timeline.append((PREPULL_CHANNEL_T, VERTHUNDER_III))
        state.black_mana = min(rd.MANA_CAP,
                               state.black_mana
                               + rd.BLACK_MANA_GENERATORS.get(VERTHUNDER_III, 0))
        state.dualcast = True

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Capture the Dualcast decision for this slot (pre-apply; `pick_gcd` reads
        # but never mutates these flags, so pick-then-duration == capture-before-
        # pick): a hardcast made instant by Dualcast runs at the 2.5 s recast, not
        # its 5 s cast lock. Stashes `instant_this_slot` / `_free_instant_use` for
        # the post-apply weave_budget / apply_cast. Lives HERE (not in a `gcd_slot`
        # override) so the beam / exact-solver paths — which call `gcd_duration`
        # directly, never `gcd_slot` — get identical timing (the engine seam caveat).
        dualcast_active = state.dualcast
        base_cast = self.timing._cast_time(gcd_id)
        # A 5 s spell can be made instant by Dualcast OR by a banked free instant
        # (Acceleration / Swiftcast). The free-instant path is what lets a
        # dualcast-less slot run a 440 instead of a Jolt III enabler.
        free_instant_use = (base_cast > 0.0 and not dualcast_active
                            and state.free_instant > 0
                            and gcd_id in (VERTHUNDER_III, VERAERO_III))
        state._free_instant_use = free_instant_use
        instant = base_cast <= 0.0 or dualcast_active or free_instant_use
        state.instant_this_slot = instant
        if gcd_id in ENCHANTED_RECAST:
            return ENCHANTED_RECAST[gcd_id]
        if instant:
            return self.timing.gcd_recast_s
        return max(base_cast, self.timing.gcd_recast_s)

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        base = (self.timing.instant_weaves if state.instant_this_slot
                else self.timing.hardcast_weaves)
        return min(base, params.max_weaves_per_gcd)

    def pick_gcd(self, state: SimState, params) -> int:
        # 1. Finisher chain (Scorch -> Resolution) after a Verflare/Verholy.
        if state.finisher_step == 1:
            return SCORCH
        if state.finisher_step == 2:
            return RESOLUTION
        # 2. Melee finisher at 3 stacks — build the lower-of mana (and chase the
        #    guaranteed proc, which our budget already accounts for).
        if state.mana_stacks >= 3:
            return VERHOLY if state.white_mana <= state.black_mana else VERFLARE
        # 3. Continue an in-progress enchanted combo.
        if state.combo_step == 1:
            return ENCHANTED_ZWERCHHAU
        if state.combo_step == 2:
            return ENCHANTED_REDOUBLEMENT
        # 4. Start the enchanted combo: free under Magicked Swordplay
        #    (Manafication), else when both mana are at the 50/50 threshold.
        if state.magicked_swordplay > 0 or (
                state.white_mana >= MANA_FOR_COMBO and state.black_mana >= MANA_FOR_COMBO):
            return ENCHANTED_RIPOSTE
        # 5. Grand Impact (instant, from Acceleration).
        if state.grand_impact_ready:
            return GRAND_IMPACT
        # 6. Filler Dualcast loop.
        if state.dualcast:
            # Dualcasted 440 — build the lower mana.
            return VERTHUNDER_III if state.black_mana <= state.white_mana else VERAERO_III
        # Banked free instant (Acceleration / Swiftcast): run a 440 in this
        # dualcast-less slot instead of a Jolt III. Doesn't grant Dualcast, so
        # the loop resumes with an enabler next cast.
        if state.free_instant > 0:
            return VERTHUNDER_III if state.black_mana <= state.white_mana else VERAERO_III
        # Enabler slot (hardcast → grants Dualcast): spend a proc, else Jolt III.
        # Procs are RNG off Verthunder III / Veraero III, so they accrue THROUGH
        # the fight — you can't have one at t=0. Pace the budget evenly in time
        # (spend only when behind the linear schedule) so the opener can't open on
        # a proc and the proc mana (+5/color) isn't front-loaded, which would skew
        # gauge expenditure + the melee-combo cadence vs real play.
        procs_used = self.proc_budget - state.procs_remaining
        due = (self.proc_budget * state.t / state.fight_duration_s
               if state.fight_duration_s > 0 else 0.0)
        if state.procs_remaining > 0 and procs_used < due:
            return VERFIRE if state.black_mana <= state.white_mana else VERSTONE
        return JOLT_III

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Embolden — raid buff (+ Thorned Flourish).
        if state.cd_ready.get(EMBOLDEN, 0) <= t and not is_forbidden(EMBOLDEN, t, fw):
            return EMBOLDEN
        # Manafication — only when idle of combo/finisher so its 3 free enchanted
        # GCDs aren't wasted mid-sequence.
        if (state.cd_ready.get(MANAFICATION, 0) <= t
                and not is_forbidden(MANAFICATION, t, fw)
                and state.combo_step == 0 and state.finisher_step == 0
                and state.mana_stacks == 0 and state.magicked_swordplay == 0):
            return MANAFICATION
        # Prefulgence (from Manafication) — high-potency, fire ASAP.
        if state.prefulgence_ready:
            return PREFULGENCE
        # Vice of Thorns (from Embolden) — fire ASAP.
        if state.thorned_flourish:
            return VICE_OF_THORNS
        # Acceleration — grants Grand Impact; keep a charge moving, don't stack
        # the Grand Impact it grants.
        if (state.charges.get(ACCELERATION, 0) >= 1
                and not is_forbidden(ACCELERATION, t, fw)
                and not state.grand_impact_ready):
            return ACCELERATION
        # Fleche / Contre-sixte — big oGCDs on their own recast.
        if state.cd_ready.get(FLECHE, 0) <= t and not is_forbidden(FLECHE, t, fw):
            return FLECHE
        if state.cd_ready.get(CONTRE_SIXTE, 0) <= t and not is_forbidden(CONTRE_SIXTE, t, fw):
            return CONTRE_SIXTE
        # Swiftcast — a second free-instant source (like Acceleration), fired on
        # its 40s cooldown for the max DPS ceiling. Held while a free instant is
        # already banked so the next instant isn't wasted. (Players routinely save
        # Swiftcast for a movement mechanic; that's never flagged — it isn't a DPS
        # cooldown, so it produces no missed-cast / drift finding.)
        if (state.cd_ready.get(SWIFTCAST, 0) <= t
                and not is_forbidden(SWIFTCAST, t, fw)
                and state.free_instant == 0):
            return SWIFTCAST
        # Gap-closers as low-priority weave fillers — they only fire when a weave
        # slot is otherwise idle, so the count self-limits to roughly the
        # convenient-use rate top RDMs actually hit (not full cooldown).
        if state.charges.get(ENGAGEMENT, 0) >= 1:
            return ENGAGEMENT
        if state.charges.get(CORPS_A_CORPS, 0) >= 1:
            return CORPS_A_CORPS
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Mana generation.
        if ability_id in rd.WHITE_MANA_GENERATORS:
            state.white_mana = min(rd.MANA_CAP,
                                   state.white_mana + rd.WHITE_MANA_GENERATORS[ability_id])
        if ability_id in rd.BLACK_MANA_GENERATORS:
            state.black_mana = min(rd.MANA_CAP,
                                   state.black_mana + rd.BLACK_MANA_GENERATORS[ability_id])
        # Enchanted-combo mana spend — free under Magicked Swordplay.
        if ability_id in rd.WHITE_MANA_SPENDERS:
            if state.magicked_swordplay > 0:
                state.magicked_swordplay -= 1
            else:
                state.white_mana = max(0, state.white_mana - rd.WHITE_MANA_SPENDERS[ability_id])
                state.black_mana = max(0, state.black_mana - rd.BLACK_MANA_SPENDERS[ability_id])

        # Generic cooldown / charges.
        apply_cooldown(state, self.cooldowns, ability_id)

        # Dualcast bookkeeping (cast-time spells only): a true hardcast grants it;
        # a hardcast made instant by Dualcast consumes it. Instant weaponskills
        # (enchanted / finisher / Grand Impact) leave it untouched.
        base_cast = self.timing._cast_time(ability_id)
        if base_cast > 0.0:
            state.dualcast = not state.instant_this_slot
        # Consume a banked free instant if this VT3/VA3 used one.
        if state._free_instant_use:
            state.free_instant = max(0, state.free_instant - 1)
            state._free_instant_use = False

        # Per-ability effects.
        if ability_id in (VERFIRE, VERSTONE):
            state.procs_remaining = max(0, state.procs_remaining - 1)
        elif ability_id == GRAND_IMPACT:
            state.grand_impact_ready = False
        elif ability_id == ENCHANTED_RIPOSTE:
            state.mana_stacks = min(3, state.mana_stacks + 1)
            state.combo_step = 1
        elif ability_id == ENCHANTED_ZWERCHHAU:
            state.mana_stacks = min(3, state.mana_stacks + 1)
            state.combo_step = 2
        elif ability_id == ENCHANTED_REDOUBLEMENT:
            state.mana_stacks = min(3, state.mana_stacks + 1)
            state.combo_step = 0
        elif ability_id in (VERFLARE, VERHOLY):
            state.mana_stacks = 0
            state.finisher_step = 1
            state.combo_step = 0
        elif ability_id == SCORCH:
            state.finisher_step = 2
        elif ability_id == RESOLUTION:
            state.finisher_step = 0
        elif ability_id == MANAFICATION:
            state.magicked_swordplay = 3
            state.prefulgence_ready = True
            state.combo_step = 0
        elif ability_id == EMBOLDEN:
            state.thorned_flourish = True
        elif ability_id == ACCELERATION:
            # Acceleration grants BOTH Grand Impact Ready AND an instant-cast
            # VT3/VA3 — two payoffs from one oGCD (live-probe confirmed on a real
            # log: Accel -> instant Verthunder III + a later Grand Impact).
            state.grand_impact_ready = True
            state.free_instant += 1
        elif ability_id == SWIFTCAST:
            # Swiftcast grants a single free instant (no Grand Impact). Not in
            # COOLDOWNS, so its recast is tracked here directly.
            state.free_instant += 1
            state.cd_ready[SWIFTCAST] = t + rd.SWIFTCAST_RECAST_S
        elif ability_id == VICE_OF_THORNS:
            state.thorned_flourish = False
        elif ability_id == PREFULGENCE:
            state.prefulgence_ready = False

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # Cast Vercure during downtime to bank a Dualcast, so the first GCD out of
        # downtime is an INSTANT 440 (Verthunder/Veraero III) rather than a 2 s
        # hardcast enabler — the standard RDM downtime trick. Vercure is a heal
        # (no DPS, absent from POTENCIES), so this is a Dualcast flag + a display
        # cast, not scored. Modeling it keeps the ceiling's downtime exit at the
        # optimal instant 440, so a player who does the trick can't beat it there.
        if (win_end - win_start) >= rd.VERCURE_CAST_S:
            if not state.dualcast:
                # Time the Vercure to complete at the uptime edge (Dualcast then
                # fresh for the first in-fight GCD).
                press_t = max(win_start, win_end - rd.VERCURE_CAST_S)
                state.timeline.append((press_t, VERCURE))
            state.dualcast = True

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _default_proc_budget(duration_s: float) -> int:
    return max(0, int(duration_s / _DEFAULT_PROC_RATE_S))


def _model_for(duration_s: float, sim_context) -> RedMageRotationModel:
    """Build a model bound to this run's per-pull context. After unwrapping any
    per-player effective GCD (CeilingContext, faster-than-constant Spell Speed), the
    payload is the player's measured Verfire+Verstone proc count; falls back to a
    duration estimate."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    if isinstance(payload, MultiTargetContext):
        payload = payload.inner          # the proc budget rides inside
    pb = payload if payload is not None else _default_proc_budget(duration_s)
    return RedMageRotationModel(proc_budget=int(pb), gcd_base_s=gcd)


def _schedule_of(sim_context):
    """The multi-target N(t) schedule from this run's sim_context ('()' on a
    single-target pull)."""
    from jobs._core.downtime_sources import schedule_from_context
    return schedule_from_context(sim_context)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn bound to a multi-target N(t) `schedule`:
    the cleaving finisher chain (Scorch / Resolution / Verflare / Verholy / Grand
    Impact) + Contre Sixte scale per-target via `aoe_potency.potency_for`. Buff-
    aware when given. Empty schedule -> single target, byte-identical. (The
    dedicated AoE filler / Enchanted Moulinet combo are a deferred refinement.)"""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.redmage.scoring import score_delivered_potency
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
    """Run the idealized rotation once. Returns (timeline, 0) — RDM has no pet/
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
        model, _make_score(_schedule_of(sim_context)), fight_duration_s,
        downtime_windows or [], buff_intervals=buff_intervals)
    return timeline, aux


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: sweep + local-search refinement, buff-aware when
    `buff_intervals` is given, then the shared raid-window burst max-guard
    (Embolden + Manafication aligned to the party window — `refine`'s delay-only
    nudge under-aligns the opener double-melee; the forced variant wins ~+1.2% at
    the 2-min cadence, max-guarded so it never regresses)."""
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
