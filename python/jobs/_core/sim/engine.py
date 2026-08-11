"""Job-agnostic idealized-rotation engine.

The goal is a **GCD-perfect optimal** rotation — the greatest ideal output a job
can deliver in a given kill time, i.e. a true upper bound that no real player can
exceed. The engine realizes that in layers, each a strict generalization of the
last:

  1. `run_rotation` — a priority-based greedy picker (one action per slot); the
     cheap baseline.
  2. `perfect` — the greedy line plus a local-search refinement of oGCD *burst
     timing* (hold Inner Release / Ikishoten / Meikyo into raid-buff windows).
  3. `beam_search` / `beam_perfect` — a beam search over the model's GCD *forks*
     (e.g. SAM "refresh Higanbana now vs. keep building toward a 3-Sen Midare"),
     run on top of (2)'s refined timing. **Width 1 with the default single greedy
     candidate reproduces (1)/(2) exactly**, so a job is byte-identical until it
     opts in by overriding `gcd_candidates`. As the beam width grows the ceiling
     approaches the true GCD-perfect optimum.

     ⚠️ Seam caveat: the beam and the exact solver take slot timing from
     `gcd_duration(state, gcd_id, params)`, NOT from `gcd_slot` — only the
     greedy loop calls `gcd_slot`. A model whose slot timing lives in a
     `gcd_slot` override must move that logic into `gcd_duration` before
     opting into `gcd_candidates` / `legal_gcds`, or the searched lines run
     at wrong GCD speeds. (No shipped model overrides `gcd_slot` today —
     MCH's pre-pick Overheated capture and RDM's Dualcast capture both moved
     to `gcd_duration`.)

The output is validated against real top-parse data: a correct ceiling has top
parses clustering just under 100% and **never** over it.

That loop, the downtime/forbidden-window handling, the multi-charge regen, the
parameter sweep, the refinement, the beam search and the canonical buff-window
alignment are **identical across jobs**. They live here, once. A job supplies
only the parts that genuinely differ via the `RotationModel` protocol below:

  * `init_state()` / `prepull(state, params)` — the job's gauge state + pre-pull
    casts (a channel that resolves at t≈0, and/or a melee engage-delay start).
  * `pick_gcd` / `gcd_candidates` / `pick_ogcd` / `apply_cast` — the rotation
    priority, the beam-search forks, and the per-cast state transitions.
  * `gcd_slot` / `gcd_duration` / `weave_budget` — GCD timing (defaults defer to
    a `GcdTiming` archetype preset; jobs override for reduced-GCD windows like
    MCH Overheated / RPR Enshroud).
  * `on_downtime_window` — what (if anything) to squeeze at a downtime edge
    (MCH Flamethrower tick, RPR Soulsow re-arm, SAM Meditate). Default: nothing.
  * `sweep_params` / `agnostic_anchors` / `buff_anchors` / `canonical_anchors` —
    the sweep axes + which burst the refinement nudges.
  * `final_aux(state)` — an opaque scalar the job's scorer consumes (MCH = total
    Queen battery spent; everyone else = 0).

The engine reads only the base `SimStateBase` fields and the base
`SimParamsBase` knobs; jobs subclass both to add gauge fields / sweep axes.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional, Protocol

from jobs._core.buff_windows import multiplier_at
from jobs._core.tincture import TINCTURE_ACTION_ID


# --- Engine-wide tuning (identical across every current job) ----------------

WEAVE_DURATION_S = 0.7        # animation lockout per oGCD weave
_MAX_ITERS = 6000            # belt-and-braces against simulator bugs (never binds)

# Local-search refinement (the "perfect" sim).
_PERFECT_DELAY_OPTIONS: tuple[float, ...] = (2.5, 5.0, 7.5)
_PERFECT_MAX_ITERATIONS = 5
_BUFF_MAX_ITERATIONS = 8
# How far ahead the optimizer will hold a burst ability to land it in an
# upcoming raid-buff window.
_ALIGN_HORIZON_S: float = 35.0
# Canonical "hold the 2-min burst for the window" line: hold at most this long.
_CANONICAL_HOLD_S: float = 110.0
# Standard FFXIV burst cadence (opener, then every 2 minutes). The default in-sim
# pot rule holds each non-opener tincture to the next boundary, where both the job's
# own 2-min burst and the raid burst land — so pots are placed well even in the
# raid-agnostic strict scenario. (Mirror of buff_windows.BURST_CADENCE_S.)
_BURST_CADENCE_S: float = 120.0


# --- State + params base ----------------------------------------------------

@dataclass
class SimStateBase:
    """The simulation state fields the engine itself touches. Jobs subclass and
    add their gauge / proc / combo fields (heat+battery, soul+shroud, mana …)."""
    t: float = 0.0
    charges: dict[int, float] = field(default_factory=dict)
    cd_ready: dict[int, float] = field(default_factory=dict)
    last_gcd_t: float = float("-inf")  # start time of the most recent GCD fired
    timeline: list[tuple[float, int]] = field(default_factory=list)
    fight_duration_s: float = 600.0
    # Boss-untargetable windows for this run (set by `run_rotation`). The engine
    # loop reads its own local copy; a model's pickers read this to make
    # targetability-aware decisions — e.g. a pet whose damage lands over a window
    # is worthless if the boss leaves mid-sequence (MCH Queen).
    downtime_windows: list[tuple[float, float]] = field(default_factory=list)
    # Exogenous raid-buff overlay for this run, as (start, end, multiplier). Set by
    # the exact solver (`optimal.solve_optimal`) so a job's buff-aware incremental
    # scoring can read the multiplier in `apply_cast` — buffs/tincture optimized
    # *inside* the sim, not multiplied onto a finished timeline. The greedy/beam path
    # leaves this empty and passes buffs to `score_fn` directly (byte-identical).
    buff_intervals: list[tuple[float, float, float]] = field(default_factory=list)
    # In-sim tincture (damage potion) state: when the pot comes off cooldown, and how
    # many pots have been used. A model opts in by setting `tincture_spec`; the slot
    # loop then places `TINCTURE_ACTION_ID` markers (`_maybe_pot`) that scoring reads
    # as a self-buff window (jobs._core.tincture) — pot placement optimized INSIDE the
    # sim, not a post-hoc sweep. Both stay 0 for a model that doesn't pot →
    # byte-identical.
    tincture_cd_ready: float = 0.0
    tincture_used: int = 0
    # Per-window satisfied counts for `model.locked_gcd_windows` (the healer
    # mit-plan locks — see jobs/_core/heal_locks.py). Parallel tuple, immutable
    # so `_clone_state` copies it trivially. Empty for every job without locks
    # → every lock code path below is skipped → byte-identical.
    lock_done: tuple[int, ...] = ()


@dataclass(frozen=True)
class SimParamsBase:
    """The picker knobs the engine reads. Jobs subclass to add sweep axes
    (queen battery threshold, harvest-moon priority, …). Frozen so instances
    stay hashable for the scoring LRU caches; `dataclasses.replace` works on the
    subclass, which is how the refinement injects `forbidden_windows`.

    forbidden_windows: refinement hook — (ability_id, start, end); the picker
        skips that ability while `start <= t < end`. Empty = steady state.
    """
    max_weaves_per_gcd: int = 2
    triple_weave_clip_s: float = 0.5
    forbidden_windows: tuple[tuple[int, float, float], ...] = ()


# --- The per-job seam -------------------------------------------------------

class RotationModel(Protocol):
    """What the engine needs from a job. See `BaseRotationModel` for the
    defaults most jobs inherit; only `init_state`, `pick_gcd`, `pick_ogcd` and
    `apply_cast` are mandatory."""

    cooldowns: dict[int, tuple[float, int]]      # id -> (recast_s, max_charges)
    agnostic_anchors: tuple[int, ...]
    buff_anchors: tuple[int, ...]
    canonical_anchors: tuple[int, ...]
    tincture_spec: object                        # tincture.TinctureSpec | None

    def init_state(self) -> SimStateBase: ...
    def prepull(self, state: SimStateBase, params) -> None: ...
    def gcd_slot(self, state: SimStateBase, params) -> tuple[int, float]: ...
    def pick_gcd(self, state: SimStateBase, params) -> int: ...
    def gcd_candidates(self, state: SimStateBase, params) -> list[int]: ...
    def beam_prune(self, state: SimStateBase, score_fn, buff_intervals) -> float: ...
    def beam_signature(self, state: SimStateBase): ...
    def pick_ogcd(self, state: SimStateBase, params) -> Optional[int]: ...
    def apply_cast(self, state: SimStateBase, ability_id: int) -> None: ...
    def weave_budget(self, state: SimStateBase, gcd_id: int, params) -> int: ...
    def on_downtime_window(self, state: SimStateBase,
                           win_start: float, win_end: float) -> None: ...
    def should_pot(self, state: SimStateBase, params) -> bool: ...
    def final_aux(self, state: SimStateBase) -> int: ...
    def sweep_params(self, extra_forbidden: tuple[tuple[int, float, float], ...]
                     ) -> Iterable[SimParamsBase]: ...

    # --- Exact-solver seam (jobs/_core/sim/optimal.py) ----------------------
    def legal_gcds(self, state: SimStateBase, params) -> list[int]: ...
    def ogcd_candidates(self, state: SimStateBase, params) -> list: ...
    def clone(self, state: SimStateBase) -> SimStateBase: ...
    def dominance_key(self, state: SimStateBase): ...
    def dominance_vector(self, state: SimStateBase) -> tuple: ...
    def admissible_remaining(self, state: SimStateBase) -> float: ...
    def exact_g(self, state: SimStateBase, score_fn) -> float: ...
    def terminal_g(self, state: SimStateBase, score_fn) -> float: ...


class BaseRotationModel:
    """Default RotationModel behavior. Subclass and override the rotation
    methods (`pick_gcd` / `pick_ogcd` / `apply_cast`) plus whatever differs.

    A subclass sets `cooldowns`, `timing` (a GcdTiming preset), the anchor
    tuples, and implements `init_state` + the pickers. The GCD-timing and
    weave-budget defaults defer to `self.timing`, so an instant-GCD job needs to
    touch neither; a reduced-GCD window (MCH Overheated, RPR Enshroud) is a
    `gcd_duration` override keyed on the picked GCD."""

    cooldowns: dict[int, tuple[float, int]] = {}
    timing = None  # set by subclass to a GcdTiming preset
    agnostic_anchors: tuple[int, ...] = ()
    buff_anchors: tuple[int, ...] = ()
    canonical_anchors: tuple[int, ...] = ()
    # A potting job sets this to its `tincture.TinctureSpec`; the slot loop then places
    # in-sim pot markers (`_maybe_pot`). `None` (the default) → no marker, byte-identical.
    tincture_spec = None
    # Mit-plan "locked" heal-GCD windows (`jobs/_core/heal_locks.LockedGcdWindow`
    # tuples), set per-run by a healer model's constructor from its sim_context.
    # Model attribute (not a SimParams field) so `sweep_params`/`refine`, which
    # construct fresh params, can never drop them. Empty () → the engine's lock
    # scheduler never runs → byte-identical for every non-locked run.
    locked_gcd_windows: tuple = ()

    def resolve_locked_gcd(self, state: SimStateBase, ability_id: int) -> int:
        """The ability actually cast to honor a locked window — a model
        substitutes when the planned button is infeasible right now (WHM: a
        Rapture lock with no lily at the deadline casts Medica III instead)."""
        return ability_id

    def lock_satisfiers(self, ability_id: int) -> frozenset:
        """The cast ids that retire a locked window for `ability_id`. Lets the
        model's own voluntary casts count toward the plan (WHM: a Solace spends
        the same lily a planned Rapture would) — locks are count-in-window
        obligations, not exact scripts."""
        return frozenset((ability_id,))

    def prepull(self, state: SimStateBase, params) -> None:
        """Pre-pull setup, run once before the loop. May set gauge/buff state,
        emit a pre-pull channel cast (append to `state.timeline` at a small
        negative `t` so it shows in the pre-zone), and set `state.t` to the
        in-fight loop start (a melee engage delay, or the GCD a precast rolled).
        Takes `params` so the channel decision can be a swept axis. No-op by
        default — the loop starts at `state.t == 0` with no pre-pull action."""
        return None

    def gcd_slot(self, state: SimStateBase, params) -> tuple[int, float]:
        gcd_id = self.pick_gcd(state, params)
        return gcd_id, self.gcd_duration(state, gcd_id, params)

    def gcd_candidates(self, state: SimStateBase, params) -> list[int]:
        """The GCD choices `beam_search` should explore at this slot. Default: the
        single greedy pick — so beam search at any width reproduces `run_rotation`
        for a job that doesn't override this (i.e. every job until it opts into the
        search). A job overrides this to expose every legal/strategic option (not a
        greedy preference) and lets the search find the optimum."""
        return [self.pick_gcd(state, params)]

    def beam_prune(self, state: SimStateBase, score_fn, buff_intervals) -> float:
        """Beam-search top-K key. Default = the exact score of the partial timeline.

        A job with a **delayed reward** — value already committed but not yet on the
        timeline (banked gauge heading into a finisher, an in-progress DoT/combo) —
        overrides this to add an *admissible* (upper-bound) credit for that pending
        value, so a line that invests in it isn't pruned before it pays off. The
        FINAL selection always uses the exact `score_fn`, so the credit only steers
        survival, never the result."""
        return score_fn(state.timeline, self.final_aux(state), buff_intervals)

    def beam_signature(self, state: SimStateBase):
        """Optional **diversity-dedup key** for `beam_search`: when two surviving
        beams report the same signature, only the higher-scoring one is kept, so the
        fixed width holds that many *distinct* lines instead of near-duplicates of
        the locally-best one (the fix for beam *diversity collapse*). Return `None`
        (the default) to disable dedup — a job stays byte-identical until it opts in.
        A forking job returns a hashable of its future-relevant state."""
        return None

    def gcd_duration(self, state: SimStateBase, gcd_id: int, params) -> float:
        return self.timing.duration(state, gcd_id, params)

    def weave_budget(self, state: SimStateBase, gcd_id: int, params) -> int:
        return self.timing.weave_budget(state, gcd_id, params)

    def on_downtime_window(self, state: SimStateBase,
                           win_start: float, win_end: float) -> None:
        return None

    def should_pot(self, state: SimStateBase, params) -> bool:
        """Placement *preference* for the in-sim tincture, asked by `_maybe_pot` once
        the hard gates (cooldown ready, in uptime, fight time remaining, not forbidden)
        already pass. Default: pop the opener pot at the first eligible slot, then HOLD
        each later pot to the next 2-minute burst boundary — where both the job's own
        burst and the raid burst land — falling back to ASAP when no full burst window
        remains. Buff-window-free, so it places the pot well even in the raid-agnostic
        strict scenario; the buff-aware `refine` pass fine-aligns it to the actual raid
        window on top. A job overrides to pin pots to a bespoke burst signal."""
        spec = self.tincture_spec
        if state.tincture_used == 0:
            return True
        next_burst = math.ceil(state.t / _BURST_CADENCE_S) * _BURST_CADENCE_S
        if spec is not None and next_burst <= state.fight_duration_s - 0.5 * spec.duration_s:
            return state.t >= next_burst
        return True

    def pot_mult_at(self, state: SimStateBase, x: float) -> float:
        """The in-sim tincture multiplier covering time `x`, reconstructed from the
        live tincture state (`tincture_cd_ready` − cooldown = the latest pot's time) —
        what lets a job keep its incremental `exact_g` exact once the sim pots: the
        canonical scorer folds the in-timeline marker into a multiplier window even
        on the buff-agnostic path, so a flat sum diverges the moment the pot drops.
        Only the LATEST pot can cover any snapshot an incremental score asks about
        (the cooldown spaces windows apart by far more than the duration, and every
        contribution lands within ~one payload-lookback of `state.t`), so no pot
        history is needed. 1.0 for a job that doesn't pot."""
        spec = self.tincture_spec
        if spec is None or spec.multiplier <= 1.0 or state.tincture_used == 0:
            return 1.0
        pot_t = state.tincture_cd_ready - spec.cooldown_s
        # Half-open [pot_t, pot_t + duration) — mirrors `multiplier_at`.
        return spec.multiplier if pot_t <= x < pot_t + spec.duration_s else 1.0

    def final_aux(self, state: SimStateBase) -> int:
        return 0

    # --- Exact-solver seam (jobs/_core/sim/optimal.py) ----------------------
    # A job opts into the provably-optimal DP+B&B solver by overriding these. The
    # defaults make `solve_optimal` a correct (if unaccelerated) exhaustive Pareto
    # DP for any job that already supplies a `beam_signature`; a job tightens it with
    # a dense move set, a monotone dominance split, an admissible bound, a fast
    # clone, and an O(1) incremental score. Every default keeps a non-opted job
    # byte-identical (nothing calls them unless the job routes through the solver).

    def legal_gcds(self, state: SimStateBase, params) -> list[int]:
        """Every LEGAL GCD at this slot — the dense move set the exact search
        enumerates (a superset of the greedy/beam candidates). Defaults to the beam's
        `gcd_candidates`; a job overrides to expose every legal move, not a greedy
        preference, so the DP can reach the optimum."""
        return self.gcd_candidates(state, params)

    def ogcd_candidates(self, state: SimStateBase, params) -> list:
        """The oGCD-weave choices the exact solver forks at one weave slot — each an
        ability id, or `None` to leave the slot empty (stop weaving). Default = the
        single greedy `pick_ogcd`, so `commit_gcd_states` yields exactly one successor
        (identical to `_commit_gcd`) and a job stays byte-identical until it forks its
        oGCD economy — RPR Blood-Stalk-now-vs-bank-for-Gluttony, MCH Queen battery
        timing — the decisions that don't live at the GCD level."""
        return [self.pick_ogcd(state, params)]

    def clone(self, state: SimStateBase) -> SimStateBase:
        """Branch a state for the search. Defaults to the engine's generic
        deepcopy-based clone; a job overrides with a field-aware shallow clone for
        speed in the hot DP loop."""
        return _clone_state(state)

    def dominance_key(self, state: SimStateBase):
        """The CATEGORICAL bucket for Pareto dominance: the part of the state that
        must match exactly for two partial lines to be comparable (rounded `t`, combo
        step, gauge category, armed-proc flags). Defaults to the full `beam_signature`
        — every field categorical, i.e. exact-state merging, always lossless. A job
        moves its monotone resources into `dominance_vector` for stronger pruning."""
        return self.beam_signature(state)

    def dominance_vector(self, state: SimStateBase) -> tuple:
        """The MONOTONE-GOOD resource vector for Pareto dominance, signed so a larger
        component is always at-least-as-good (more gauge/charges/buff-remaining →
        +value; a cooldown/DoT timer → negate the remaining so readier/longer is
        larger). Within a `dominance_key` bucket, A dominates B (B dropped) iff
        A.score ≥ B.score and A.vector ≥ B.vector componentwise. Default () → exact
        merging only (lossless, just less pruning)."""
        return ()

    def admissible_remaining(self, state: SimStateBase) -> float:
        """An admissible (never-underestimating) upper bound on the additional score
        reachable from `state` to the fight end, for the B&B prune (g + bound ≤
        incumbent → drop). Default +inf disables the bound prune — the Pareto DP stays
        exact, just slower; a job supplies a resource-gated bound to accelerate it."""
        return float("inf")

    def exact_g(self, state: SimStateBase, score_fn) -> float:
        """The EXACT score of the committed prefix (points banked so far, incl. any
        in-progress DoT credited by elapsed time), buff-aware via
        `state.buff_intervals`. Default re-scores the timeline; a job maintains it
        incrementally in `apply_cast` for an O(1) read. Must satisfy exact_g(prefix)
        + admissible_remaining(state) ≥ the terminal score of every completion."""
        return score_fn(state.timeline, self.final_aux(state),
                        state.buff_intervals or None)

    def terminal_g(self, state: SimStateBase, score_fn) -> float:
        """The score of a COMPLETE rotation ending at `state` — must equal the
        canonical scorer on the produced timeline under `state.buff_intervals` (the
        correctness gate the DP's final selection uses). Default = score_fn on the
        timeline."""
        return score_fn(state.timeline, self.final_aux(state),
                        state.buff_intervals or None)


# --- Downtime + forbidden-window helpers ------------------------------------

def in_downtime(t: float, downtime: list[tuple[float, float]]) -> bool:
    # Half-open: `s <= t < e`. Closing both ends would loop forever at t == e
    # since `next_uptime` would keep returning the same boundary.
    return any(s <= t < e for s, e in downtime)


def next_uptime(t: float, downtime: list[tuple[float, float]]) -> float:
    for s, e in downtime:
        if s <= t < e:
            return e
    return t


def containing_window(t: float, downtime: list[tuple[float, float]]
                      ) -> tuple[float, float] | None:
    for s, e in downtime:
        if s <= t < e:
            return (s, e)
    return None


def is_forbidden(ability_id: int, t: float,
                 forbidden_windows: tuple[tuple[int, float, float], ...]) -> bool:
    """True iff `ability_id` is blocked from firing at time `t`. Half-open
    `start <= t < end` for consistency with downtime windows."""
    for fid, start, end in forbidden_windows:
        if fid == ability_id and start <= t < end:
            return True
    return False


# --- Locked-GCD windows (healer mit-plan integration) -------------------------
# `model.locked_gcd_windows` (jobs/_core/heal_locks.LockedGcdWindow) are
# count-in-window obligations: cast N of ability X inside [start_s, end_s).
# Scheduling is lazy earliest-deadline-first — the greedy damage line runs
# until the remaining slots before a deadline are exactly enough to cover the
# remaining quotas, so a heal is never fired earlier than it must be on the
# greedy path (the beam may still choose to, via the appended candidate).
# Every helper is a no-op unless the model carries locks.

def _locks_init(model: RotationModel, state: SimStateBase) -> None:
    if model.locked_gcd_windows:
        state.lock_done = (0,) * len(model.locked_gcd_windows)


def _note_lock_satisfied(model: RotationModel, state: SimStateBase,
                         ability_id: int, cast_t: float) -> None:
    """Retire one pending lock quota covered by a GCD cast at `cast_t`
    (earliest-deadline window first). Voluntary casts count too — the plan's
    tolerance is count-in-window, not exact timestamps."""
    windows = model.locked_gcd_windows
    best = -1
    for i, lk in enumerate(windows):
        if state.lock_done[i] >= lk.count:
            continue
        if not (lk.start_s <= cast_t < lk.end_s):
            continue
        if ability_id not in model.lock_satisfiers(lk.ability_id):
            continue
        if best < 0 or lk.end_s < windows[best].end_s:
            best = i
    if best >= 0:
        done = list(state.lock_done)
        done[best] += 1
        state.lock_done = tuple(done)


def _forced_lock_pick(model: RotationModel, state: SimStateBase, params
                      ) -> tuple[int, float] | None:
    """`(ability_id, slot_s)` when a lock must fire NOW: cumulative EDF
    feasibility — for some deadline prefix, the remaining GCD slots (at the
    live, haste-aware slot length) just cover the remaining quotas. None while
    there is slack (the greedy damage pick proceeds)."""
    windows = model.locked_gcd_windows
    pending = [(lk.end_s, lk.start_s, i, lk) for i, lk in enumerate(windows)
               if state.lock_done[i] < lk.count and state.t < lk.end_s]
    if not pending:
        return None
    pending.sort(key=lambda p: (p[0], p[1]))
    first_open = next((lk for _e, _s, _i, lk in pending
                       if lk.start_s <= state.t), None)
    if first_open is None:
        return None                      # nothing castable yet
    resolved = model.resolve_locked_gcd(state, first_open.ability_id)
    slot_s = model.gcd_duration(state, resolved, params)
    required = 0
    for end_s, _s, i, lk in pending:
        required += lk.count - state.lock_done[i]
        if required >= int((end_s - state.t) / slot_s):
            return resolved, slot_s
    return None


def _lock_candidates(model: RotationModel, state: SimStateBase) -> list[int]:
    """The resolved lock abilities castable at `state.t` (open, unmet windows)
    — appended to the beam's fork set so the search can place a heal earlier
    than the lazy deadline when that scores better."""
    out: list[int] = []
    for i, lk in enumerate(model.locked_gcd_windows):
        if state.lock_done[i] >= lk.count:
            continue
        if not (lk.start_s <= state.t < lk.end_s):
            continue
        rid = model.resolve_locked_gcd(state, lk.ability_id)
        if rid not in out:
            out.append(rid)
    return out


def _satisfy_locks_in_downtime(model: RotationModel, state: SimStateBase,
                               params, win_start: float, win_end: float) -> None:
    """Place pending lock casts inside a downtime window — free by construction
    (no damage GCD is displaced; exactly what a real healer does). Paced at the
    live slot length; each cast must both meet its own deadline and finish
    inside the downtime window. Runs BEFORE the model's own `on_downtime_window`
    filler so the two compose (the model's filler starts from the advanced
    `state.t`)."""
    windows = model.locked_gcd_windows
    order = sorted(range(len(windows)),
                   key=lambda j: (windows[j].end_s, windows[j].start_s))
    for i in order:
        lk = windows[i]
        while state.lock_done[i] < lk.count:
            cast_t = max(state.t, win_start, lk.start_s)
            if cast_t >= min(win_end, lk.end_s):
                break
            advance_time(model, state, cast_t)
            resolved = model.resolve_locked_gcd(state, lk.ability_id)
            slot_s = model.gcd_duration(state, resolved, params)
            if cast_t + slot_s > win_end or cast_t + slot_s > lk.end_s:
                break
            model.apply_cast(state, resolved)
            done = list(state.lock_done)
            done[i] += 1
            state.lock_done = tuple(done)
            advance_time(model, state, cast_t + slot_s)


# --- Cast bookkeeping helpers (called from a model's apply_cast) ------------

def apply_cooldown(state: SimStateBase, cooldowns: dict[int, tuple[float, int]],
                   ability_id: int) -> None:
    """The generic cooldown/charge decrement shared by every job's apply_cast:
    a multi-charge ability spends one charge, a single-recast ability sets its
    ready-time. Called by the model at the cooldown step of its apply_cast."""
    if ability_id in cooldowns:
        recast, max_ch = cooldowns[ability_id]
        if max_ch > 1:
            state.charges[ability_id] = max(
                0, state.charges.get(ability_id, max_ch) - 1)
        else:
            state.cd_ready[ability_id] = state.t + recast


def advance_time(model: RotationModel, state: SimStateBase, new_t: float) -> None:
    """Move state.t forward, regenerating charges for multi-charge abilities."""
    delta = new_t - state.t
    if delta <= 0:
        state.t = new_t
        return
    for aid, (recast, max_ch) in model.cooldowns.items():
        if max_ch > 1 and aid in state.charges:
            state.charges[aid] = min(max_ch, state.charges[aid] + delta / recast)
    state.t = new_t


# --- The core loop ----------------------------------------------------------

def _maybe_pot(model: RotationModel, state: SimStateBase,
               params: SimParamsBase) -> None:
    """Place an in-sim tincture marker at the current slot when the model pots and the
    slot is eligible (pot off cooldown, in uptime, ≥1s of fight remaining, not held by
    a `forbidden_windows` entry, and the model's `should_pot` preference). The pot is
    an off-GCD instant: the `TINCTURE_ACTION_ID` marker is appended at `state.t` and
    does NOT consume a weave or advance time. No-op for a model with no `tincture_spec`
    → byte-identical. Shared by the greedy loop, the beam, and the exact solver so all
    three place the pot identically; scoring reads the marker as a self-buff window."""
    spec = getattr(model, "tincture_spec", None)
    if spec is None:
        return
    if state.t < state.tincture_cd_ready:
        return
    if state.fight_duration_s - state.t < 1.0:
        return
    if in_downtime(state.t, state.downtime_windows):
        return
    if is_forbidden(TINCTURE_ACTION_ID, state.t, params.forbidden_windows):
        return
    if not model.should_pot(state, params):
        return
    state.timeline.append((state.t, TINCTURE_ACTION_ID))
    state.tincture_cd_ready = state.t + spec.cooldown_s
    state.tincture_used += 1


def _commit_gcd(model: RotationModel, state: SimStateBase, params: SimParamsBase,
                gcd_id: int, gcd_duration: float) -> None:
    """Apply one GCD into the slot: fire it, weave oGCDs into the post-GCD window
    (3rd+ weave clips the slot by `triple_weave_clip_s`), then advance to the slot
    end. Shared by the greedy loop (`run_rotation`) and the beam-search solver so
    the two explore the same transition function. Mutates `state`."""
    _maybe_pot(model, state, params)
    weave_end = state.t + gcd_duration
    state.last_gcd_t = state.t
    model.apply_cast(state, gcd_id)

    budget = model.weave_budget(state, gcd_id, params)
    weaves_used = 0
    while weaves_used < budget:
        if state.t + WEAVE_DURATION_S > weave_end - 0.1:
            break
        ogcd_id = model.pick_ogcd(state, params)
        if ogcd_id is None:
            break
        model.apply_cast(state, ogcd_id)
        advance_time(model, state, state.t + WEAVE_DURATION_S)
        weaves_used += 1
        if weaves_used > 2:
            weave_end += params.triple_weave_clip_s

    advance_time(model, state, weave_end)


def commit_gcd_states(model: RotationModel, state: SimStateBase,
                      params: SimParamsBase, gcd_id: int, gcd_duration: float
                      ) -> list[SimStateBase]:
    """Exact-solver transition: commit one GCD into the slot, then enumerate every
    oGCD-weave fork the model exposes (`ogcd_candidates`), returning all end-of-slot
    states. Leaves `state` untouched (clones internally).

    The default `ogcd_candidates` ([pick_ogcd]) yields exactly ONE state — woven in
    place exactly like `_commit_gcd` — so a job is byte-identical until it forks its
    oGCD economy, and only a real fork pays for a clone. The greedy loop and the beam
    keep the cheaper in-place `_commit_gcd`; only `optimal.solve_optimal` branches the
    weaves (the GCD-perfect ceiling generalized to a true *action*-perfect one)."""
    root = model.clone(state)
    _maybe_pot(model, root, params)
    root.last_gcd_t = root.t
    model.apply_cast(root, gcd_id)
    weave_end = root.t + gcd_duration
    budget = model.weave_budget(root, gcd_id, params)

    done: list[SimStateBase] = []
    stack: list[tuple[SimStateBase, int, float]] = [(root, 0, weave_end)]
    while stack:
        st, used, wend = stack.pop()
        while True:
            if used >= budget or st.t + WEAVE_DURATION_S > wend - 0.1:
                advance_time(model, st, wend)
                done.append(st)
                break
            cands = model.ogcd_candidates(st, params)
            if len(cands) <= 1:                       # greedy/forced — weave in place
                oid = cands[0] if cands else None
                if oid is None:
                    advance_time(model, st, wend)
                    done.append(st)
                    break
                model.apply_cast(st, oid)
                used += 1
                if used > 2:
                    wend += params.triple_weave_clip_s
                advance_time(model, st, st.t + WEAVE_DURATION_S)
                continue
            for oid in cands:                         # real fork — clone per branch
                child = model.clone(st)
                if oid is None:
                    advance_time(model, child, wend)
                    done.append(child)
                else:
                    model.apply_cast(child, oid)
                    c_used = used + 1
                    c_wend = wend + (params.triple_weave_clip_s if c_used > 2 else 0.0)
                    advance_time(model, child, child.t + WEAVE_DURATION_S)
                    stack.append((child, c_used, c_wend))
            break
    return done


def _maybe_skip_downtime(model: RotationModel, state: SimStateBase,
                         downtime: list[tuple[float, float]],
                         params=None) -> bool:
    """If `state.t` is inside a downtime window, let the model squeeze a boundary
    action (MCH Flamethrower tick, RPR Soulsow re-arm, SAM Meditate) and jump to
    the window end. Returns True if a window was skipped. Shared by both solvers.
    Pending mit-plan locks are satisfied here first (free — no damage GCD
    displaced); `params` is only needed for that path."""
    if not in_downtime(state.t, downtime):
        return False
    win = containing_window(state.t, downtime)
    if win is not None:
        if model.locked_gcd_windows and params is not None:
            _satisfy_locks_in_downtime(model, state, params, win[0], win[1])
        model.on_downtime_window(state, win[0], win[1])
    advance_time(model, state, next_uptime(state.t, downtime))
    return True


def run_rotation(model: RotationModel, fight_duration_s: float,
                 downtime_windows: list[tuple[float, float]] | None,
                 params: SimParamsBase,
                 ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once with the model's greedy `gcd_slot` pick.
    Returns `(timeline, aux)` where `timeline` is `[(cast_time_s, ability_id)]`
    sorted by time and `aux` is the model's opaque scalar (`final_aux`). This is
    `beam_search` at width 1 with a single greedy candidate."""
    downtime = downtime_windows or []
    state = model.init_state()
    state.fight_duration_s = fight_duration_s
    state.downtime_windows = downtime
    _locks_init(model, state)
    model.prepull(state, params)

    locks = bool(model.locked_gcd_windows)
    iters = 0
    while state.t < fight_duration_s and iters < _MAX_ITERS:
        iters += 1
        if _maybe_skip_downtime(model, state, downtime, params):
            continue
        if locks:
            forced = _forced_lock_pick(model, state, params)
            if forced is not None:
                cast_t = state.t
                _commit_gcd(model, state, params, forced[0], forced[1])
                _note_lock_satisfied(model, state, forced[0], cast_t)
                continue
        # `gcd_slot` returns the chosen GCD + its slot duration (the model owns the
        # timing decision so reduced-GCD windows and any pre-pick flag capture stay
        # exact).
        gcd_id, gcd_duration = model.gcd_slot(state, params)
        _commit_gcd(model, state, params, gcd_id, gcd_duration)
        if locks:
            _note_lock_satisfied(model, state, gcd_id, state.last_gcd_t)

    return state.timeline, model.final_aux(state)


# --- GCD-perfect search (beam) ----------------------------------------------

ScoreFn = Callable[[list[tuple[float, int]], int,
                    Optional[list[tuple[float, float, float]]]], float]


def _clone_state(state: SimStateBase) -> SimStateBase:
    """Fast clone for beam search: deep-copy the (small) gauge/flag/cooldown state
    but reference-copy the append-only timeline (its tuples are immutable) and
    share the constant downtime list — so cloning stays O(state), not O(timeline),
    even as the rotation grows."""
    tl = state.timeline
    dt = state.downtime_windows
    bi = state.buff_intervals
    state.timeline = []
    state.downtime_windows = []
    state.buff_intervals = []
    try:
        new = copy.deepcopy(state)
    finally:
        state.timeline = tl
        state.downtime_windows = dt
        state.buff_intervals = bi
    new.timeline = list(tl)        # reference-copy (immutable tuples)
    new.downtime_windows = dt      # constant — share
    new.buff_intervals = bi        # constant — share
    return new


def beam_search(model: RotationModel, score_fn: "ScoreFn",
                fight_duration_s: float,
                downtime_windows: list[tuple[float, float]] | None,
                params: SimParamsBase,
                width: int,
                buff_intervals: list[tuple[float, float, float]] | None = None,
                ) -> tuple[list[tuple[float, int]], int]:
    """GCD-perfect rotation search: explore the GCD decision tree keeping the
    top-`width` partial rotations and returning the highest *true*-scoring one.
    Strictly generalizes `run_rotation` (width 1 + the default single greedy
    candidate reproduces it exactly). A job participates by overriding
    `gcd_candidates` to expose its options. oGCD weaves stay greedy.

    Pruning (top-K) uses `model.beam_prune` (default = the exact score); a job with
    a delayed reward overrides it to keep investing lines alive. The *final* pick
    always uses the exact `score_fn`. `buff_intervals` threads the raid-buff overlay
    into both."""
    downtime = downtime_windows or []
    root = model.init_state()
    root.fight_duration_s = fight_duration_s
    root.downtime_windows = downtime
    # Expose the raid-buff overlay on the state so a model's *picker* can see it
    # (e.g. MCH banks Queen battery toward a buff window). Cloned beams inherit it
    # (`_clone_state`). Pickers that ignore it stay byte-identical; the agnostic
    # beam (`buff_intervals is None`) leaves it the empty default.
    root.buff_intervals = buff_intervals or []
    _locks_init(model, root)
    model.prepull(root, params)
    locks = bool(model.locked_gcd_windows)

    def _prune(st: SimStateBase) -> float:
        return model.beam_prune(st, score_fn, buff_intervals)

    # Each beam carries its state + its prune score (the top-K key).
    beams: list[tuple[SimStateBase, float]] = [(root, _prune(root))]
    iters = 0
    while iters < _MAX_ITERS:
        iters += 1
        successors: list[tuple[SimStateBase, float]] = []
        active = False
        for state, sc in beams:
            if state.t >= fight_duration_s:
                successors.append((state, sc))            # finished — carry forward
                continue
            active = True
            if _maybe_skip_downtime(model, state, downtime, params):
                successors.append((state, _prune(state)))
                continue
            forced = _forced_lock_pick(model, state, params) if locks else None
            if forced is not None:
                # A lock deadline binds — no fork, the heal is the only move.
                cands = [forced[0]]
            else:
                cands = model.gcd_candidates(state, params)
                if not cands:
                    cands = [model.pick_gcd(state, params)]
                if locks:
                    # Offer the open locks as extra forks so the search can
                    # place a heal earlier than the lazy deadline when that
                    # scores better (e.g. a Rapture feeding a buffed Misery).
                    for rid in _lock_candidates(model, state):
                        if rid not in cands:
                            cands.append(rid)
            if len(cands) == 1:
                # No fork — advance this beam in place (no clone needed).
                cast_t = state.t
                dur = model.gcd_duration(state, cands[0], params)
                _commit_gcd(model, state, params, cands[0], dur)
                if locks:
                    _note_lock_satisfied(model, state, cands[0], cast_t)
                successors.append((state, _prune(state)))
            else:
                for cand in cands:
                    child = _clone_state(state)
                    cast_t = child.t
                    dur = model.gcd_duration(child, cand, params)
                    _commit_gcd(model, child, params, cand, dur)
                    if locks:
                        _note_lock_satisfied(model, child, cand, cast_t)
                    successors.append((child, _prune(child)))
        if not active:
            break
        successors.sort(key=lambda b: b[1], reverse=True)
        # Diversity dedup (opt-in via `beam_signature`): keep the best beam per
        # signature so the width holds distinct lines, not near-duplicates. A model
        # returning None (the default) keeps every successor → byte-identical.
        kept: list[tuple[SimStateBase, float]] = []
        seen: set = set()
        for st_sc in successors:
            sig = model.beam_signature(st_sc[0])
            if sig is not None:
                if locks:
                    # Two beams with different lock progress are NOT the same
                    # position — one still owes heals the other already paid.
                    sig = (sig, st_sc[0].lock_done)
                if sig in seen:
                    continue
                seen.add(sig)
            kept.append(st_sc)
            if len(kept) >= max(1, width):
                break
        beams = kept

    best = max(beams, key=lambda b: score_fn(
        b[0].timeline, model.final_aux(b[0]), buff_intervals))
    return best[0].timeline, model.final_aux(best[0])


# --- Parameter sweep --------------------------------------------------------


def sweep_best(model: RotationModel, score_fn: ScoreFn,
               fight_duration_s: float,
               downtime_windows: list[tuple[float, float]] | None,
               extra_forbidden: tuple[tuple[int, float, float], ...] = (),
               buff_intervals: list[tuple[float, float, float]] | None = None,
               ) -> tuple[list[tuple[float, int]], int, SimParamsBase, float]:
    """Try each param combination the model declares, optionally with extra
    forbidden windows applied, and return the best-scoring
    `(timeline, aux, params, score)`. `buff_intervals` makes the score
    buff-aware so the sweep prefers params that pay off under the pull's
    raid-buff windows. First max wins on ties (strict `>`), so the model's
    `sweep_params` order is significant."""
    best: tuple[list[tuple[float, int]], int, SimParamsBase, float] | None = None
    for params in model.sweep_params(extra_forbidden):
        timeline, aux = run_rotation(model, fight_duration_s, downtime_windows, params)
        score = score_fn(timeline, aux, buff_intervals)
        if best is None or score > best[3]:
            best = (timeline, aux, params, score)
    assert best is not None  # sweep set is non-empty
    return best


# --- Local-search refinement (the "perfect" sim) ----------------------------

def alignment_delays(cast_t: float,
                     buff_intervals: list[tuple[float, float, float]] | None
                     ) -> list[float]:
    """Extra 'hold until the next buff window' delays for an anchor at `cast_t`:
    the delay needed to land it at each upcoming window start within the
    horizon (empty when buff-agnostic)."""
    if not buff_intervals:
        return []
    return [start - cast_t for start, _e, _m in buff_intervals
            if 0.1 < start - cast_t <= _ALIGN_HORIZON_S]


def refine(model: RotationModel, score_fn: ScoreFn,
           base_params: SimParamsBase,
           initial_forbidden: list[tuple[int, float, float]],
           fight_duration_s: float,
           downtime_windows: list[tuple[float, float]] | None,
           buff_intervals: list[tuple[float, float, float]] | None,
           anchors: tuple[int, ...],
           max_iterations: int,
           ) -> tuple[list[tuple[float, int]], int,
                      list[tuple[int, float, float]], float]:
    """Hill-climb from a starting (params, forbidden) point. Each step tries
    small timing nudges plus — when buff-aware — alignment holds on every
    anchor, accepts the first improving move (scored under `buff_intervals`),
    and restarts the walk. Returns (timeline, aux, forbidden, score).

    Seeding a buff-aware pass with a buff-agnostic result's `forbidden` makes
    the buffed ceiling provably >= that result scored under buffs — the buff
    awareness can only add value, never regress it."""
    # In the buff-aware pass, also let the search hold the in-sim tincture marker, so
    # the pot fine-aligns to the actual raid window (the cadence rule already placed it
    # near the 2-min burst; `alignment_delays` nudges it onto the real window start).
    if getattr(model, "tincture_spec", None) and buff_intervals:
        anchors = (*anchors, TINCTURE_ACTION_ID)
    forbidden = list(initial_forbidden)
    timeline, aux = run_rotation(
        model, fight_duration_s, downtime_windows,
        replace(base_params, forbidden_windows=tuple(forbidden)))
    best_score = score_fn(timeline, aux, buff_intervals)

    for _iteration in range(max_iterations):
        improved = False
        # Burst anchors in the current timeline, latest first (end-of-fight
        # constraints are strongest, so refine those decisions first).
        cast_anchors = sorted(
            [(t, aid) for t, aid in timeline if aid in anchors],
            key=lambda x: -x[0])
        for cast_t, cast_id in cast_anchors:
            delays = list(_PERFECT_DELAY_OPTIONS) + alignment_delays(
                cast_t, buff_intervals)
            for delay in delays:
                trial_forbidden = (*forbidden, (cast_id, cast_t, cast_t + delay))
                trial_tl, trial_aux = run_rotation(
                    model, fight_duration_s, downtime_windows,
                    replace(base_params, forbidden_windows=trial_forbidden))
                trial_score = score_fn(trial_tl, trial_aux, buff_intervals)
                if trial_score > best_score + 1e-3:
                    timeline, aux, best_score = trial_tl, trial_aux, trial_score
                    forbidden = list(trial_forbidden)
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    return timeline, aux, forbidden, best_score


def perfect(model: RotationModel, score_fn: ScoreFn,
            fight_duration_s: float,
            downtime_windows: list[tuple[float, float]] | None = None,
            buff_intervals: list[tuple[float, float, float]] | None = None,
            ) -> tuple[list[tuple[float, int]], int]:
    """Perfect simulator: parameter sweep + local-search refinement.

    Without `buff_intervals` this is the buff-agnostic ceiling: sweep, then
    refine the agnostic anchors. With `buff_intervals` it runs that agnostic
    refinement first, then a second buff-aware pass **seeded from it** that adds
    alignment holds — guaranteeing the buffed ceiling never falls below the
    agnostic timeline scored under the same buffs."""
    _tl, _aux, base_params, _score = sweep_best(
        model, score_fn, fight_duration_s, downtime_windows)

    timeline, aux, forbidden, _ = refine(
        model, score_fn, base_params, [], fight_duration_s, downtime_windows,
        buff_intervals=None, anchors=model.agnostic_anchors,
        max_iterations=_PERFECT_MAX_ITERATIONS)
    if not buff_intervals:
        return timeline, aux

    timeline, aux, _forbidden, _ = refine(
        model, score_fn, base_params, forbidden, fight_duration_s,
        downtime_windows, buff_intervals=buff_intervals,
        anchors=model.buff_anchors, max_iterations=_BUFF_MAX_ITERATIONS)
    return timeline, aux


def beam_perfect(model: RotationModel, score_fn: ScoreFn,
                 fight_duration_s: float,
                 downtime_windows: list[tuple[float, float]] | None = None,
                 buff_intervals: list[tuple[float, float, float]] | None = None,
                 width: int = 1,
                 ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: the local-search burst-timing refinement (`perfect`)
    PLUS a beam search over the model's GCD forks on top of the refined timing —
    the two optimize orthogonal axes (oGCD burst alignment vs. GCD-level choices
    like SAM's Higanbana-vs-Midare), so a true optimum needs both.

    `width == 1` (or a model with no `gcd_candidates` override) reduces this to
    `perfect` exactly. The result is guarded to never fall below `perfect`'s own
    refined ceiling, so beam search can only improve on it."""
    downtime = downtime_windows or []
    _tl, _aux, base_params, _ = sweep_best(model, score_fn, fight_duration_s, downtime)
    timeline, aux, forbidden, base_score = refine(
        model, score_fn, base_params, [], fight_duration_s, downtime,
        buff_intervals=None, anchors=model.agnostic_anchors,
        max_iterations=_PERFECT_MAX_ITERATIONS)
    if buff_intervals:
        timeline, aux, forbidden, base_score = refine(
            model, score_fn, base_params, forbidden, fight_duration_s, downtime,
            buff_intervals=buff_intervals, anchors=model.buff_anchors,
            max_iterations=_BUFF_MAX_ITERATIONS)

    refined_params = replace(base_params, forbidden_windows=tuple(forbidden))
    # Beam over the model's GCD forks. The refinement optimizes the GREEDY line's
    # oGCD burst *timing* (forbidden windows); when the beam restructures the GCDs
    # those holds can over-constrain the search, so also run the beam UNCONSTRAINED
    # (base_params) and keep the best — the two are orthogonal and the unconstrained
    # beam can be strictly better. (The extra run only happens when refine actually
    # added holds; otherwise refined_params == base_params.) Guarded to never fall
    # below the refined greedy ceiling.
    best_tl, best_aux, best_score = timeline, aux, base_score
    param_sets = [refined_params]
    if refined_params.forbidden_windows:
        param_sets.append(base_params)
    for params in param_sets:
        cand_tl, cand_aux = beam_search(model, score_fn, fight_duration_s, downtime,
                                        params, width, buff_intervals)
        cand_score = score_fn(cand_tl, cand_aux, buff_intervals)
        if cand_score > best_score:
            best_tl, best_aux, best_score = cand_tl, cand_aux, cand_score
    # Buff-aware: also try the canonical-anchor alignment (the 2-min burst forced into
    # the raid windows) and max-guard. `refine`'s local delay-nudge can't phase a
    # sub-120s-cadence burst (DRG/PLD 60s Geirskogul/FoF) into the irregular observed
    # windows — it would have to hold alternate bursts coordinately, which the greedy
    # hill-climb sees as a net-negative per-burst delay — so the buffed ceiling
    # otherwise under-credits the alignment a real top parse achieves. No-op without
    # buffs (the strict ceiling is unchanged) or without `canonical_anchors`;
    # max-guarded so it can only raise the ceiling, never regress it.
    if buff_intervals:
        best_tl, best_aux = canonical_aligned_max_guard(
            model, score_fn, fight_duration_s, downtime, buff_intervals,
            (best_tl, best_aux), beam_width=(width if width > 1 else None))
    return best_tl, best_aux


# --- Canonical (buff-window-aligned) variant --------------------------------

def canonical_burst_forbidden(
        buff_intervals: list[tuple[float, float, float]],
        anchors: tuple[int, ...],
        hold_s: float = _CANONICAL_HOLD_S,
        cooldowns: dict[int, tuple[float, int]] | None = None,
        ) -> tuple[tuple[int, float, float], ...]:
    """Forbidden-window entries that hold each anchor until the start of every
    full-stack buff segment, so it's cast in-window. Targets the
    highest-multiplier segments (where the canonical burst belongs).

    Each anchor's hold is capped at its OWN recast (`cooldowns`), so one window's
    forbid range `[w-hold, w]` absorbs at most ONE cast of it. Without the cap a
    sub-120s-cadence burst (DRG Geirskogul / Lance Charge, PLD Fight or Flight — all
    60s) merges its off-minute cast into the window: at `hold_s=110` the range spans
    ~two 60s casts, so the earlier one is held all the way to the window too (dropped,
    not just delayed) → throughput loss → the `max_guard` rejects the whole aligned
    line and the ceiling never aligns. At `hold <= recast` the off-minute cast (one
    recast before the held one) provably sits outside `[w-hold, w]`. A 120s-recast
    anchor keeps the full 110s hold → byte-identical for the 2-min-burst jobs."""
    if not buff_intervals:
        return ()
    max_mult = max(m for _s, _e, m in buff_intervals)
    targets = [s for s, _e, m in buff_intervals if m >= max_mult - 1e-6]
    fw: list[tuple[int, float, float]] = []
    for w in targets:
        for aid in anchors:
            h = hold_s
            if cooldowns and aid in cooldowns:
                h = min(h, cooldowns[aid][0])
            start = max(0.0, w - h)
            if w > start:
                fw.append((aid, start, w))
    return tuple(fw)


# --- Buff-aware spend-timing primitives (shared by snapshot jobs) -----------
# A snapshot spend (MCH Queen at summon, SAM/WHM DoT at application) buffs its
# whole payload by the multiplier at the spend instant. These let a model's
# picker shift that spend toward a raid window, bounded by the job's own waste
# term (MCH overcap, SAM/WHM DoT-clip). All take the raid-buff overlay the engine
# now exposes on `state.buff_intervals` (set in `beam_search`).

def in_top_window(t: float,
                  buff_intervals: list[tuple[float, float, float]] | None) -> bool:
    """True iff `t` sits inside a highest-multiplier raid-buff segment (where a
    snapshot spend captures the full party burst). False with no buffs."""
    if not buff_intervals:
        return False
    m_max = max(m for _s, _e, m in buff_intervals)
    return multiplier_at(t, buff_intervals) >= m_max - 1e-6


def reachable_richer_window(
        t: float,
        buff_intervals: list[tuple[float, float, float]] | None,
        max_lead_s: float) -> float | None:
    """The soonest higher-multiplier window start within `max_lead_s` ahead of
    `t`, or None — already in a top window, none ahead, or the next is past the
    reach `max_lead_s` (the caller's waste budget: MCH overcap headroom, a DoT's
    acceptable clip). A model holds/banks its spend toward the returned start."""
    if not buff_intervals:
        return None
    m_now = multiplier_at(t, buff_intervals)
    m_max = max(m for _s, _e, m in buff_intervals)
    if m_now >= m_max - 1e-6:
        return None
    nxt = min((s for s, _e, m in buff_intervals
               if s > t and m > m_now + 1e-6), default=None)
    if nxt is None or nxt - t > max_lead_s:
        return None
    return nxt


def canonical_aligned_max_guard(
        model: RotationModel, score_fn: ScoreFn,
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None,
        buff_intervals: list[tuple[float, float, float]] | None,
        base: tuple[list[tuple[float, int]], int],
        *, beam_width: int | None = None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Given a job's free buff-aware optimum `base`, also evaluate the
    canonical-anchor-aligned variant (the model's `canonical_anchors` forced into
    the raid windows) and return whichever scores higher. The free optimizer
    under-searches the burst hold — `refine` decides anchor timing via
    `run_rotation`, where snapshot-spend banking is off — so a forced variant can
    win; the max guard means it never regresses, and since this only raises a
    ceiling it can never push efficiency over 100%.

    Solver mirrors the job's own: `beam_search` at `beam_width` for a forking job
    (MCH/PLD/SAM/WHM), else `run_rotation` (RDM/WAR/RPR). No-op without buffs or
    `canonical_anchors`."""
    if not buff_intervals or not model.canonical_anchors:
        return base
    downtime = downtime_windows or []
    fw = canonical_burst_forbidden(buff_intervals, model.canonical_anchors,
                                   cooldowns=model.cooldowns)
    _tl, _aux, base_params, _ = sweep_best(
        model, score_fn, fight_duration_s, downtime, buff_intervals=buff_intervals)
    params = replace(base_params, forbidden_windows=fw)
    if beam_width:
        aligned = beam_search(model, score_fn, fight_duration_s, downtime,
                              params, beam_width, buff_intervals)
    else:
        aligned = run_rotation(model, fight_duration_s, downtime, params)
    if score_fn(*aligned, buff_intervals) > score_fn(*base, buff_intervals):
        return aligned
    return base


def canonical_aligned(model: RotationModel, score_fn: ScoreFn,
                      fight_duration_s: float,
                      downtime_windows: list[tuple[float, float]] | None = None,
                      buff_intervals: list[tuple[float, float, float]] | None = None,
                      ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows
    (the canonical 'hold for the window' line). Falls back to the throughput
    optimum when there are no party buffs to align to."""
    downtime = downtime_windows or []
    if not buff_intervals:
        return perfect(model, score_fn, fight_duration_s, downtime)
    _tl, _aux, base_params, _score = sweep_best(
        model, score_fn, fight_duration_s, downtime, buff_intervals=buff_intervals)
    fw = canonical_burst_forbidden(buff_intervals, model.canonical_anchors,
                                   cooldowns=model.cooldowns)
    return run_rotation(
        model, fight_duration_s, downtime,
        replace(base_params, forbidden_windows=fw))
