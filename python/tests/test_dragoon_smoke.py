"""Dragoon smoke test — registration / wiring, end-to-end through the registry.

NOTE: a registration test only. The full per-fixture integration lives in
test_dragoon_sim.py (synthetic) + test_dragoon_pulls.py (real, once fixtures exist).

Run from python/:  python tests/test_dragoon_smoke.py
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
    print("Test: Dragoon registers with the expected aspect list")
    drg = jobs.get_job("Dragoon")
    _check("get_job('Dragoon') returns a Job", drg is not None)
    _check("name is 'Dragoon'", drg.name == "Dragoon")

    aspect_names = [a.name for a in drg.aspects]
    expected = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring", "Positionals", "LifeSurge"]
    _check(f"aspect list = {expected}",
           aspect_names == expected,
           f"got {aspect_names}")

    _check("has a simulator (DRG ships a full idealized sim)",
           drg.simulator is not None)
    _check("has improvement_contributors (positionals + Life Surge)",
           drg.improvement_contributors is not None)

    print()
    print("Test: JobData is populated")
    d = drg.data
    _check("potencies non-empty", len(d.potencies) > 0)
    _check("cooldowns non-empty", len(d.cooldowns) > 0)
    _check("canonical_opener has 12 slots",
           len(d.canonical_opener) == 12,
           f"got {len(d.canonical_opener)}")
    _check("1 gauge model (focus; LotD is granted directly in 7.x, not a gauge)",
           len(d.gauges) == 1,
           f"got {len(d.gauges)}")
    gauge_names = sorted(g.name for g in d.gauges)
    _check("gauge names = ['focus']",
           gauge_names == ["focus"],
           f"got {gauge_names}")
    _check("no ranged filler (pure melee with gap-closer jumps)",
           d.ranged_filler_id is None)
    _check("splash_potencies set (free-splash burst oGCDs)",
           len(d.splash_potencies) > 0)
    from jobs.dragoon.data import POSITIONAL_IDS
    _check("three positionals (Chaotic Spring / Wheeling Thrust / Fang and Claw)",
           len(POSITIONAL_IDS) == 3,
           f"got {len(POSITIONAL_IDS)}")

    print()
    print("Test: Samurai still registers (didn't break an existing job)")
    sam = jobs.get_job("Samurai")
    _check("get_job('Samurai') still works", sam is not None)
    _check("Samurai aspects > 5", len(sam.aspects) > 5, f"got {len(sam.aspects)}")

    print()
    print("============================================================")
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


def test_dragoon_smoke() -> None:
    """pytest entry: the registry/data path for the 11th job."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
