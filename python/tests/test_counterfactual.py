"""Unit tests for the prefix-cascade counterfactual runner
(jobs/_core/sim/counterfactual.py) — C(t), the telescoping identity, and the
Runner façade's in-process path.

Run from python/:  python tests/test_counterfactual.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine
from jobs._core.sim.counterfactual import (
    Runner, cascade_scores, normalized_cuts, state_delta,
)
from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.machinist.simulator import SimParams, _model_for

MOD = "jobs.machinist.simulator"
DUR = 120.0
CUTS = (0.0, 30.2, 55.3, 80.4, DUR)
# MCH GCD ids (weaponskills): the ST combo, tools, procs, Blazing Shot, AoE
# line. Explicit so the tests stay hermetic (no ability-metadata lookups).
GCD_IDS = (7411, 7412, 7413, 16497, 16498, 16499, 16500, 25786, 25788,
           36978, 36981, 36982)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _greedy_stream() -> tuple[tuple[float, int], ...]:
    """The pot-free greedy line replayed as the 'player' — the clean baseline."""
    params = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, DUR),))
    timeline, _aux = engine.run_rotation(_model_for(None), DUR, [], params)
    return tuple((t, a) for t, a in timeline if a != TINCTURE_ACTION_ID)


def _sloppy_stream() -> tuple[tuple[float, int], ...]:
    """The clean line with a 6s hole punched at [60, 66) — an idle stretch
    whose cost the cascade must localize in the containing segment."""
    return tuple((t, a) for t, a in _greedy_stream() if not 60.0 <= t < 66.0)


def _gaps(scores: dict[float, float]) -> dict[tuple[float, float], float]:
    asc = sorted(scores)
    return {(a, b): scores[a] - scores[b] for a, b in zip(asc, asc[1:])}


def test_determinism() -> None:
    print("\nTest: cascade_scores is deterministic")
    casts = _sloppy_stream()
    a = cascade_scores(MOD, DUR, (), None, casts, CUTS, GCD_IDS)
    b = cascade_scores(MOD, DUR, (), None, casts, CUTS, GCD_IDS)
    _check("two calls byte-identical", a == b, f"{a} != {b}")


def test_telescoping_identity() -> None:
    print("\nTest: Σ segment losses == C(0) − C(end) (pre-clamp, exact)")
    casts = _sloppy_stream()
    vals = cascade_scores(MOD, DUR, (), None, casts, CUTS, GCD_IDS)
    total = vals[0] - vals[-1]
    seg_sum = sum(a - b for a, b in zip(vals, vals[1:]))
    _check("telescoping holds", abs(seg_sum - total) < 1e-6,
           f"Σ={seg_sum} vs C(0)−C(end)={total}")


def test_clean_stream_exact_zero_gap() -> None:
    print("\nTest: replaying the greedy line itself telescopes to exactly 0")
    clean = cascade_scores(MOD, DUR, (), None, _greedy_stream(), CUTS,
                           GCD_IDS)
    sloppy = cascade_scores(MOD, DUR, (), None, _sloppy_stream(), CUTS,
                            GCD_IDS)
    clean_gap = clean[0] - clean[-1]
    sloppy_gap = sloppy[0] - sloppy[-1]
    # C(0) is greedy-from-scratch by construction and the full stable-order
    # replay reproduces the same timeline + aux — the ends are identical.
    _check("clean C(0) == C(end) exactly", abs(clean_gap) < 1e-6,
           f"gap={clean_gap}")
    # Interior cuts carry a bounded greedy-boundary slack (a resumed line
    # can't retro-weave inside the cut GCD's slot) — per-segment noise only,
    # the total is pinned above.
    per_seg = [a - b for a, b in zip(clean, clean[1:])]
    _check("per-segment boundary slack stays small",
           max(abs(v) for v in per_seg) < 400.0, f"segments={per_seg}")
    _check("the 6s hole costs clearly more than the clean baseline",
           sloppy_gap > clean_gap + 500.0,
           f"sloppy={sloppy_gap} clean={clean_gap}")


def test_hole_localized_to_its_segment() -> None:
    print("\nTest: the hole's segment carries the max loss")
    casts = _sloppy_stream()
    vals = cascade_scores(MOD, DUR, (), None, casts, CUTS, GCD_IDS)
    scores = dict(zip(normalized_cuts(CUTS), vals))
    gaps = _gaps(scores)
    worst = max(gaps, key=lambda s: gaps[s])
    _check("worst segment is (55.3, 80.4) — it contains [60, 66)",
           worst == (55.3, 80.4), f"got {worst} gaps={gaps}")


def test_runner_in_process() -> None:
    print("\nTest: Runner (no pool installed) matches the module entry")
    casts = _sloppy_stream()
    r = Runner(MOD, DUR, (), None, casts, gcd_ids=GCD_IDS)
    got = r.scores(CUTS)
    want = dict(zip(normalized_cuts(CUTS),
                    cascade_scores(MOD, DUR, (), None, casts, CUTS,
                                   GCD_IDS)))
    _check("Runner.scores == cascade_scores", got == want,
           f"{got} != {want}")
    deltas = r.deltas([(55.3, 80.4)])
    _check("Runner.deltas returns player+ideal snapshots",
           len(deltas) == 1 and "player" in deltas[0]
           and "ideal" in deltas[0], f"got {deltas}")


def test_state_delta_shows_the_hole() -> None:
    print("\nTest: state_delta's ideal side out-runs the idle player")
    casts = _sloppy_stream()
    d = state_delta(MOD, DUR, (), None, casts, 55.3, 80.4, GCD_IDS)
    ideal_n = len(d["idealSegmentTimeline"])
    player_n = len(d["playerSegmentCasts"])
    _check("ideal continuation fits more casts than the player's holey stretch",
           ideal_n > player_n, f"ideal={ideal_n} player={player_n}")


def main() -> int:
    test_determinism()
    test_telescoping_identity()
    test_clean_stream_exact_zero_gap()
    test_hole_localized_to_its_segment()
    test_runner_in_process()
    test_state_delta_shows_the_hole()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
