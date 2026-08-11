"""Unit tests for the residual attribution split
(jobs/_core/improvements.py::split_residual).

The reconcile residual ("Spacing & sequencing") gets its attributable slices
moved into named `residual_tail` sibling cards via the per-ability count diff
(delivered vs strict idealized), with double-count guardrails (cooldowns /
rng procs / filler-quality GCDs excluded) and the top-level sum conserved.

Run from python/:  python tests/test_residual_split.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.improvements import (
    _SPLIT_KEEP_FLOOR,
    Improvement,
    reconcile_to_budget,
    split_residual,
)
from jobs._core.job import JobData

# Real ability ids so ability_metadata knows their GCD/oGCD-ness.
DRILL = 16498         # GCD (660p on MCH)
DOUBLE_CHECK = 36979  # damaging oGCD

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _data(**kw) -> JobData:
    """Synthetic JobData: DRILL as a NON-cooldown 500p GCD over a 300p filler,
    DOUBLE_CHECK as a non-cooldown 180p oGCD (nothing excluded by default)."""
    base = dict(job_name="Test", patch_version="7.x",
                potencies={DRILL: 500, DOUBLE_CHECK: 180},
                filler_gcd_potency=300)
    base.update(kw)
    return JobData(**base)


def _residual(pot: float) -> Improvement:
    return Improvement("residual", 0, "", 0.0, pot, "Spacing & sequencing")


def test_gcd_premium_bucket() -> None:
    print("\nTest: non-cooldown GCD deficit prices at the above-filler premium")
    # Sim lands 3 more Drills; premium (500-300)=200 each → 600p bucket.
    ideal = [(float(i), DRILL) for i in range(5)]
    player = [(float(i), DRILL) for i in range(2)]
    out = split_residual([_residual(1000.0)], player, ideal, _data())
    tails = [c for c in out if c.kind == "residual_tail"]
    res = next(c for c in out if c.kind == "residual")
    _check("one GCD-mix bucket", len(tails) == 1, f"got {len(tails)}")
    _check("priced 3 × (500−300) = 600",
           abs(tails[0].lost_potency - 600.0) < 0.01,
           f"got {tails[0].lost_potency}")
    _check("residual shrunk by the bucket (1000−600=400)",
           abs(res.lost_potency - 400.0) < 0.01, f"got {res.lost_potency}")
    _check("top-level sum conserved",
           abs(sum(c.lost_potency for c in out) - 1000.0) < 0.01,
           f"got {sum(c.lost_potency for c in out)}")
    _check("bucket names the dominant ability",
           "Drill" in tails[0].summary and tails[0].ability_id == DRILL,
           f"got {tails[0].summary!r}")
    _check("bucket is non-located (no timeline noise)",
           tails[0].time_s == 0.0, f"got {tails[0].time_s}")
    _check("bucket carries a prescription",
           bool(tails[0].prescription), f"got {tails[0].prescription!r}")


def test_ogcd_bucket_full_value() -> None:
    print("\nTest: non-cooldown damaging oGCD deficit prices at full value")
    # Sim lands 2 more Double Checks → 2 × 180 = 360p.
    ideal = [(float(i), DOUBLE_CHECK) for i in range(4)]
    player = [(float(i), DOUBLE_CHECK) for i in range(2)]
    out = split_residual([_residual(800.0)], player, ideal, _data())
    tails = [c for c in out if c.kind == "residual_tail"]
    _check("one oGCD bucket", len(tails) == 1, f"got {len(tails)}")
    _check("priced 2 × 180 = 360",
           abs(tails[0].lost_potency - 360.0) < 0.01,
           f"got {tails[0].lost_potency}")
    _check("oGCD wording (displaces no GCD)",
           tails[0].prescription is not None
           and "displace no GCD" in tails[0].prescription,
           f"got {tails[0].prescription!r}")


def test_scaling_cap_preserves_keep_floor() -> None:
    print("\nTest: buckets scale down to residual − keep-floor when over")
    # Raw bucket = 5 × 200 = 1000p but residual is only 460 → avail 400.
    ideal = [(float(i), DRILL) for i in range(7)]
    player = [(float(i), DRILL) for i in range(2)]
    out = split_residual([_residual(460.0)], player, ideal, _data())
    tails = [c for c in out if c.kind == "residual_tail"]
    res = next(c for c in out if c.kind == "residual")
    _check("bucket capped at avail (400)",
           abs(tails[0].lost_potency - 400.0) < 0.01,
           f"got {tails[0].lost_potency}")
    _check("residual keeps the floor (60)",
           abs(res.lost_potency - _SPLIT_KEEP_FLOOR) < 0.01,
           f"got {res.lost_potency}")
    _check("sum conserved at 460",
           abs(sum(c.lost_potency for c in out) - 460.0) < 0.01)


def test_guardrail_exclusions() -> None:
    print("\nTest: cooldown / rng-proc / filler-quality abilities are excluded")
    ideal = [(float(i), DRILL) for i in range(5)]
    player = [(float(i), DRILL) for i in range(2)]
    as_cooldown = _data(cooldowns={DRILL: (20.0, 1)})
    _check("cooldown ability → passthrough (missed-cast diff owns it)",
           split_residual([_residual(1000.0)], player, ideal, as_cooldown)
           == [_residual(1000.0)], "expected passthrough")
    as_proc = _data(rng_proc_ids=frozenset({DRILL}))
    _check("rng-proc ability → passthrough",
           split_residual([_residual(1000.0)], player, ideal, as_proc)
           == [_residual(1000.0)], "expected passthrough")
    as_fq = _data(filler_quality_gcds=frozenset({DRILL}))
    _check("filler-quality ability → passthrough (Filler card owns it)",
           split_residual([_residual(1000.0)], player, ideal, as_fq)
           == [_residual(1000.0)], "expected passthrough")


def test_passthrough_cases() -> None:
    print("\nTest: passthrough when nothing to split")
    ideal = [(0.0, DRILL)]
    player = [(0.0, DRILL)]
    _check("no residual card → unchanged",
           split_residual([], player, ideal, _data()) == [], "expected []")
    _check("small residual (< bucket+keep floors) → unchanged",
           split_residual([_residual(200.0)], player, ideal, _data())
           == [_residual(200.0)], "expected passthrough")
    _check("no count deficit → unchanged",
           split_residual([_residual(1000.0)], player, ideal, _data())
           == [_residual(1000.0)], "expected passthrough")
    # A bucket whose raw value is below the floor is dropped, not emitted as
    # noise — its potency stays in the residual.
    ideal2 = [(0.0, DOUBLE_CHECK)]
    player2: list[tuple[float, int]] = []
    small = _data(potencies={DOUBLE_CHECK: 140})
    _check("sub-floor bucket (1 × 140 < 150) dropped, residual unchanged",
           split_residual([_residual(1000.0)], player2, ideal2, small)
           == [_residual(1000.0)], "expected passthrough")


def test_residual_summary_renamed() -> None:
    print("\nTest: reconcile residual now titled 'Spacing & sequencing'")
    cards = [Improvement("missed_cast", DRILL, "Drill", 40.0, 340.0, "x")]
    out = reconcile_to_budget(cards, 500.0)
    res = next(i for i in out if i.kind == "residual")
    _check("bare residual title", res.summary == "Spacing & sequencing",
           f"got {res.summary!r}")
    out = reconcile_to_budget(
        cards, 500.0,
        extra_children=[Improvement("idle", 0, "", 10.0, 40.0, "Idle 0.3s")])
    res = next(i for i in out if i.kind == "residual")
    _check("with children: counts the located losses",
           res.summary == "Spacing & sequencing: 1 small located loss "
                          "below the listing threshold",
           f"got {res.summary!r}")


def main() -> int:
    test_gcd_premium_bucket()
    test_ogcd_bucket_full_value()
    test_scaling_cap_preserves_keep_floor()
    test_guardrail_exclusions()
    test_passthrough_cases()
    test_residual_summary_renamed()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
