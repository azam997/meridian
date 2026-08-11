"""Consensus ranged-filler (disconnect) window tests.

Tier-B's sibling: where >= consensus_pct of refs cast the job's ranged
filler (RPR Harpe) at the same fight time, the LENIENT ceiling swaps its
melee GCDs for the filler there. Covers the detection vote
(`ranged_filler_windows_from_refs`), the sim-context plumbing
(`RangedFillerContext` -> `_model_for`), and the RPR model honoring the
windows (ranged-legal GCDs only inside; ceiling strictly lower).

Run from python/:  python tests/test_ranged_windows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import (
    RangedFillerContext,
    RefRun,
    ranged_filler_windows_from_refs,
)
from jobs._core.gcd_speed import CeilingContext
from jobs._core.job import MELEE_DPS
from jobs.reaper import simulator as rpr
from jobs.reaper.data import HARPE
from jobs.reaper.scoring import score_delivered_potency


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


def _melee_stream(duration_s: float,
                  harpe_at: tuple[float, ...] = (),
                  gcd_s: float = 2.5) -> tuple[tuple[float, int], ...]:
    """Continuous GCD stream with Harpe casts injected at `harpe_at`."""
    out: list[tuple[float, int]] = []
    t = 0.0
    aids = [24373, 24374, 24375]
    i = 0
    while t < duration_s:
        out.append((t, aids[i % 3]))
        t += gcd_s
        i += 1
    for ht in harpe_at:
        out.append((ht, HARPE))
    return tuple(sorted(out))


# --- Detection --------------------------------------------------------------

def test_consensus_cluster_fires() -> None:
    print()
    print("Test: >=75% of refs casting Harpe at ~t=300 -> one window")
    refs = [RefRun(f"r{i}", _melee_stream(500, harpe_at=(298.0 + i,)), 500.0)
            for i in range(8)]  # 8/8 casting in a tight cluster
    out = ranged_filler_windows_from_refs(refs, 500.0, MELEE_DPS, HARPE, [])
    _check("one window detected", len(out) == 1, f"got {out}")
    w = out[0]
    _check("window covers the cluster", w.start_s < 300.0 < w.end_s,
           f"got [{w.start_s}, {w.end_s}]")
    _check("n_casting carried", w.n_idle >= 6, f"got {w.n_idle}")


def test_below_consensus_no_window() -> None:
    print()
    print("Test: only 3/8 refs casting -> no window (MELEE_DPS pct=0.75)")
    refs = ([RefRun(f"c{i}", _melee_stream(500, harpe_at=(300.0,)), 500.0)
             for i in range(3)]
            + [RefRun(f"n{i}", _melee_stream(500), 500.0) for i in range(5)])
    out = ranged_filler_windows_from_refs(refs, 500.0, MELEE_DPS, HARPE, [])
    _check("no window", out == [], f"got {out}")


def test_opener_precast_excluded() -> None:
    print()
    print("Test: the t~0 pre-pull Harpe never forms a window")
    refs = [RefRun(f"r{i}", _melee_stream(500, harpe_at=(0.2,)), 500.0)
            for i in range(8)]
    out = ranged_filler_windows_from_refs(refs, 500.0, MELEE_DPS, HARPE, [])
    _check("opener excluded", out == [], f"got {out}")


def test_below_min_ref_count() -> None:
    print()
    print("Test: < min_ref_count refs -> empty")
    refs = [RefRun(f"r{i}", _melee_stream(500, harpe_at=(300.0,)), 500.0)
            for i in range(3)]  # MELEE_DPS min_ref_count = 4
    out = ranged_filler_windows_from_refs(refs, 500.0, MELEE_DPS, HARPE, [])
    _check("suppressed", out == [], f"got {out}")


def _gapped_stream(duration_s: float, gap_start: float, gap_end: float,
                   gcd_s: float = 2.5) -> tuple[tuple[float, int], ...]:
    return tuple((t, aid) for t, aid in _melee_stream(duration_s, gcd_s=gcd_s)
                 if not (gap_start <= t < gap_end))


def test_union_with_idle_evidence() -> None:
    """The same forced disconnect shows as Harpe on some refs and as a short
    idle on others (the measured M10S shape). Neither alone reaches the 0.75
    bar; the union vote (data= mode) does."""
    print()
    print("Test: 5/8 Harpe + 3/8 idle at ~t=300 -> union window fires")
    from jobs.reaper.data import JOB_DATA as RPR_DATA
    refs = ([RefRun(f"h{i}", _melee_stream(500, harpe_at=(299.0,)), 500.0)
             for i in range(5)]
            + [RefRun(f"i{i}", _gapped_stream(500, 296.0, 306.0), 500.0)
               for i in range(3)])
    without = ranged_filler_windows_from_refs(
        refs, 500.0, MELEE_DPS, HARPE, [])
    _check("filler-only vote stays below the bar", without == [],
           f"got {without}")
    union = ranged_filler_windows_from_refs(
        refs, 500.0, MELEE_DPS, HARPE, [], data=RPR_DATA)
    _check("union vote fires", len(union) == 1, f"got {union}")
    _check("window covers the disconnect",
           union[0].start_s < 300.0 < union[0].end_s,
           f"got {union}")


def test_exclude_windows_subtracted() -> None:
    print()
    print("Test: a Tier-B window passed as exclude is carved out")
    refs = [RefRun(f"r{i}", _melee_stream(500, harpe_at=(300.0,)), 500.0)
            for i in range(8)]
    out = ranged_filler_windows_from_refs(
        refs, 500.0, MELEE_DPS, HARPE, [], exclude_windows=[(290.0, 310.0)])
    _check("nothing left after exclusion",
           all(e <= 290.0 or s >= 310.0 for w in out
               for s, e in [(w.start_s, w.end_s)]),
           f"got {out}")


def test_tier_a_subtracted() -> None:
    print()
    print("Test: a Tier-A window overlapping the cluster is subtracted")
    refs = [RefRun(f"r{i}", _melee_stream(500, harpe_at=(300.0,)), 500.0)
            for i in range(8)]
    tier_a = [(296.0, 320.0)]
    out = ranged_filler_windows_from_refs(refs, 500.0, MELEE_DPS, HARPE,
                                          tier_a)
    _check("no piece inside Tier A",
           all(e <= 296.0 or s >= 320.0 for w in out
               for s, e in [(w.start_s, w.end_s)]),
           f"got {out}")


# --- Context plumbing -------------------------------------------------------

def test_model_for_unwraps_nested_context() -> None:
    print()
    print("Test: _model_for unwraps CeilingContext(RangedFillerContext(...))")
    win = ((100.0, 110.0),)
    ctx = CeilingContext(gcd_base_s=2.5,
                         payload=RangedFillerContext(inner=None, windows=win))
    model = rpr._model_for(ctx)
    _check("windows threaded", model.ranged_windows == win,
           f"got {model.ranged_windows}")
    _check("entry stays None", model.entry is None)
    bare = rpr._model_for(RangedFillerContext(inner=None, windows=win))
    _check("bare wrapper (no CeilingContext) also threads",
           bare.ranged_windows == win, f"got {bare.ranged_windows}")
    none_model = rpr._model_for(None)
    _check("None context -> no windows", none_model.ranged_windows == ())


# --- Sim behavior ------------------------------------------------------------

_MELEE_GCDS = frozenset({
    rpr.SLICE, rpr.WAXING_SLICE, rpr.INFERNAL_SLICE, rpr.SOUL_SLICE,
    rpr.SHADOW_OF_DEATH, rpr.GIBBET, rpr.GALLOWS, rpr.EXEC_GIBBET,
    rpr.EXEC_GALLOWS, rpr.PLENTIFUL_HARVEST,
})


def test_sim_honors_windows() -> None:
    print()
    print("Test: the sim bridges ranged windows with ranged-legal GCDs only")
    win = ((100.0, 112.0), (160.0, 170.0))
    ctx = RangedFillerContext(inner=None, windows=win)
    timeline, _aux = rpr.simulate_idealized(240.0, [], sim_context=ctx)
    inside = [aid for t, aid in timeline
              if any(s <= t < e for s, e in win)]
    _check("no melee GCD inside a window",
           not (set(inside) & _MELEE_GCDS),
           f"melee inside: {set(inside) & _MELEE_GCDS}")
    _check("the filler actually fires", HARPE in inside,
           f"in-window casts: {inside}")

    base_tl, _ = rpr.simulate_idealized(240.0, [], sim_context=None)
    with_score = score_delivered_potency([c for c in timeline])
    base_score = score_delivered_potency([c for c in base_tl])
    _check("windowed ceiling strictly below the free ceiling",
           with_score < base_score,
           f"with={with_score} base={base_score}")


def test_empty_windows_byte_identical() -> None:
    print()
    print("Test: an empty-window context is byte-identical to None")
    tl_none, aux_none = rpr.simulate_idealized(200.0, [], sim_context=None)
    tl_empty, aux_empty = rpr.simulate_idealized(
        200.0, [], sim_context=RangedFillerContext(inner=None, windows=()))
    _check("timelines identical", list(tl_none) == list(tl_empty))
    _check("aux identical", aux_none == aux_empty)


def main() -> int:
    for fn in (test_consensus_cluster_fires, test_below_consensus_no_window,
               test_opener_precast_excluded, test_below_min_ref_count,
               test_union_with_idle_evidence, test_exclude_windows_subtracted,
               test_tier_a_subtracted, test_model_for_unwraps_nested_context,
               test_sim_honors_windows, test_empty_windows_byte_identical):
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed.")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
