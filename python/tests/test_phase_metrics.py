"""Tests for per-phase execution metrics + deviation detection
(`jobs/_core/phase_metrics.py`). Pure synthetic casts + a synthetic gauge.

Run from python/:  python tests/test_phase_metrics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.job import GaugeModel
from jobs._core.phases import Phase
from jobs._core.phase_metrics import (
    aggregate_phase_metrics,
    compute_phase_metrics,
    detect_deviations,
)

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


# Gen aid=1 (+10), flat spend aid=2 (-30), "all"-spend aid=3, cap 100.
GAUGE = GaugeModel(name="kenki", generators={1: 10}, spenders={2: 30, 3: "all"},
                   cap=100, value_p_per_unit=0.5)
PHASES = (
    Phase(id=1, name="P1", start_s=0.0, end_s=100.0, is_intermission=False),
    Phase(id=2, name="P2", start_s=100.0, end_s=200.0, is_intermission=False),
    Phase(id=3, name="P3", start_s=200.0, end_s=300.0, is_intermission=False),
)


def _gauge_of(m):
    return m.gauges[0]


def test_conservation_and_boundary() -> None:
    casts = [(10.0, 1), (20.0, 1), (30.0, 1),        # P1: +30
             (110.0, 2), (120.0, 1),                 # P2: -30, +10
             (210.0, 1), (220.0, 3)]                 # P3: +10 then dump
    ms = compute_phase_metrics(casts, PHASES, [GAUGE], [])
    for m in ms:
        g = _gauge_of(m)
        _check(f"conservation P{m.phase_id}",
               g.entry + g.generated - g.spent - g.overcapped == g.exit,
               f"{g}")
    # Exit of phase i == entry of phase i+1.
    for i in range(len(ms) - 1):
        _check(f"boundary P{i+1}->P{i+2}",
               _gauge_of(ms[i]).exit == _gauge_of(ms[i + 1]).entry)
    _check("P1 exit 30", _gauge_of(ms[0]).exit == 30, str(_gauge_of(ms[0])))
    _check("P2 exit 10", _gauge_of(ms[1]).exit == 10, str(_gauge_of(ms[1])))
    _check("P3 all-spend zeroes", _gauge_of(ms[2]).exit == 0, str(_gauge_of(ms[2])))
    _check("P3 spent==20 (the dumped balance)",
           _gauge_of(ms[2]).spent == 20, str(_gauge_of(ms[2])))


def test_cap_clamp_and_overcap() -> None:
    # 12 generators of +10 = 120 requested, cap 100 -> 20 overcapped, exit 100.
    casts = [(float(i), 1) for i in range(12)]
    ms = compute_phase_metrics(casts, PHASES, [GAUGE], [])
    g = _gauge_of(ms[0])
    _check("exit clamped to cap", g.exit == 100, str(g))
    _check("overcapped 20", g.overcapped == 20, str(g))
    _check("generated 120 (requested)", g.generated == 120, str(g))


def test_entry_seeding_continuation() -> None:
    """A spend before any generation -> deepest-deficit seeds carried gauge."""
    casts = [(5.0, 2)]  # spend 30 with no prior generation
    ms = compute_phase_metrics(casts, PHASES, [GAUGE], [])
    g = _gauge_of(ms[0])
    _check("entry seeded to 30 (never goes negative)", g.entry == 30, str(g))
    _check("exit 0 after the spend", g.exit == 0, str(g))


def test_gcd_and_active_and_truncation() -> None:
    casts = [(10.0, 1), (12.0, 9), (20.0, 1),        # P1: 2 GCD (aid 1), 1 oGCD (9)
             (110.0, 1)]
    is_gcd = lambda aid: aid != 9  # noqa: E731
    ms = compute_phase_metrics(casts, PHASES, [GAUGE], [(40.0, 60.0)], is_gcd=is_gcd)
    _check("P1 gcd_casts 2", ms[0].gcd_casts == 2, str(ms[0]))
    _check("P1 total 3", ms[0].total_casts == 3, str(ms[0]))
    _check("P1 active = 100 - 20 downtime", abs(ms[0].active_s - 80.0) < 1e-6,
           str(ms[0].active_s))
    # Truncate at 115s: P2 becomes partial, P3 not reached (empty).
    mt = compute_phase_metrics(casts, PHASES, [GAUGE], [], end_s=115.0, is_gcd=is_gcd)
    p2 = next(m for m in mt if m.phase_id == 2)
    _check("P2 partial at end_s=115", p2.partial, str(p2))
    _check("P2 active clipped to 15s", abs(p2.active_s - 15.0) < 1e-6, str(p2.active_s))


def test_pot_used() -> None:
    ms = compute_phase_metrics([(10.0, 1)], PHASES, [GAUGE], [],
                               tincture_windows=[(105.0, 135.0)])
    _check("P1 no pot", not ms[0].pot_used)
    _check("P2 pot used", ms[1].pot_used)


# --- aggregation + deviations ------------------------------------------------

def _ref_metrics(exit_p1: int, gcd_p2: int, pot_p2: bool):
    """A synthetic ref: control P1 exit gauge, P2 gcd count + pot."""
    casts = [(float(i), 1) for i in range(exit_p1 // 10)]              # P1 gen
    casts += [(100.0 + i, 1) for i in range(gcd_p2)]                   # P2 gcds
    tinc = [(100.0, 130.0)] if pot_p2 else []
    return compute_phase_metrics(casts, PHASES, [GAUGE], [],
                                 tincture_windows=tinc), tinc


def test_aggregation_and_deviations() -> None:
    # 6 refs: P1 exit ~50, P2 ~40 GCDs, all pot in P2.
    refs = []
    for _ in range(6):
        rm, _ = _ref_metrics(exit_p1=50, gcd_p2=40, pot_p2=True)
        refs.append(rm)
    agg = aggregate_phase_metrics(refs)
    _check("agg P1 exit median 50", abs(agg[1].gauges["kenki"]["exit"]["median"] - 50) < 1e-6,
           str(agg[1].gauges["kenki"]["exit"]))
    _check("agg P2 pot 100%", abs(agg[2].pot_pct - 1.0) < 1e-6, str(agg[2].pot_pct))

    # Subject: P1 exit only 10 (banked far less), no pot in P2, slow P2.
    user_casts = [(0.0, 1)]                       # P1 exit 10
    user_casts += [(100.0 + i * 2, 1) for i in range(20)]  # P2 ~20 gcds, spread
    user = compute_phase_metrics(user_casts, PHASES, [GAUGE], [])
    # Refs reliably pot in the COMPLETED P2; the user didn't -> flagged.
    devs = detect_deviations(user, agg, PHASES, [GAUGE], ref_count=6)
    kinds = {d["kind"] for d in devs}
    _check("gauge_exit flagged", "gauge_exit" in kinds, str(kinds))
    _check("pot_phase flagged on a completed phase the refs pot in",
           "pot_phase" in kinds, str(kinds))

    # The pot deviation must NOT fire on the partial (death) phase, even if the
    # refs pot there — a pot "due" in the phase you wiped in isn't held against you.
    user_partial = compute_phase_metrics(user_casts, PHASES, [GAUGE], [], end_s=150.0)
    p2_partial = next(m for m in user_partial if m.phase_id == 2)
    assert p2_partial.partial, "expected P2 truncated to be partial"
    devs_partial = detect_deviations(user_partial, agg, PHASES, [GAUGE], ref_count=6)
    _check("no pot_phase on the partial death phase",
           not any(d["kind"] == "pot_phase" and d["phase_id"] == 2 for d in devs_partial),
           str([(d["kind"], d["phase_id"]) for d in devs_partial]))

    # Suppressed under the ref-count floor.
    _check("suppressed below min refs",
           detect_deviations(user, agg, PHASES, [GAUGE], ref_count=3) == [])


def main() -> None:
    for fn in [test_conservation_and_boundary, test_cap_clamp_and_overcap,
               test_entry_seeding_continuation, test_gcd_and_active_and_truncation,
               test_pot_used, test_aggregation_and_deviations]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
