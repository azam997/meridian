"""Unit tests for the buff-window timing cards
(jobs/_core/improvements.py::buff_window_improvements).

A SEPARATE currency from the strict improvements panel: burst tools delivered
just outside the party's observed buff windows, priced base × (mult − 1) per
cast, forward-shift only (≤ 8s before a window), capped at the observed-lens
budget with reconcile's infinite residual floor (no residual card is minted).

Run from python/:  python tests/test_buff_window_card.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.improvements import buff_window_improvements
from jobs._core.job import JobData

DRILL = 16498   # real GCD id so ability_metadata resolves a name

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _data() -> JobData:
    return JobData(job_name="Test", patch_version="7.x",
                   potencies={DRILL: 600},
                   burst_abilities=frozenset({DRILL}))


# One 1.10× window from 20s to 40s.
WINDOWS = [(20.0, 40.0, 1.10, "Battle Litany")]


def test_forward_shift_priced() -> None:
    print("\nTest: a burst cast shortly before a window prices base × (mult−1)")
    out = buff_window_improvements([(17.0, DRILL)], WINDOWS, _data(), 1000.0)
    _check("one card", len(out) == 1, f"got {len(out)}")
    im = out[0]
    _check("kind buff_window, located at the cast",
           im.kind == "buff_window" and abs(im.time_s - 17.0) < 0.01,
           f"got {im.kind} @ {im.time_s}")
    _check("priced 600 × 0.10 = 60",
           abs(im.lost_potency - 60.0) < 0.01, f"got {im.lost_potency}")
    _check("summary names provider + hold time",
           "Battle Litany" in im.summary and "Holding ~3s" in im.summary,
           f"got {im.summary!r}")
    _check("prescription says hold",
           im.prescription == "Hold Drill ~3s for the buff window.",
           f"got {im.prescription!r}")


def test_exclusions() -> None:
    print("\nTest: inside-window, far-before, after-window, non-burst → nothing")
    d = _data()
    _check("inside the window → nothing",
           buff_window_improvements([(25.0, DRILL)], WINDOWS, d, 1000.0) == [],
           "expected []")
    _check("more than 8s before → nothing",
           buff_window_improvements([(10.0, DRILL)], WINDOWS, d, 1000.0) == [],
           "expected []")
    _check("after the last window → nothing",
           buff_window_improvements([(50.0, DRILL)], WINDOWS, d, 1000.0) == [],
           "expected []")
    _check("non-burst ability → nothing",
           buff_window_improvements([(17.0, 999999)], WINDOWS, d, 1000.0) == [],
           "expected []")
    _check("pre-pull cast → nothing",
           buff_window_improvements([(-3.0, DRILL)], WINDOWS, d, 1000.0) == [],
           "expected []")


def test_noise_floor() -> None:
    print("\nTest: sub-noise-floor value is dropped")
    weak = JobData(job_name="Test", patch_version="7.x",
                   potencies={DRILL: 300},   # 300 × 0.10 = 30 < 40 floor
                   burst_abilities=frozenset({DRILL}))
    _check("30p < 40p floor → nothing",
           buff_window_improvements([(17.0, DRILL)], WINDOWS, weak, 1000.0) == [],
           "expected []")


def test_budget_cap_no_residual() -> None:
    print("\nTest: cards cap at the observed budget with NO residual card")
    # Three qualifying casts at 60p each; budget 130 keeps only two.
    casts = [(17.0, DRILL), (15.0, DRILL), (13.0, DRILL)]
    out = buff_window_improvements(casts, WINDOWS, _data(), 130.0)
    _check("two cards kept (120 ≤ 130 < 180)", len(out) == 2, f"got {len(out)}")
    _check("no residual minted",
           all(im.kind == "buff_window" for im in out),
           f"got {[im.kind for im in out]}")
    _check("total within budget",
           sum(im.lost_potency for im in out) <= 130.0 + 1e-6,
           f"got {sum(im.lost_potency for im in out)}")


def test_empty_inputs() -> None:
    print("\nTest: zero budget / no windows / no burst abilities → nothing")
    d = _data()
    _check("zero budget", buff_window_improvements(
        [(17.0, DRILL)], WINDOWS, d, 0.0) == [], "expected []")
    _check("no windows", buff_window_improvements(
        [(17.0, DRILL)], [], d, 1000.0) == [], "expected []")
    no_burst = JobData(job_name="Test", patch_version="7.x",
                       potencies={DRILL: 600})
    _check("no burst_abilities", buff_window_improvements(
        [(17.0, DRILL)], WINDOWS, no_burst, 1000.0) == [], "expected []")


def main() -> int:
    test_forward_shift_priced()
    test_exclusions()
    test_noise_floor()
    test_budget_cap_no_residual()
    test_empty_inputs()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
