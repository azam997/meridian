"""Unit tests for compute_eff_gcd + compute_idle_stretches.

Pure-function tests with synthetic norm_casts and per-role policies.

Run from python/:  python tests/test_idle_stretches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.idle_stretches import compute_eff_gcd, compute_idle_stretches
from jobs._core.job import (
    CASTER_HEALER,
    MELEE_TANK,
    PHYSICAL_RANGED,
)
from jobs.machinist.data import JOB_DATA as MCH_DATA


_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


# --- compute_eff_gcd -------------------------------------------------------

def test_eff_gcd_clean_2_5() -> None:
    print()
    print("Test: compute_eff_gcd - 2.5s clean cadence")
    norm = [(i * 2.5, 7411) for i in range(20)]
    g = compute_eff_gcd(norm, MCH_DATA)
    _check("eff_gcd ~ 2.5s", abs(g - 2.5) < 0.05, f"got {g}")


def test_eff_gcd_too_few_falls_back() -> None:
    print()
    print("Test: compute_eff_gcd - < 4 casts -> 2.5s default")
    norm = [(0.0, 7411), (2.5, 7412)]
    g = compute_eff_gcd(norm, MCH_DATA)
    _check("eff_gcd = 2.5 default", g == 2.5, f"got {g}")


def test_eff_gcd_blazing_shot_excluded() -> None:
    """Blazing Shot's 1.5s recast is a clip exclusion so it should not
    drag the median down."""
    print()
    print("Test: compute_eff_gcd - clip_exclusions honored")
    # 10 regular GCDs at 2.5s, then 6 Blazing Shots at 1.5s inside HC.
    norm = [(i * 2.5, 7411) for i in range(10)]
    norm += [(25.0 + i * 1.5, 36978) for i in range(6)]
    g = compute_eff_gcd(norm, MCH_DATA)
    _check("eff_gcd ~ 2.5s, not pulled to 1.5", g >= 2.45,
           f"got {g}")


# --- compute_idle_stretches ------------------------------------------------

def test_idle_clean_cadence_no_stretches() -> None:
    print()
    print("Test: compute_idle_stretches - clean cadence -> no stretches")
    norm = [(i * 2.5, 7411) for i in range(40)]
    out = compute_idle_stretches(norm, fight_duration_s=100.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[])
    _check("no stretches", out == [], f"got {out}")


def test_idle_one_big_gap_phys_ranged() -> None:
    """At PHYSICAL_RANGED idle_floor_mult=1.5 × 2.5 = 3.75s — any gap
    above that produces a stretch. Fill the rest of the fight with clean
    casts so the trailing-to-end gap doesn't confound the assertion."""
    print()
    print("Test: compute_idle_stretches - one 6s gap (P-Range)")
    norm = [(i * 2.5, 7411) for i in range(3)]   # t=0,2.5,5.0
    norm += [(11.0 + i * 2.5, 7411) for i in range(8)]   # t=11..28.5
    out = compute_idle_stretches(norm, fight_duration_s=30.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[])
    _check("one stretch (5.0, 11.0)",
           out == [(5.0, 11.0)], f"got {out}")


def test_idle_excluded_window_removed() -> None:
    """A stretch fully covered by an exclude window should disappear."""
    print()
    print("Test: compute_idle_stretches - gap inside exclude window dropped")
    norm = [(0.0, 7411), (2.5, 7412), (5.0, 7413)]
    norm += [(11.0 + i * 2.5, 7411) for i in range(8)]
    out = compute_idle_stretches(norm, fight_duration_s=30.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[(4.0, 12.0)])
    _check("no stretches (covered by exclude)", out == [],
           f"got {out}")


def test_idle_excluded_window_partial() -> None:
    """A stretch partially overlapped by an exclude window should produce
    the uncovered slice only."""
    print()
    print("Test: compute_idle_stretches - partial overlap leaves uncovered slice")
    # Gap from t=5 to t=15 (10s); exclude covers (7, 12).
    # Uncovered slices: (5,7) and (12,15).
    norm = [(0.0, 7411), (2.5, 7412), (5.0, 7413)]
    norm += [(15.0 + i * 2.5, 7411) for i in range(6)]   # fills t=15..27.5
    out = compute_idle_stretches(norm, fight_duration_s=30.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[(7.0, 12.0)])
    _check("two slices (5,7) and (12,15)",
           out == [(5.0, 7.0), (12.0, 15.0)],
           f"got {out}")


def test_idle_caster_healer_tolerant_of_hardcasts() -> None:
    """Caster/Healer policy: idle_floor_mult=2.5 — a single 4s gap on a
    2.5s GCD baseline shouldn't flag (would for P-Range)."""
    print()
    print("Test: compute_idle_stretches - caster_healer ignores 4s gap")
    norm = [(0.0, 7411), (2.5, 7412), (5.0, 7413),
            (9.0, 7411)]   # 4s gap
    norm += [(9.0 + (i + 1) * 2.5, 7411) for i in range(8)]  # fills t=11.5..29
    p_out = compute_idle_stretches(norm, 30.0, 2.5, PHYSICAL_RANGED, [])
    c_out = compute_idle_stretches(norm, 30.0, 2.5, CASTER_HEALER, [])
    _check("phys_ranged flags the 4s gap",
           p_out == [(5.0, 9.0)], f"got {p_out}")
    _check("caster_healer ignores 4s gap",
           c_out == [], f"got {c_out}")


def test_idle_trailing_gap_to_fight_end() -> None:
    """A long gap from the last cast to fight end should produce a
    trailing stretch."""
    print()
    print("Test: compute_idle_stretches - trailing gap to fight end")
    norm = [(0.0, 7411), (2.5, 7412), (5.0, 7413)]
    out = compute_idle_stretches(norm, fight_duration_s=20.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[])
    _check("trailing (5.0, 20.0)", out == [(5.0, 20.0)],
           f"got {out}")


def test_idle_pre_pull_ignored() -> None:
    """Pre-pull casts (t<0) shouldn't contribute to gaps."""
    print()
    print("Test: compute_idle_stretches - pre-pull casts ignored")
    norm = [(-2.5, 2876), (0.0, 7411), (2.5, 7412)]
    out = compute_idle_stretches(norm, fight_duration_s=10.0,
                                  eff_gcd_s=2.5, policy=PHYSICAL_RANGED,
                                  exclude_windows=[])
    _check("no false stretches",
           out == [(2.5, 10.0)],     # only the trailing fight-end gap
           f"got {out}")


def test_idle_melee_tank_slightly_stricter_than_phys() -> None:
    """MELEE_TANK and PHYSICAL_RANGED use the same idle_floor_mult; the
    difference shows up at consensus aggregation, not detection."""
    print()
    print("Test: compute_idle_stretches - MELEE_TANK detection matches P-Range")
    norm = [(0.0, 7411), (2.5, 7412), (10.0, 7413)]
    p_out = compute_idle_stretches(norm, 30.0, 2.5, PHYSICAL_RANGED, [])
    m_out = compute_idle_stretches(norm, 30.0, 2.5, MELEE_TANK, [])
    _check("identical idle detection (only consensus_pct differs)",
           p_out == m_out, f"p={p_out} m={m_out}")


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_eff_gcd_clean_2_5()
    test_eff_gcd_too_few_falls_back()
    test_eff_gcd_blazing_shot_excluded()
    test_idle_clean_cadence_no_stretches()
    test_idle_one_big_gap_phys_ranged()
    test_idle_excluded_window_removed()
    test_idle_excluded_window_partial()
    test_idle_caster_healer_tolerant_of_hardcasts()
    test_idle_trailing_gap_to_fight_end()
    test_idle_pre_pull_ignored()
    test_idle_melee_tank_slightly_stricter_than_phys()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
