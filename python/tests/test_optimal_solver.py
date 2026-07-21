"""Invariants for the job-agnostic exact rotation solver (jobs/_core/sim/optimal.py).

The solver computes the **provable model optimum** (forward layered DP + Pareto
dominance + branch-and-bound) for the jobs that opt into the engine seam. These
tests guard the properties the >100% gate rests on, on a SHORT fight so the
full-exact-key reference (no Pareto) stays cheap:

  * **exact >= beam** — the optimum can never fall below the shipped beam ceiling
    (it searches a superset).
  * **terminal_g == canonical scorer** — the model's incremental terminal score
    equals `score_delivered_potency` on the produced timeline (the correctness gate
    the DP's final selection uses).
  * **dominance lossless** — the job's Pareto split (`dominance_key` +
    `dominance_vector`) reaches the SAME optimum as full exact-state merging (every
    field categorical, no vector). This is the proof the monotone vector never drops
    a winning line.
  * **bound admissible at root** — `exact_g(root) + admissible_remaining(root)` is
    >= the optimum (an admissible bound never underestimates), so the B&B prune can
    never discard the optimum.
  * **default seam is exact** — a model driven entirely by the `BaseRotationModel`
    defaults (gcd_candidates / beam_signature / no vector / +inf bound / re-scan
    score) reaches the same optimum, so a job is correct the moment it routes through
    the solver, before any tuning.

The reusable `assert_solver_invariants` harness is imported by each opted-in job's
own test (Reaper / Machinist / Paladin) so they all check the same five properties.

Run from python/:  python tests/test_optimal_solver.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine, optimal

pytestmark = pytest.mark.slow

_SHORT_S = 50.0


def assert_solver_invariants(model, score_fn, duration_s, *, beam_score,
                             downtime=None, legal_check=None,
                             check_default_seam=True):
    """Check the exact-solver invariants for one job `model` on a short fight.

    `model` is a fresh RotationModel instance (its overrides under test); `score_fn`
    is the job's `(timeline, aux, buff_intervals) -> float`; `beam_score` is the
    shipped beam/greedy ceiling for the same fight. `legal_check(timeline)` optionally
    asserts job-specific rotation legality. `check_default_seam` (default True) checks
    that the engine defaults reach the same optimum — valid only when the job's dense
    moves live in `gcd_candidates` and it has a full `beam_signature` (SAM/PLD); a job
    that puts its forks in `legal_gcds` with a bespoke `dominance_key` (RPR) passes
    False. Returns the optimal `(timeline, aux)`."""
    downtime = downtime or []

    # Solve the exact optimum over the model's full param sweep (matching the beam),
    # keeping the best — so `exact >= beam` is a fair comparison and every param
    # converges. `terminal_g` must equal the canonical scorer on each produced line.
    best: tuple[float, list, int, object] | None = None
    for params in model.sweep_params(()):
        st: dict = {}
        tl, aux, proven = optimal.solve_optimal(
            model, score_fn, duration_s, downtime, params, incumbent=0.0, stats=st)
        assert proven, f"solver did not converge on the {duration_s:.0f}s fixture"
        s = score_fn(tl, aux, None)
        assert abs(st["score"] - s) < 1e-6, (
            f"terminal_g {st['score']:.3f} != canonical {s:.3f}")
        if best is None or s > best[0]:
            best = (s, tl, aux, params)
    assert best is not None
    opt, tl, aux, params = best
    assert opt >= beam_score - 1e-6, f"exact {opt:.1f} < beam {beam_score:.1f}"

    # The remaining invariants are param-independent properties — check them on the
    # winning param. Dominance lossless: the Pareto split reaches the same optimum as
    # full exact-key merging (every field categorical, no vector).
    exact_key = _ExactKey(model)
    tl2, aux2, _p2 = optimal.solve_optimal(
        exact_key, score_fn, duration_s, downtime, params, incumbent=0.0)
    opt2 = score_fn(tl2, aux2, None)
    assert abs(opt - opt2) < 1e-6, (
        f"Pareto split lossy: {opt:.3f} vs exact-key {opt2:.3f}")

    # Bound admissible at the root (fast static sanity).
    root = model.init_state()
    root.fight_duration_s = duration_s
    root.downtime_windows = downtime
    root.buff_intervals = []
    model.prepull(root, params)
    assert model.exact_g(root, score_fn) + model.admissible_remaining(root) \
        >= opt - 1e-6, "root admissible bound underestimates the optimum"

    # Bound admissible ALONG THE OPTIMAL LINE — the property the B&B prune actually
    # rests on, which the root check alone can't give. The sweep above runs with
    # incumbent=0.0, so the prune (`g + admissible_remaining < floor`) never fires;
    # here we re-solve with the incumbent set just BELOW the optimum, making the prune
    # maximally aggressive — it now discards every state whose `exact_g +
    # admissible_remaining` falls under `opt`. An admissible bound never underestimates
    # an optimal prefix (the optimum is reachable from each), so those prefixes survive
    # and the solve still reaches `opt`; an inadmissible bound prunes one and the result
    # drops below it. This stresses the prune at every visited state — the strong guard
    # for the new MCH / tightened SAM bounds.
    tlb, auxb, _pb = optimal.solve_optimal(
        model, score_fn, duration_s, downtime, params, incumbent=opt - 1e-3)
    assert score_fn(tlb, auxb, None) >= opt - 1e-6, (
        f"admissible bound pruned the optimum under a tight incumbent: "
        f"{score_fn(tlb, auxb, None):.3f} < {opt:.3f}")

    # Default seam reaches the same optimum (correct before any tuning) — only when
    # the job's dense moves live in gcd_candidates with a full beam_signature.
    if check_default_seam:
        default = _DefaultSeam(model)
        tl3, aux3, _p3 = optimal.solve_optimal(
            default, score_fn, duration_s, downtime, params, incumbent=0.0)
        assert abs(opt - score_fn(tl3, aux3, None)) < 1e-6, (
            f"default-seam optimum {score_fn(tl3, aux3, None):.3f} != tuned {opt:.3f}")

    if legal_check is not None:
        legal_check(tl)
    return tl, aux


class _ExactKey:
    """Wrap a model so dominance folds the job's whole (dominance_key,
    dominance_vector) into ONE categorical key with no Pareto vector — i.e. exact
    state merging, the lossless reference. If the job's Pareto split reaches the same
    optimum as this, the monotone vector never dropped a winning line. Works for any
    opted-in job (no `beam_signature` required)."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    def dominance_key(self, state):
        return (self._base.dominance_key(state), self._base.dominance_vector(state))

    def dominance_vector(self, state):
        return ()


class _DefaultSeam:
    """Wrap a model so the solver seam falls back to the BaseRotationModel defaults
    (gcd_candidates as legal_gcds, beam_signature as dominance_key, empty vector,
    +inf bound, re-scan exact_g/terminal_g, deepcopy clone) — proving a job is exact
    the moment it routes through the solver, before tuning. Delegates the rotation
    methods (pick_gcd / apply_cast / init_state / ...) to the wrapped model."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    legal_gcds = engine.BaseRotationModel.legal_gcds
    ogcd_candidates = engine.BaseRotationModel.ogcd_candidates
    clone = engine.BaseRotationModel.clone
    dominance_key = engine.BaseRotationModel.dominance_key
    dominance_vector = engine.BaseRotationModel.dominance_vector
    admissible_remaining = engine.BaseRotationModel.admissible_remaining
    exact_g = engine.BaseRotationModel.exact_g
    terminal_g = engine.BaseRotationModel.terminal_g


# --- Samurai (the first opted-in job) ---------------------------------------

def _sam():
    from jobs.samurai import simulator as sam
    from jobs.samurai.scoring import score_delivered_potency

    def score(tl, aux, bi):
        return score_delivered_potency(tl, buff_intervals=bi)

    model = sam._model_for(_SHORT_S, 0)
    beam = score(sam.simulate_idealized_perfect(_SHORT_S, [], sim_context=0)[0], 0, None)
    return model, score, beam


def _sam_legal(timeline):
    from jobs.samurai import data as sd
    from collections import Counter
    c = Counter(a for t, a in timeline if t >= 0)
    # Every Iaijutsu/Ogi has its Kaeshi replay (the Sen/Tsubame balance the model holds).
    assert c[sd.TENDO_SETSUGEKKA] == c[sd.TENDO_KAESHI_SETSUGEKKA], c
    assert c[sd.MIDARE_SETSUGEKKA] == c[sd.KAESHI_SETSUGEKKA], c
    assert c[sd.OGI_NAMIKIRI] == c[sd.KAESHI_NAMIKIRI], c


def test_samurai_solver_invariants():
    model, score, beam = _sam()
    assert_solver_invariants(model, score, _SHORT_S, beam_score=beam,
                             legal_check=_sam_legal)


def test_samurai_incremental_exact_g():
    """SAM's O(1) incremental `exact_g`/`terminal_g` equals the canonical scorer
    on every PREFIX state — across the dense Sen/Higanbana GCD forks, a downtime
    window, and the in-sim opener pot (the pot-aware accumulators replaced the
    re-score-when-`tincture_used` fallback; the Higanbana DoT snapshots the pot
    multiplier at the application instant, the part a flat sum can't carry)."""
    from jobs.samurai import simulator as sam

    score = sam._score
    duration, downtime = 90.0, [(40.0, 48.0)]
    model = sam._model_for(duration, 0)
    params = sam.SimParams(max_weaves_per_gcd=2)

    root = model.init_state()
    root.fight_duration_s = duration
    root.downtime_windows = downtime
    root.buff_intervals = []
    model.prepull(root, params)
    optimal._settle_downtime(model, root, downtime)

    frontier = [root]
    expanded = leaves = 0
    while frontier and expanded < 1200:
        state = frontier.pop()
        inc = model.exact_g(state, score)
        ref = score(state.timeline, 0, None)
        # exact_g credits the TRAILING Higanbana by elapsed time (an intentional
        # underestimate of the scorer's full-credit convention); add the same
        # remainder to compare exactly.
        if state._score_last_higan is not None:
            ref -= (sam._HIGANBANA_FULL_DOT_P
                    - sam._dot_segment(state.t - state._score_last_higan)) \
                * state._g_last_higan_m
        assert abs(inc - ref) < 1e-6, (
            f"incremental exact_g {inc:.6f} != scorer {ref:.6f} "
            f"at t={state.t:.2f} ({len(state.timeline)} casts)")
        if state.t >= duration:
            term = model.terminal_g(state, score)
            full = score(state.timeline, 0, None)
            assert abs(term - full) < 1e-6, (
                f"terminal_g {term:.6f} != scorer {full:.6f}")
            leaves += 1
            continue
        expanded += 1
        for m in model.legal_gcds(state, params):
            dur = model.gcd_duration(state, m, params)
            for child in engine.commit_gcd_states(model, state, params, m, dur):
                optimal._settle_downtime(model, child, downtime)
                frontier.append(child)
    assert leaves > 0, "no complete rotation reached within the expansion cap"


# --- Paladin (FoF-packing GCD fork; FoF self-buff scored from the timeline) --

def _pld():
    from jobs.paladin import simulator as pld
    from jobs.paladin.scoring import score_delivered_potency

    def score(tl, aux, bi):
        return score_delivered_potency(tl, buff_intervals=bi)

    model = pld._model_for(None)
    beam = score(pld._beam_best(model, score, _SHORT_S, [], None)[0], 0, None)
    return model, score, beam


def test_paladin_solver_invariants():
    model, score, beam = _pld()
    assert_solver_invariants(model, score, _SHORT_S, beam_score=beam)


# --- Machinist (Queen weave fork; battery/heat categorical) -------------------

class _MchDpParams:
    """Wrap the MCH model so the harness sweeps only what the production DP
    solves: the weave budgets, at `queen_cast_battery=50` (the legal minimum) —
    the `[QUEEN, None]` weave fork owns the hold decision, subsuming the higher
    greedy thresholds the beam's 30-point sweep walks."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    def sweep_params(self, extra_forbidden):
        from jobs.machinist import simulator as mch
        for mw in mch._SWEEP_MAX_WEAVES:
            yield mch.SimParams(max_weaves_per_gcd=mw,
                                forbidden_windows=extra_forbidden)


# 40s, not _SHORT_S: with the incremental exact_g a 40s budget proves in ~6s vs
# ~24s at 50s (bench_mch_dp.py) — kept under the 45s production gate
# (`_DP_MAX_DURATION_S`) purely for suite wall time; the harness runs the full
# sweep plus the exact-key reference, so seconds here multiply.
_MCH_S = 40.0


def _mch():
    from jobs.machinist import simulator as mch
    from jobs.machinist.scoring import score_delivered_potency

    def score(tl, aux, bi):
        return score_delivered_potency(tl, aux, bi)

    model = _MchDpParams(mch._model_for(None))
    # Lower bound = the GREEDY line (RPR convention): the DP's move set forks the
    # GCDs + the Queen weave but keeps other weaves greedy, so it can't replicate
    # `refine`'s Hypercharge/Wildfire burst-timing holds — production guards the
    # ceiling with max(DP, beam) for exactly this reason.
    greedy_tl, greedy_aux = mch.simulate_idealized(_MCH_S, [])
    return model, score, score(greedy_tl, greedy_aux, None)


def test_machinist_solver_invariants():
    model, score, lower = _mch()
    # MCH's GCD forks live in gcd_candidates (default-seam reachable), but the
    # Queen hold is an ogcd_candidates weave fork the engine-default seam (greedy
    # weaves) can't explore — same situation as RPR, so that check is skipped.
    assert_solver_invariants(model, score, _MCH_S, beam_score=lower,
                             check_default_seam=False)


def _mch_aoe():
    """MCH under a 3-target AoE schedule. `_optimal_best` routes AoE THROUGH the DP
    (unlike SAM, which routes AoE to the beam), so the solver seam must stay correct
    when casts are scored per-target: (a) the monotone `dominance_vector` must stay
    lossless under cleaved scoring, and (b) the single-target `admissible_remaining`
    must be DISABLED (+inf) under AoE — else it under-estimates the cleaved reward and
    could prune the optimum. This path isn't covered by the no-AoE model above."""
    from jobs.machinist import simulator as mch
    schedule = ((0.0, _MCH_S, 3),)
    model = _MchDpParams(mch.MachinistRotationModel(mt_schedule=schedule))
    score = mch._make_score(schedule)
    greedy_tl, greedy_aux = engine.run_rotation(
        mch.MachinistRotationModel(mt_schedule=schedule), _MCH_S, [], mch.SimParams())
    return model, score, score(greedy_tl, greedy_aux, None)


def test_machinist_solver_invariants_aoe():
    model, score, lower = _mch_aoe()
    # The AoE-bound guard returns +inf, so admissibility is trivial; the load-bearing
    # check here is dominance LOSSLESSNESS under per-target scoring (the `_ExactKey`
    # reference) — the monotone vector must not drop a winning cleaved line.
    assert model.admissible_remaining(model.init_state()) == float("inf")
    assert_solver_invariants(model, score, _MCH_S, beam_score=lower,
                             check_default_seam=False)


def test_machinist_incremental_exact_g():
    """MCH's O(1) incremental `exact_g`/`terminal_g` equals the canonical scorer
    on every PREFIX state, not just leaves — walked over the solver's own
    transition (GCD forks x the Queen weave fork) across a mid-fight downtime
    window (Flamethrower tick + Reassemble consumption + Queen deliverability
    fraction) and the in-sim opener pot (the pot-aware part a flat sum can't
    reproduce: the scorer folds the marker into a multiplier window even with
    `buff_intervals=None`)."""
    from jobs.machinist import simulator as mch
    from jobs.machinist.scoring import score_delivered_potency

    def score(tl, aux, bi):
        return score_delivered_potency(tl, aux, bi)

    model = mch._model_for(None)
    duration, downtime = 70.0, [(30.0, 40.0)]
    params = mch.SimParams(max_weaves_per_gcd=2)

    root = model.init_state()
    root.fight_duration_s = duration
    root.downtime_windows = downtime
    root.buff_intervals = []
    model.prepull(root, params)
    optimal._settle_downtime(model, root, downtime)

    # Depth-first so full-length lines (incl. post-downtime FT + late Queens)
    # are reached well inside the expansion cap.
    frontier = [root]
    expanded = leaves = 0
    while frontier and expanded < 1200:
        state = frontier.pop()
        inc = model.exact_g(state, score)
        ref = score(state.timeline, model.final_aux(state), None)
        assert abs(inc - ref) < 1e-6, (
            f"incremental exact_g {inc:.6f} != scorer {ref:.6f} "
            f"at t={state.t:.2f} ({len(state.timeline)} casts)")
        if state.t >= duration:
            assert abs(model.terminal_g(state, score) - ref) < 1e-6
            leaves += 1
            continue
        expanded += 1
        for m in model.legal_gcds(state, params):
            dur = model.gcd_duration(state, m, params)
            for child in engine.commit_gcd_states(model, state, params, m, dur):
                optimal._settle_downtime(model, child, downtime)
                frontier.append(child)
    assert leaves > 0, "no complete rotation reached within the expansion cap"


# --- Reaper (exercises the oGCD-economy FORK path: Blood-Stalk-vs-bank) -------
# RPR's production ceiling stays on engine.perfect — its >100% is model FIDELITY,
# not search (the action-perfect solver finds only ~+0.2% vs a ~2.2% gap; see the
# exact-optimal-rotation-solver memory). But its solver overrides + the oGCD-weave
# fork are validated here for losslessness / exactness / the default seam. The lower
# bound is the greedy line (a fork can only match or beat it), not `perfect` (whose
# burst-timing refinement the action search doesn't replicate).

def _rpr():
    from jobs.reaper import simulator as rpr
    from jobs.reaper.scoring import score_delivered_potency

    def score(tl, aux, bi):
        return score_delivered_potency(tl, buff_intervals=bi)

    model = rpr._model_for(None)
    greedy = score(rpr.simulate_idealized(_SHORT_S, [])[0], 0, None)
    return model, score, greedy


def test_reaper_solver_oGCD_fork_invariants():
    model, score, lower = _rpr()
    # RPR's dense moves live in legal_gcds with a bespoke dominance_key (no
    # beam_signature), so the engine-default seam can't match its forks — skip that
    # check. The fork path + losslessness + admissibility are what's validated here.
    assert_solver_invariants(model, score, _SHORT_S, beam_score=lower,
                             check_default_seam=False)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all exact-solver invariants passed")


if __name__ == "__main__":
    main()
