"""Gunbreaker smoke test — registration / wiring, end-to-end through the registry.

NOTE: a registration test only. The full per-fixture integration lives in
test_gunbreaker_sim.py (synthetic) + test_gunbreaker_pulls.py (real, once fixtures exist).

Run from python/:  python tests/test_gunbreaker_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    print()
    print("Test: Gunbreaker registers with the expected aspect list")
    gnb = jobs.get_job("Gunbreaker")
    _check("get_job('Gunbreaker') returns a Job", gnb is not None)
    _check("name is 'Gunbreaker'", gnb.name == "Gunbreaker")

    aspect_names = [a.name for a in gnb.aspects]
    expected = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]
    _check(f"aspect list = {expected}",
           aspect_names == expected,
           f"got {aspect_names}")

    _check("has a simulator (GNB ships a full idealized sim)",
           gnb.simulator is not None)

    print()
    print("Test: JobData is populated")
    d = gnb.data
    _check("potencies non-empty", len(d.potencies) > 0)
    _check("cooldowns non-empty", len(d.cooldowns) > 0)
    _check("canonical_opener has 12 slots",
           len(d.canonical_opener) == 12,
           f"got {len(d.canonical_opener)}")
    _check("1 gauge model (cartridge / Powder Gauge)",
           len(d.gauges) == 1,
           f"got {len(d.gauges)}")
    gauge_names = sorted(g.name for g in d.gauges)
    _check("gauge names = ['cartridges']",
           gauge_names == ["cartridges"],
           f"got {gauge_names}")
    _check("cartridge cap is 3 (base; Bloodfest raises it to 6 in-sim)",
           d.gauges[0].cap == 3,
           f"got {d.gauges[0].cap}")
    _check("no ranged filler (pure melee with gap-closer)",
           d.ranged_filler_id is None)
    _check("splash_potencies set (free-splash cleaving casts)",
           len(d.splash_potencies) > 0)
    _check("aoe_potencies set (dedicated AoE line)",
           len(d.aoe_potencies) > 0)
    _check("tincture modeled (tank Strength)",
           d.tincture_main_stat is not None)
    from jobs.gunbreaker.data import DEFENSIVE_IDS
    _check("defensive_ids populated (tank Defensives lane)",
           len(DEFENSIVE_IDS) >= 8,
           f"got {len(DEFENSIVE_IDS)}")

    print()
    print("Test: Dragoon still registers (didn't break an existing job)")
    drg = jobs.get_job("Dragoon")
    _check("get_job('Dragoon') still works", drg is not None)
    _check("Dragoon aspects > 5", len(drg.aspects) > 5, f"got {len(drg.aspects)}")

    print()
    print("============================================================")
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


def test_gunbreaker_smoke() -> None:
    """pytest entry: the registry/data path for the 12th job."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
