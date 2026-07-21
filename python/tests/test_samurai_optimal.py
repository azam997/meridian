"""Invariants for the exact SAM rotation solver (scripts/solve_samurai_optimal.py).

Network-free, marked `slow` (the solver is a diagnostic, not on the hot path). They
guard the three properties the search-vs-fidelity verdict rests on:

  * **exact >= beam** — the exact optimum can never fall below the shipped
    beam-search ceiling (it searches a superset).
  * **incremental g == canonical scorer** — the DP's incremental score equals
    `score_delivered_potency` on the produced timeline (the correctness gate).
  * **dominance is lossless** — solving with the dominance memo on vs. off yields
    the *same* optimum (and the admissible bound never underestimates at the root).

All on a short fight so the exhaustive (memo-off) variant stays cheap.

Run from python/:  python tests/test_samurai_optimal.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.samurai import simulator as sam
from jobs.samurai.scoring import score_delivered_potency
from scripts.solve_samurai_optimal import Solver, cast_mix, solve_optimal

pytestmark = pytest.mark.slow

_SHORT_S = 50.0        # exact + memo-off both tractable here
_CTX = 0               # bonus Kenki 0 (bare-int sim_context)


def _score_no_pot(tl: list) -> float:
    """Score a timeline with the in-sim tincture pot markers stripped. This diagnostic
    is a GCD-optimality (search-vs-fidelity) check, deliberately tincture-FREE to match
    the script solver's tincture-blind incremental `g` (and the real-parse replay, whose
    casts carry no pot marker); the engine transition still places the marker, so strip
    it before scoring for an apples-to-apples comparison."""
    return score_delivered_potency([c for c in tl if c[1] != TINCTURE_ACTION_ID])


def _beam(dur: float) -> float:
    return _score_no_pot(sam.simulate_idealized_perfect(dur, [], sim_context=_CTX)[0])


def test_exact_beats_or_ties_beam():
    beam = _beam(_SHORT_S)
    score, _tl, _solver = solve_optimal(_SHORT_S, [], _CTX, incumbent0=beam)
    assert score >= beam - 1e-6, f"exact {score:.1f} < beam {beam:.1f}"


def test_incremental_score_matches_canonical_scorer():
    """The DP's incremental g (flat + finalized DoT + full trailing DoT) must equal
    score_delivered_potency on the produced optimal timeline. Seed the incumbent at 0
    (not the beam) so the solver returns a concrete optimal timeline even when the
    improved beam already ties the exact optimum at this short duration."""
    score, tl, _solver = solve_optimal(_SHORT_S, [], _CTX, incumbent0=0.0)
    assert tl is not None
    canonical = _score_no_pot(tl)
    assert abs(score - canonical) < 1e-6, f"DP {score:.3f} != canonical {canonical:.3f}"


def test_dominance_is_lossless():
    """Same optimum with the dominance memo on vs. off — proves the prune never
    discards a winning line (exactness)."""
    on = solve_optimal(_SHORT_S, [], _CTX, incumbent0=0.0, use_dominance=True)[0]
    off = solve_optimal(_SHORT_S, [], _CTX, incumbent0=0.0, use_dominance=False)[0]
    assert abs(on - off) < 1e-6, f"dominance changed the optimum: {on:.3f} vs {off:.3f}"


def test_bound_admissible_at_root():
    """g(root) + UB(root) must be >= the true optimum (an admissible bound never
    underestimates) — else B&B could prune the optimum."""
    score, _tl, _solver = solve_optimal(_SHORT_S, [], _CTX, incumbent0=_beam(_SHORT_S))
    model = sam._model_for(_SHORT_S, _CTX)
    params = sam.SimParams(max_weaves_per_gcd=2, forbidden_windows=())
    solver = Solver(model, params, _SHORT_S, [], 0.0, 2)
    root = solver._init_root()
    assert solver.g(root) + solver.ub(root) >= score - 1e-6, "root bound underestimates"


def test_optimal_rotation_is_legal():
    """The optimal timeline obeys the SAM Sen/Iaijutsu balance the model enforces:
    every 3-Sen Setsugekka consumes three enders, each Iaijutsu/Ogi has its Kaeshi,
    Tendo Setsugekka count <= Tendo-Kaeshi count, and Ogi == its Kaeshi."""
    _score, tl, _solver = solve_optimal(_SHORT_S, [], _CTX, incumbent0=0.0)
    m = cast_mix(tl)
    assert m["tendo"] == m["tendo_kaeshi"], m
    assert m["midare"] == m["kaeshi"], m
    assert m["ogi"] == m["kaeshi_nami"], m


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all exact-solver tests passed")


if __name__ == "__main__":
    main()
