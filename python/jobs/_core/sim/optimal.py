"""Provably-optimal idealized-rotation solver — exact DP + branch-and-bound.

The beam search (`engine.beam_search`) keeps the top-K partial rotations per GCD
slot and discards the rest: a bounded-memory heuristic that can prune a line which
is temporarily behind but would have won (diversity collapse), so a real top parse
can beat the ceiling (>100%). This module instead computes the **provable model
optimum** for the jobs that opt in, so the ceiling is a true upper bound and any
remaining over-100% is *definitively* a fidelity issue, not search.

It is a job-agnostic generalization of the SAM exact-solver prototype
(`scripts/solve_samurai_optimal.py`):

  * **Transition** = the same `engine._commit_gcd` the greedy loop and the beam use
    (fire the GCD, greedily weave oGCDs, advance time) — so oGCD weaving stays greedy
    and the search isolates the GCD-level decision. One source of truth for the
    rules: the job's imperative `apply_cast`, never re-encoded as constraints.
  * **Forward layered DP** over GCD slots: states are processed in increasing time
    order (every GCD strictly advances `t`, so time is a topological order on the
    decision DAG) and each distinct state is expanded once.
  * **Pareto dominance** (the convergence lever the prototype lacked): within a
    `dominance_key` bucket, drop a state another dominates on (score, monotone
    resource vector). Lossless — the dominator can replay the dropped line's
    completion for ≥ score — and it collapses the otherwise ~10M-state frontier.
  * **Branch-and-bound**: an admissible `admissible_remaining` upper bound prunes a
    state that can't beat the incumbent (seeded from the beam). Pure acceleration —
    correctness rides on the Pareto DP alone (the default +inf bound is still exact).
  * **Buff/tincture-aware**: the objective is scored *inside* the sim via
    `state.buff_intervals` + the model's in-state self-buffs/tincture, so burst
    alignment and pot timing are decisions the optimizer makes — not a multiplier
    applied to a finished timeline (which is what broke scoring with the >100% guard).
  * **Time-box + `proven`**: returns `proven=True` when the frontier is exhausted
    (the result is the provable optimum), `False` on time-out (best complete leaf —
    a lower bound; the caller guards with the beam so a pull never regresses).

A job opts in via the `RotationModel` seam (`legal_gcds`, `dominance_key`,
`dominance_vector`, `admissible_remaining`, `exact_g`, `terminal_g`, `clone`); the
defaults on `BaseRotationModel` keep every other job untouched.
"""
from __future__ import annotations

import time
from typing import Optional

from jobs._core.sim import engine
from jobs._core.sim.engine import (
    RotationModel,
    SimParamsBase,
    SimStateBase,
    advance_time,
    containing_window,
    in_downtime,
    next_uptime,
)


def _dominates(a: tuple, b: tuple) -> bool:
    """Componentwise `a >= b` (a larger component is at-least-as-good; the model
    signs its vector accordingly). Empty vectors → always True, so a job that
    declares no `dominance_vector` collapses each bucket to its single
    highest-score state (exact-state merging)."""
    return all(x >= y for x, y in zip(a, b))


class _ParetoFront:
    """The non-dominated `(vector, g, state)` points within one `dominance_key`
    bucket. Insert keeps the frontier minimal: a candidate dominated by a kept
    point is rejected; kept points it dominates are removed. Frontier size is the
    number of incomparable resource trade-offs — small in practice, which is what
    makes the DP converge."""

    __slots__ = ("pts",)

    def __init__(self) -> None:
        self.pts: list[tuple[tuple, float, SimStateBase]] = []

    def add(self, vec: tuple, g: float, state: SimStateBase) -> bool:
        for v2, g2, _s in self.pts:
            if g2 >= g and _dominates(v2, vec):
                return False                       # dominated by an existing point
        self.pts = [p for p in self.pts
                    if not (g >= p[1] and _dominates(vec, p[0]))]
        self.pts.append((vec, g, state))
        return True


def _settle_downtime(model: RotationModel, state: SimStateBase,
                     downtime: list[tuple[float, float]]) -> None:
    """Advance a state through any boss-untargetable window it has entered (the
    model squeezes a boundary action, then we jump to the window end), mirroring the
    greedy loop's `_maybe_skip_downtime` but looping until targetable."""
    while in_downtime(state.t, downtime):
        win = containing_window(state.t, downtime)
        if win is not None:
            model.on_downtime_window(state, win[0], win[1])
        advance_time(model, state, next_uptime(state.t, downtime))


def solve_optimal(model: RotationModel, score_fn, fight_duration_s: float,
                  downtime_windows: Optional[list[tuple[float, float]]],
                  params: SimParamsBase, *,
                  buff_intervals: Optional[list[tuple[float, float, float]]] = None,
                  incumbent: float = float("-inf"),
                  time_box: Optional[float] = None,
                  stats: Optional[dict] = None,
                  ) -> tuple[list[tuple[float, int]], int, bool]:
    """Exact GCD-perfect rotation via forward layered DP + Pareto dominance + B&B.

    Returns `(timeline, aux, proven)`. `incumbent` seeds the bound prune (pass the
    beam/greedy score for strong pruning). `buff_intervals` is threaded onto the
    state so the model's buff-aware scoring runs inside the sim. `proven` is True iff
    the frontier was exhausted within `time_box` (then the result is the provable
    optimum). `stats`, if given, is filled with `nodes`/`leaves`/`states` counts."""
    # Mit-plan locked-GCD windows (healer runs) are not modeled by the exact
    # solver: its transitions never consult the lock scheduler, so a "proven"
    # result would silently skip the owed heals. No healer routes here today
    # (WHM is beam-only); fail loudly rather than over-prove.
    if getattr(model, "locked_gcd_windows", ()):
        raise NotImplementedError(
            "solve_optimal does not support locked_gcd_windows")
    downtime = downtime_windows or []
    start = time.monotonic()

    root = model.init_state()
    root.fight_duration_s = fight_duration_s
    root.downtime_windows = downtime
    root.buff_intervals = list(buff_intervals) if buff_intervals else []
    model.prepull(root, params)
    _settle_downtime(model, root, downtime)

    # layers[round(t,3)][dominance_key] -> _ParetoFront, processed in increasing t.
    layers: dict[float, dict[object, _ParetoFront]] = {}
    best_leaf_g = float("-inf")
    best_leaf_state: Optional[SimStateBase] = None
    nodes = leaves = states = 0
    # Diagnostics (accuracy-neutral): how many states each mechanism discards —
    # `pruned_bound` by the B&B admissible bound, `pruned_dominance` by Pareto
    # dominance — plus the peak per-time-layer frontier width. Reported in `stats`
    # so a bound/dominance change can be MEASURED, not assumed (bench_*_dp.py).
    pruned_bound = pruned_dominance = max_layer_width = 0
    timed_out = False

    def consider(state: SimStateBase) -> None:
        nonlocal best_leaf_g, best_leaf_state, leaves, states
        nonlocal pruned_bound, pruned_dominance
        if state.t >= fight_duration_s:
            leaves += 1
            tg = model.terminal_g(state, score_fn)
            if tg > best_leaf_g:
                best_leaf_g, best_leaf_state = tg, state
            return
        g = model.exact_g(state, score_fn)
        # B&B bound prune (lossless): a subtree that cannot reach the best complete
        # rotation we already hold (or the seed incumbent) is discarded. Strict, so a
        # line that can still *tie* the floor survives — the optimum is never pruned.
        floor = incumbent if incumbent > best_leaf_g else best_leaf_g
        if g + model.admissible_remaining(state) < floor - 1e-6:
            pruned_bound += 1
            return
        tkey = round(state.t, 3)
        bucket = layers.get(tkey)
        if bucket is None:
            bucket = layers[tkey] = {}
        dkey = model.dominance_key(state)
        front = bucket.get(dkey)
        if front is None:
            front = bucket[dkey] = _ParetoFront()
        if front.add(model.dominance_vector(state), g, state):
            states += 1
        else:
            pruned_dominance += 1

    consider(root)

    # Forward LAYERED processing (increasing `t`), NOT best-first. Best-first's only
    # lever is reaching a tight incumbent floor sooner so the B&B bound prunes more —
    # but a zero-code probe (run the solve with incumbent = the TIGHTEST possible floor,
    # the optimum itself, vs the beam seed) showed the surviving-state count is
    # unchanged to within 0.0% on both opted-in jobs (SAM 300s, MCH 100s): the Pareto
    # FRONTIER is the entire wall and it is order-invariant, while the admissible bounds
    # are too loose to fire even at the optimal floor (`pruned_bound` stays negligible
    # next to `pruned_dominance`). So a best-first reorder cannot help here; the layered
    # order is kept because it batches dominance cleanly (every state at time `t` is
    # generated before `t` is expanded). See the exact-optimal-rotation-solver memory.
    while layers:
        if time_box is not None and (time.monotonic() - start) > time_box:
            timed_out = True
            break
        tkey = min(layers)
        bucket = layers.pop(tkey)
        layer_width = sum(len(f.pts) for f in bucket.values())
        if layer_width > max_layer_width:
            max_layer_width = layer_width
        for front in bucket.values():
            for _vec, _g, state in front.pts:
                nodes += 1
                for m in model.legal_gcds(state, params):
                    dur = model.gcd_duration(state, m, params)
                    # commit_gcd_states forks the oGCD weaves (action-perfect), not
                    # just the GCD; the default ogcd_candidates yields one successor.
                    for child in engine.commit_gcd_states(model, state, params, m, dur):
                        _settle_downtime(model, child, downtime)
                        consider(child)

    if stats is not None:
        stats.update(nodes=nodes, leaves=leaves, states=states,
                     pruned_bound=pruned_bound, pruned_dominance=pruned_dominance,
                     max_layer_width=max_layer_width,
                     wall_s=time.monotonic() - start, proven=not timed_out,
                     score=best_leaf_g)   # the solver's internal terminal_g of the best leaf

    if best_leaf_state is None:
        # Timed out before any complete leaf: return greedy so the caller always has a
        # concrete timeline (it guards with the beam, so this never sets the ceiling).
        tl, aux = engine.run_rotation(model, fight_duration_s, downtime, params)
        return tl, aux, False
    return list(best_leaf_state.timeline), model.final_aux(best_leaf_state), \
        not timed_out
