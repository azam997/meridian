"""Tests for the prog-pull kill-time projector (`jobs/_core/kill_projection.py`).

Pure pins: the active-rate math, the ref downtime walk beyond the wipe point,
the closest-ref pick, every graceful-None guard, and the clamps.

Run from python/:  python tests/test_kill_projection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.kill_projection import (
    _PROJECTION_MAX_S,
    ProjectionInputs,
    project_kill_time,
)

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _inputs(**kw) -> ProjectionInputs:
    base = dict(
        elapsed_s=100.0,
        fight_pct_remaining=50.0,
        own_downtime_s=0.0,
        ref_downtime_windows=((),),
        ref_kill_times=(200.0,),
    )
    base.update(kw)
    return ProjectionInputs(**base)


def test_linear_no_downtime() -> None:
    """50% burned in 100 fully-active seconds -> 100 more -> 200s kill."""
    p = project_kill_time(_inputs())
    _check("linear projects 200s", p is not None and abs(p.projected_s - 200.0) < 1e-6,
           f"got {p}")
    _check("linear meta", p.method == "active_rate_v1" and p.ref_count == 1
           and abs(p.active_s - 100.0) < 1e-6 and abs(p.burned_pct - 50.0) < 1e-6
           and p.downtime_beyond_s == 0.0, f"got {p}")


def test_own_downtime_raises_rate() -> None:
    """30% burned over 60 ACTIVE seconds (100 elapsed - 40 downtime): the
    remaining 70% needs 140 active seconds -> naive 240."""
    p = project_kill_time(_inputs(fight_pct_remaining=70.0, own_downtime_s=40.0,
                                  ref_kill_times=(240.0,)))
    _check("own-downtime active rate", p is not None
           and abs(p.projected_s - 240.0) < 1e-6, f"got {p}")


def test_ref_downtime_beyond_extends() -> None:
    """A ref downtime window past the wipe point pushes the projection out by
    its full width; active time before/after it is spent normally."""
    p = project_kill_time(_inputs(
        ref_downtime_windows=(((120.0, 150.0),),), ref_kill_times=(230.0,)))
    # walk: 100 -> 20 active to window edge, skip 30 downtime, 80 active left
    _check("beyond-window extends", p is not None
           and abs(p.projected_s - 230.0) < 1e-6
           and abs(p.downtime_beyond_s - 30.0) < 1e-6, f"got {p}")


def test_straddling_window() -> None:
    """A ref window straddling the wipe point only counts its remainder."""
    p = project_kill_time(_inputs(
        ref_downtime_windows=(((90.0, 130.0),),), ref_kill_times=(230.0,)))
    # t=100 sits inside (90,130): skip to 130 (30s credited), then 100 active
    _check("straddling window remainder", p is not None
           and abs(p.projected_s - 230.0) < 1e-6
           and abs(p.downtime_beyond_s - 30.0) < 1e-6, f"got {p}")


def test_projection_ends_before_window() -> None:
    """Remaining active time consumed before a later window: that window
    contributes nothing."""
    p = project_kill_time(_inputs(
        ref_downtime_windows=(((250.0, 300.0),),), ref_kill_times=(200.0,)))
    _check("later window untouched", p is not None
           and abs(p.projected_s - 200.0) < 1e-6
           and p.downtime_beyond_s == 0.0, f"got {p}")


def test_multiple_windows() -> None:
    p = project_kill_time(_inputs(
        ref_downtime_windows=(((110.0, 120.0), (150.0, 170.0)),),
        ref_kill_times=(230.0,)))
    # 10 active -> +10 dt -> 30 active -> +20 dt -> 60 active left -> 230
    _check("two windows walked", p is not None
           and abs(p.projected_s - 230.0) < 1e-6
           and abs(p.downtime_beyond_s - 30.0) < 1e-6, f"got {p}")


def test_closest_ref_pick() -> None:
    """The ref closest to the naive total supplies the downtime windows."""
    p = project_kill_time(_inputs(
        ref_downtime_windows=(((110.0, 140.0),), ()),
        ref_kill_times=(230.0, 500.0)))
    _check("closest ref chosen", p is not None
           and abs(p.ref_kill_s - 230.0) < 1e-6
           and abs(p.downtime_beyond_s - 30.0) < 1e-6, f"got {p}")
    p2 = project_kill_time(_inputs(
        fight_pct_remaining=80.0,  # naive 100 + 400 = 500
        ref_downtime_windows=(((110.0, 140.0),), ()),
        ref_kill_times=(230.0, 500.0)))
    _check("far ref chosen at long naive", p2 is not None
           and abs(p2.ref_kill_s - 500.0) < 1e-6
           and p2.downtime_beyond_s == 0.0, f"got {p2}")


def test_guards_return_none() -> None:
    _check("missing pct", project_kill_time(_inputs(fight_pct_remaining=None)) is None)
    _check("zero pct (kill-like)", project_kill_time(_inputs(fight_pct_remaining=0.0)) is None)
    _check("negative pct", project_kill_time(_inputs(fight_pct_remaining=-1.0)) is None)
    _check("100 pct (no progress)", project_kill_time(_inputs(fight_pct_remaining=100.0)) is None)
    _check("sub-1% burned", project_kill_time(_inputs(fight_pct_remaining=99.5)) is None)
    _check("no refs", project_kill_time(_inputs(ref_kill_times=(),
                                                ref_downtime_windows=())) is None)
    _check("zero elapsed", project_kill_time(_inputs(elapsed_s=0.0)) is None)


def test_clamps() -> None:
    p = project_kill_time(_inputs(fight_pct_remaining=98.0,
                                  ref_kill_times=(600.0,)))
    # burned 2% in 100s -> 4900s remaining -> clamped to the sim ceiling
    _check("upper clamp", p is not None
           and abs(p.projected_s - _PROJECTION_MAX_S) < 1e-6, f"got {p}")
    p2 = project_kill_time(_inputs(fight_pct_remaining=0.01))
    # kills probe at fightPercentage=0.01: projection collapses to ~elapsed,
    # floored at elapsed
    _check("lower clamp >= elapsed", p2 is not None
           and p2.projected_s >= p2.elapsed_s, f"got {p2}")


def test_phase_fields_inert() -> None:
    """v1 ignores the phase seam — same result with/without phase data."""
    a = project_kill_time(_inputs())
    b = project_kill_time(_inputs(last_phase=3,
                                  phase_transitions=((1, 0), (2, 60000))))
    _check("phase fields inert in v1", a == b, f"{a} vs {b}")


def main() -> None:
    for fn in [test_linear_no_downtime, test_own_downtime_raises_rate,
               test_ref_downtime_beyond_extends, test_straddling_window,
               test_projection_ends_before_window, test_multiple_windows,
               test_closest_ref_pick, test_guards_return_none, test_clamps,
               test_phase_fields_inert]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
