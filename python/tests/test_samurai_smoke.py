"""Samurai smoke test — proves the job-generic refactor works for a
second job, end-to-end through the registry.

NOTE: this is a registration / wiring test only. A full per-fixture
integration test for SAM requires recorded SAM cast events in
tests/fixtures/ — once those exist, model after test_execution.py.

Run from project root:  python tests/test_samurai_smoke.py
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
    print("Test: Samurai registers with the expected aspect list")
    sam = jobs.get_job("Samurai")
    _check("get_job('Samurai') returns a Job", sam is not None)
    _check("name is 'Samurai'", sam.name == "Samurai")

    aspect_names = [a.name for a in sam.aspects]
    expected = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                "Alignment", "BuffDrift", "Scoring"]
    _check(f"aspect list = {expected}",
           aspect_names == expected,
           f"got {aspect_names}")

    _check("has a simulator (SAM ships a full idealized sim)",
           sam.simulator is not None)

    print()
    print("Test: JobData is populated")
    d = sam.data
    _check("potencies non-empty", len(d.potencies) > 0)
    _check("cooldowns non-empty", len(d.cooldowns) > 0)
    _check("canonical_opener has 12 slots",
           len(d.canonical_opener) == 12,
           f"got {len(d.canonical_opener)}")
    _check("1 gauge model (kenki; Sen tracked in the sim, not overcap-prone)",
           len(d.gauges) == 1,
           f"got {len(d.gauges)}")
    gauge_names = sorted(g.name for g in d.gauges)
    _check("gauge names = ['kenki']",
           gauge_names == ["kenki"],
           f"got {gauge_names}")

    print()
    print("Test: Machinist still registers (didn't break existing job)")
    mch = jobs.get_job("Machinist")
    _check("get_job('Machinist') still works", mch is not None)
    _check("Machinist name is 'Machinist'", mch.name == "Machinist")
    _check("Machinist aspects > 5 (split + MCH-specific)",
           len(mch.aspects) > 5,
           f"got {len(mch.aspects)}")

    print()
    print(f"============================================================")
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


def test_samurai_smoke() -> None:
    """pytest entry: the registry/data-only path for a second job."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
