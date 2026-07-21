"""Tier-B consensus aggregation tests.

Synthetic refs (build their cast streams in code) exercise the
agreement-threshold and Tier-A-subtraction behavior. Real fixture
behavior is covered later in the dual-idealized test (Phase 5).

Run from python/:  python tests/test_tier_b_consensus.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import (
    ConsensusWindow,
    RefRun,
    consensus_windows_from_refs,
)
from jobs._core.job import PHYSICAL_RANGED
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


def _clean_cast_stream(duration_s: float, gcd_s: float = 2.5
                       ) -> tuple[tuple[float, int], ...]:
    """Continuous 2.5s GCDs filling `duration_s`. No gaps."""
    out: list[tuple[float, int]] = []
    t = 0.0
    aids = [7411, 7412, 7413]
    i = 0
    while t < duration_s:
        out.append((t, aids[i % 3]))
        t += gcd_s
        i += 1
    return tuple(out)


def _stream_with_gap(duration_s: float,
                      gap_start: float, gap_end: float,
                      gcd_s: float = 2.5
                      ) -> tuple[tuple[float, int], ...]:
    """Same as _clean_cast_stream but no casts inside [gap_start, gap_end]."""
    return tuple((t, aid)
                 for t, aid in _clean_cast_stream(duration_s, gcd_s)
                 if not (gap_start <= t < gap_end))


# --- Suppression / threshold tests -----------------------------------------

def test_below_min_ref_count_returns_empty() -> None:
    print()
    print("Test: < policy.min_ref_count refs -> empty list")
    refs = [RefRun("r1", _stream_with_gap(200, 100, 110), 200.0),
            RefRun("r2", _stream_with_gap(200, 100, 110), 200.0),
            RefRun("r3", _stream_with_gap(200, 100, 110), 200.0)]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("3 refs vs P-Range min=4 => no consensus", out == [], f"got {out}")


def test_caster_healer_demands_more_refs() -> None:
    """CASTER_HEALER policy has min_ref_count=5. Exactly 4 idle refs
    should NOT produce a window for casters even though it would for
    physical_ranged."""
    print()
    print("Test: CASTER_HEALER requires 5 refs (P-Range requires 4)")
    from jobs._core.job import CASTER_HEALER
    refs = [RefRun(f"r{i}", _stream_with_gap(200, 100, 110, gcd_s=3.0), 200.0)
            for i in range(4)]
    p_out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    c_out = consensus_windows_from_refs(
        refs, 200.0, CASTER_HEALER, MCH_DATA, tier_a_windows=[])
    _check("P-Range accepts 4-ref consensus", len(p_out) == 1,
           f"got {len(p_out)}")
    _check("CASTER_HEALER rejects 4-ref consensus",
           c_out == [], f"got {c_out}")


def test_full_consensus_at_4_refs() -> None:
    print()
    print("Test: 4 refs all idle 100-110s -> one window")
    refs = [RefRun(f"r{i}", _stream_with_gap(200, 100, 110), 200.0)
            for i in range(4)]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("one window", len(out) == 1, f"got {len(out)}")
    if out:
        w = out[0]
        # Cast cadence means the gap actually begins from the last kept
        # cast (t=97.5) — one GCD before the requested gap start.
        _check(f"window covers ~97.5-110s (got {w.start_s}-{w.end_s})",
               abs(w.start_s - 97.5) < 1.0 and abs(w.end_s - 110) < 1.0)
        _check("n_idle = 4", w.n_idle == 4, f"got {w.n_idle}")
        _check("n_total = 4", w.n_total == 4, f"got {w.n_total}")


def test_minority_consensus_suppressed() -> None:
    print()
    print("Test: 1 of 5 refs idle -> no window")
    refs = [RefRun("r0", _stream_with_gap(200, 100, 110), 200.0)] + [
        RefRun(f"r{i}", _clean_cast_stream(200), 200.0) for i in range(1, 5)
    ]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("no consensus window", out == [], f"got {out}")


def test_majority_consensus_passes_threshold() -> None:
    """At 60% threshold (P-Range), 3/5 (60%) and 4/5 (80%) should produce
    windows; 2/5 (40%) shouldn't."""
    print()
    print("Test: P-Range 60% threshold - 3/5 idle -> window")
    refs = [RefRun(f"r{i}", _stream_with_gap(200, 100, 110), 200.0)
            for i in range(3)] + [
        RefRun(f"r{i}", _clean_cast_stream(200), 200.0) for i in range(3, 5)
    ]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("one window at 3/5",
           len(out) == 1 and out[0].n_idle == 3 and out[0].n_total == 5,
           f"got {out}")


def test_tier_a_subtracted_from_consensus() -> None:
    """If Tier A covers the same window, the consensus output should be
    empty — Tier A wins, no double-count."""
    print()
    print("Test: Tier A subtracted from consensus result")
    refs = [RefRun(f"r{i}", _stream_with_gap(200, 100, 110), 200.0)
            for i in range(5)]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA,
        tier_a_windows=[(95.0, 115.0)],
    )
    _check("Tier A covers it -> no Tier B window",
           out == [], f"got {out}")


def test_partial_tier_a_overlap_produces_uncovered_slice() -> None:
    """Tier A covers part of the consensus window; Tier B should report
    only the uncovered slices."""
    print()
    print("Test: partial Tier A overlap leaves uncovered slice")
    # Refs idle from t=97.5 (last kept cast) through to t=122.5 (next
    # cast after gap), since requested gap is 100-120 inclusive of
    # cast slots at 120 too.
    refs = [RefRun(f"r{i}", _stream_with_gap(200, 100, 122.5), 200.0)
            for i in range(5)]
    # Tier A covers 100..110 (interior of the idle range).
    # Uncovered slices: (97.5, 100) and (110, 122.5).
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA,
        tier_a_windows=[(100.0, 110.0)],
    )
    _check("two slices remain",
           len(out) == 2, f"got {len(out)}")
    if len(out) == 2:
        a, b = sorted(out, key=lambda w: w.start_s)
        _check(f"slice 1 ~ (97.5, 100) (got {a.start_s}-{a.end_s})",
               abs(a.start_s - 97.5) < 1.0 and abs(a.end_s - 100) < 1.0)
        _check(f"slice 2 ~ (110, 122.5) (got {b.start_s}-{b.end_s})",
               abs(b.start_s - 110) < 1.0 and abs(b.end_s - 122.5) < 1.0)


def test_short_ref_doesnt_block_consensus() -> None:
    """A short ref (below MIN_REF_DURATION) shouldn't count toward the
    pool. If only short refs remain, return [] not a crash."""
    print()
    print("Test: short refs excluded from pool")
    short_refs = [
        RefRun(f"r{i}", _clean_cast_stream(15), 15.0) for i in range(4)
    ]
    out = consensus_windows_from_refs(
        short_refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("only short refs -> no consensus", out == [], f"got {out}")


def test_min_idle_records_worst_case() -> None:
    """If 5 refs are idle during one tick and 4 are idle during another
    in the same window, n_idle should be 4 (the worst-case agreement)."""
    print()
    print("Test: window n_idle = worst-case across the window")
    # 5 refs idle 100-105. 4 of them (all but r0) extend idle to 105-110.
    refs = [
        RefRun("r0", _stream_with_gap(200, 100, 105), 200.0),
        RefRun("r1", _stream_with_gap(200, 100, 110), 200.0),
        RefRun("r2", _stream_with_gap(200, 100, 110), 200.0),
        RefRun("r3", _stream_with_gap(200, 100, 110), 200.0),
        RefRun("r4", _stream_with_gap(200, 100, 110), 200.0),
    ]
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[])
    _check("one merged window",
           len(out) == 1, f"got {out}")
    if out:
        _check("n_idle reflects worst-case 4",
               out[0].n_idle == 4, f"got {out[0].n_idle}")


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_below_min_ref_count_returns_empty()
    test_full_consensus_at_4_refs()
    test_minority_consensus_suppressed()
    test_majority_consensus_passes_threshold()
    test_tier_a_subtracted_from_consensus()
    test_partial_tier_a_overlap_produces_uncovered_slice()
    test_short_ref_doesnt_block_consensus()
    test_min_idle_records_worst_case()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
