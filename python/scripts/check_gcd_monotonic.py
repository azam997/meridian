"""Empirical check: the threaded (per-player gear GCD) ceiling is monotone-anchored.

The gear-GCD inference (gcd_speed.effective_gcd_for) no longer has a safety floor; its
safety is STRUCTURAL — `subgcd_gcd_sweep(gear, constant)` anchors every threaded sweep
with the job constant's calibrated band, so the maxed ceiling can never fall below the
calibrated constant ceiling. This script verifies that guarantee end-to-end on the real
job simulators (no network), and reports the ceiling curve across the gear axis so any
residual search non-monotonicity WITHIN the gear bands stays visible (informational —
the anchor + the dense threaded band make it harmless, but a large wobble here would
mean the dense band needs more points).

Run from python/:
    python scripts/check_gcd_monotonic.py
    python scripts/check_gcd_monotonic.py --jobs Paladin Machinist --dur 390 --depth 0.08
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.gcd_speed import CeilingContext, subgcd_gcd_sweep  # noqa: E402

# job -> (package, GCD-constant attribute on its simulator module)
JOBS: dict[str, tuple[str, str]] = {
    "Paladin": ("paladin", "GCD_BASE_S"),
    "Machinist": ("machinist", "GCD_BASE_S"),
    "Reaper": ("reaper", "GCD_BASE_S"),
    "Warrior": ("warrior", "GCD_BASE_S"),
    "RedMage": ("redmage", "GCD_BASE_S"),
    "Samurai": ("samurai", "SAM_GCD_S"),
    "WhiteMage": ("whitemage", "WHM_GCD_S"),
    "Astrologian": ("astrologian", "AST_GCD_S"),
    "Scholar": ("scholar", "SCH_GCD_S"),
    "Sage": ("sage", "SGE_GCD_S"),
    "Dancer": ("dancer", "GCD_BASE_S"),
    "BlackMage": ("blackmage", "GCD_BASE_S"),
    "Dragoon": ("dragoon", "DRG_GCD_S"),
    "Gunbreaker": ("gunbreaker", "GNB_GCD_S"),
    "Ninja": ("ninja", "NIN_GCD_S"),
    "Monk": ("monk", "MNK_GCD_S"),
    "Bard": ("bard", "BRD_GCD_S"),
    "Pictomancer": ("pictomancer", "PCT_GCD_S"),
    "Summoner": ("summoner", "SMN_GCD_S"),
    "DarkKnight": ("darkknight", "DRK_GCD_S"),
}


def ceiling_at(idealized_at_duration, dur: float, gear: float, const: float) -> tuple[float, float]:
    """The final maxed ceiling for a threaded gear GCD: max over the union sweep,
    exactly as ScoringAspectBase / _inject_tier_b compute it. Returns (ceiling, argmax
    cadence)."""
    best, best_cad = -1.0, gear
    for cad in subgcd_gcd_sweep(gear, const):
        v = idealized_at_duration(dur, [], None,
                                  sim_context=CeilingContext(gcd_base_s=cad))
        if v > best:
            best, best_cad = v, cad
    return best, best_cad


def check_job(job: str, dur: float, depth: float, step: float) -> bool:
    pkg, const_attr = JOBS[job]
    sim_mod = importlib.import_module(f"jobs.{pkg}.simulator")
    sc_mod = importlib.import_module(f"jobs.{pkg}.scoring")
    const = getattr(sim_mod, const_attr)
    ideal = sc_mod.idealized_at_duration

    base, _ = ceiling_at(ideal, dur, const, const)   # the calibrated constant ceiling
    print(f"\n=== {job}  (constant {const:.3f}s, {dur:.0f}s fight) ===")
    print(f"  {'gear':>6} {'ceiling':>10} {'vs const':>9} {'argmax cad':>11}")
    print(f"  {const:>6.3f} {base:>10.0f} {'+0.00%':>9}   (anchor)")

    ok = True
    prev = base
    n = int(round(depth / step))
    for k in range(1, n + 1):
        gear = const - k * step
        ceil, cad = ceiling_at(ideal, dur, gear, const)
        rel = (ceil / base - 1.0) * 100.0
        marks = []
        if ceil < base - 1e-6:
            ok = False
            marks.append("BELOW CONSTANT — anchor violated!")
        if ceil < prev - 1e-6:
            marks.append("dip vs slower gear (informational)")
        print(f"  {gear:>6.3f} {ceil:>10.0f} {rel:>+8.2f}% {cad:>11.3f}  {' '.join(marks)}")
        prev = ceil
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", nargs="*", default=["Paladin", "Machinist", "Reaper"])
    ap.add_argument("--dur", type=float, default=390.0)
    ap.add_argument("--depth", type=float, default=0.06,
                    help="how far below the constant to scan (s)")
    ap.add_argument("--step", type=float, default=0.01)
    args = ap.parse_args()

    all_ok = True
    for job in args.jobs:
        if job not in JOBS:
            print(f"unknown job {job!r} (known: {', '.join(JOBS)})")
            return 2
        all_ok &= check_job(job, args.dur, args.depth, args.step)
    print("\nPASS: every threaded ceiling >= the calibrated constant ceiling"
          if all_ok else "\nFAIL: anchor violated (see above)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
