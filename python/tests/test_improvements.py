"""Unit tests for the sim-diff Potential Improvements engine
(jobs/_core/improvements.py).

Run from python/:  python tests/test_improvements.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass

from jobs._core.improvements import (
    Improvement,
    _unmatched_idealized,
    compute_missed_cast_improvements,
    diagnostics_from_opener,
    group_improvements,
    improvements_from_clipping,
    improvements_from_gcd_quality,
    improvements_from_hypercharge_windows,
    improvements_from_wildfire_windows,
    reconcile_to_budget,
)
from jobs._aspects.clipping import ClippingFinding
from jobs._aspects.opener import OpenerFinding
from jobs.machinist.data import JOB_DATA as MCH
from jobs.redmage.data import (
    JOB_DATA as RDM, JOLT_III, VERAERO_III, VERTHUNDER_III,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


DRILL = 16498        # GCD tool, 660p, filler-backfilled
WILDFIRE = 2878      # enabler oGCD, no direct potency → zero-priced diagnostic
DOUBLE_CHECK = 36979  # damaging oGCD, 180p, full value when missed


def test_unmatched_greedy() -> None:
    print("\nTest: _unmatched_idealized pairs nearest, returns the rest")
    # ideal at 0,20,40; actual at 2,21 -> 0 and 20 consumed, 40 unmatched
    out = _unmatched_idealized([0.0, 20.0, 40.0], [2.0, 21.0])
    _check("40 left unmatched", out == [40.0], f"got {out}")


def test_no_miss_when_counts_match() -> None:
    print("\nTest: counts match => no improvements")
    ideal = [(0.0, DRILL), (20.0, DRILL)]
    actual = [(1.0, DRILL), (22.0, DRILL)]
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 200.0)
    _check("no improvements", out == [], f"got {out}")


def test_missed_gcd_tool_marginal() -> None:
    print("\nTest: missed Drill priced at opportunity cost (660-320)")
    ideal = [(0.0, DRILL), (20.0, DRILL), (40.0, DRILL)]
    actual = [(0.0, DRILL), (22.0, DRILL)]   # 2 of 3
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 100.0)
    _check("one missed-cast improvement", len(out) == 1, f"got {len(out)}")
    if out:
        im = out[0]
        _check("located at the unmatched idealized time (40s)",
               abs(im.time_s - 40.0) < 0.01, f"got {im.time_s}")
        _check("priced marginal 660-320=340",
               abs(im.lost_potency - 340.0) < 0.01, f"got {im.lost_potency}")
        _check("summary mentions the ability + time",
               "Drill" in im.summary and "0:40" in im.summary,
               f"got {im.summary!r}")


def test_missed_damaging_ogcd_full_value() -> None:
    print("\nTest: missed damaging oGCD (Double Check) costs full value")
    ideal = [(0.0, DOUBLE_CHECK), (30.0, DOUBLE_CHECK)]
    actual = [(0.0, DOUBLE_CHECK)]
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 100.0)
    _check("one improvement", len(out) == 1, f"got {len(out)}")
    if out:
        # potency 180, no filler subtraction for an oGCD
        _check("full value ~180 (no filler subtraction)",
               abs(out[0].lost_potency - 180.0) < 1.0, f"got {out[0].lost_potency}")
        _check("kind is missed_cast", out[0].kind == "missed_cast", out[0].kind)


def test_missed_enabler_priced_from_sim_value() -> None:
    print("\nTest: missed enabler (Wildfire) priced at its sim-derived net value")
    ideal = [(0.0, WILDFIRE), (120.0, WILDFIRE)]
    actual = [(0.0, WILDFIRE)]
    # With a sim-derived net value supplied, the miss is a priced loss.
    out = compute_missed_cast_improvements(
        actual, ideal, MCH, None, 200.0, enabler_values={WILDFIRE: 1440.0})
    _check("one item", len(out) == 1, f"got {len(out)}")
    if out:
        im = out[0]
        _check("priced at the net value (1440)",
               abs(im.lost_potency - 1440.0) < 0.01, f"got {im.lost_potency}")
        _check("kind is missed_enabler", im.kind == "missed_enabler", im.kind)
        _check("located at the missed time (120s)",
               abs(im.time_s - 120.0) < 0.01, f"got {im.time_s}")

    # No value (or below floor) → zero-priced diagnostic note, still located.
    note = compute_missed_cast_improvements(actual, ideal, MCH, None, 200.0)
    _check("falls back to a zero-priced note",
           len(note) == 1 and note[0].lost_potency == 0.0, f"got {note}")


def test_wildfire_window_underfill_priced() -> None:
    print("\nTest: underfilled Wildfire window priced at (6-hits)×240")

    @dataclass
    class _W:
        cast_time_s: float
        hits: int

    state = {"windows": [_W(100.0, 5), _W(220.0, 6), _W(330.0, 3)]}
    out = improvements_from_wildfire_windows(state)
    _check("only the underfilled windows surface (5/6 and 3/6)",
           len(out) == 2, f"got {len(out)}")
    by_t = {round(i.time_s): i for i in out}
    _check("5/6 window short 1 hit = 240p",
           abs(by_t[100].lost_potency - 240.0) < 0.01, f"got {by_t[100].lost_potency}")
    _check("3/6 window short 3 hits = 720p",
           abs(by_t[330].lost_potency - 720.0) < 0.01, f"got {by_t[330].lost_potency}")


def test_hypercharge_underfill_priced_from_sim_value() -> None:
    print("\nTest: underfilled Hypercharge priced at (5-hits)×per-shot sim value")

    @dataclass
    class _HC:
        cast_time_s: float
        hits: int
        cut_short: bool = False

    HYPERCHARGE = 17209
    # enabler net for a full Hypercharge = 650 → per Blazing Shot = 130.
    enabler = {HYPERCHARGE: 650.0}
    state = {"windows": [
        _HC(100.0, 5),               # full → no card
        _HC(220.0, 3),               # short 2 → 2×130 = 260p
        _HC(330.0, 2, cut_short=True),  # short 3 but window cut → skipped
    ]}
    out = improvements_from_hypercharge_windows(state, enabler)
    _check("only the actionable underfill surfaces (220s)",
           len(out) == 1, f"got {len(out)}")
    if out:
        im = out[0]
        _check("priced (5-3)×130 = 260", abs(im.lost_potency - 260.0) < 0.01,
               f"got {im.lost_potency}")
        _check("located at the window cast time (220s)",
               abs(im.time_s - 220.0) < 0.01, f"got {im.time_s}")
        _check("kind is hypercharge", im.kind == "hypercharge", im.kind)
        _check("summary names the shot count",
               "3/5" in im.summary, f"got {im.summary!r}")

    # No enabler value (sim-less) → nothing priced.
    _check("no sim value → no cards",
           improvements_from_hypercharge_windows(state, None) == [],
           "expected empty")


def test_reconcile_caps_at_budget_and_adds_residual() -> None:
    print("\nTest: reconcile keeps the ranked prefix, residual fills to the gap")
    items = [
        Improvement("missed_cast", DRILL, "Drill", 40.0, 340.0, "a"),
        Improvement("overcap", 0, "", 80.0, 120.0, "b"),
        Improvement("clip", 0, "", 0.0, 90.0, "c"),
    ]
    # Budget 500: keep 340 (fits), next 120 fits (460), next 90 would overflow
    # (550>500) -> stop; residual = 500-460 = 40 (< floor 60) -> no residual.
    out = reconcile_to_budget(items, 500.0)
    total = sum(i.lost_potency for i in out)
    _check("never exceeds the budget", total <= 500.0 + 1e-6, f"got {total}")
    _check("kept the two biggest, dropped the tail", len(out) == 2, f"got {len(out)}")

    # Budget 1000: all 550 fit; residual 450 (>= floor) appended.
    out2 = reconcile_to_budget(items, 1000.0)
    total2 = sum(i.lost_potency for i in out2)
    _check("sums exactly to the budget", abs(total2 - 1000.0) < 1e-6, f"got {total2}")
    _check("residual line present",
           any(i.kind == "residual" for i in out2), [i.kind for i in out2])

    # Clean run: nothing recoverable -> drop the noise entirely.
    _check("empty when budget <= 0", reconcile_to_budget(items, 0.0) == [])


def test_reconcile_residual_carries_folded_children() -> None:
    print("\nTest: reconcile folds the dropped tail into the residual's children")
    items = [
        Improvement("missed_cast", DRILL, "Drill", 40.0, 600.0, "big"),
        Improvement("overcap", 0, "", 80.0, 500.0, "mid"),
        Improvement("clip", 0, "", 30.0, 400.0, "tail-1"),
        Improvement("idle", 0, "", 50.0, 300.0, "tail-2"),
    ]
    # Budget 800: keep 600 (fits), next 500 overflows (1100>800) -> stop.
    # residual = 800-600 = 200 (>= floor 60) -> residual with 3 folded children.
    out = reconcile_to_budget(items, 800.0)
    total = sum(i.lost_potency for i in out)
    _check("panel still sums to the budget", abs(total - 800.0) < 1e-6, f"got {total}")
    residual = next((i for i in out if i.kind == "residual"), None)
    _check("residual present", residual is not None)
    if residual:
        _check("residual carries the 3 folded items as children",
               len(residual.children) == 3, f"got {len(residual.children)}")
        _check("children keep their individual summaries",
               {c.summary for c in residual.children} == {"mid", "tail-1", "tail-2"},
               f"got {[c.summary for c in residual.children]}")


def test_reconcile_residual_absorbs_extra_children() -> None:
    print("\nTest: reconcile attaches sub-floor extra_children to the residual")
    # One big priced item well under budget -> a large pure remainder. The
    # sub-card-floor 'minor' items have nothing to fold, so they ride in via
    # extra_children and make the otherwise-opaque residual expandable.
    priced = [Improvement("missed_cast", DRILL, "Drill", 40.0, 340.0, "big")]
    minor = [
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 12.0, 90.0, "m1"),
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 88.0, 70.0, "m2"),
    ]
    out = reconcile_to_budget(priced, 2000.0, extra_children=minor)
    residual = next((i for i in out if i.kind == "residual"), None)
    _check("residual present (big remainder)", residual is not None)
    if residual:
        _check("residual still bounded by the gap (2000-340=1660)",
               abs(residual.lost_potency - 1660.0) < 1e-6,
               f"got {residual.lost_potency}")
        _check("extra_children rode into the residual breakdown",
               len(residual.children) == 2, f"got {len(residual.children)}")
        _check("children sorted by potency desc",
               [c.summary for c in residual.children] == ["m1", "m2"],
               f"got {[c.summary for c in residual.children]}")


def test_group_attaches_constituents_as_children() -> None:
    print("\nTest: group_improvements collapses small same-ability items + keeps children")
    items = [
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 10.0, 180.0, "a"),
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 30.0, 180.0, "b"),
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 50.0, 180.0, "c"),
    ]
    out = group_improvements(items, nit_threshold=300.0)
    _check("collapsed into one aggregate card", len(out) == 1, f"got {len(out)}")
    agg = out[0]
    _check("aggregate sums the constituents (540p)",
           abs(agg.lost_potency - 540.0) < 1e-6, f"got {agg.lost_potency}")
    _check("aggregate carries the 3 constituents as children",
           len(agg.children) == 3, f"got {len(agg.children)}")
    _check("summary reads ×3", "×3" in agg.summary, f"got {agg.summary!r}")


def test_clipping_splits_idle_and_clip() -> None:
    print("\nTest: improvements_from_clipping emits separate idle + clip cards")
    f = ClippingFinding(
        effective_gcd_s=2.5, avg_gcd_potency=400.0,
        total_idle_s=3.5, idle_lost_gcds=1.4, idle_lost_potency=560.0,
        worst_idle=[(45.0, 1.9), (71.0, 1.1)],
        total_clip_s=0.9, clip_lost_gcds=0.36, clip_lost_potency=144.0,
        worst_clips=[(62.0, 0.55, 3), (41.0, 0.35, 3)],
    )
    out = improvements_from_clipping({"clipping": f})
    kinds = {i.kind for i in out}
    _check("emits both an idle and a clip card", kinds == {"idle", "clip"}, kinds)
    idle = next(i for i in out if i.kind == "idle")
    clip = next(i for i in out if i.kind == "clip")
    _check("idle priced at its lost potency (560)",
           abs(idle.lost_potency - 560.0) < 1e-6, f"got {idle.lost_potency}")
    _check("idle card carries per-stretch children",
           len(idle.children) == 2, f"got {len(idle.children)}")
    _check("clip summary mentions over-weaving",
           "over-weaving" in clip.summary, f"got {clip.summary!r}")
    _check("clip child mentions the weave count",
           bool(clip.children) and "3 oGCDs" in clip.children[0].summary,
           f"got {clip.children[:1]}")

    # Below the floors → no cards (a clean run shouldn't nag).
    quiet = improvements_from_clipping({"clipping": ClippingFinding(
        effective_gcd_s=2.5, avg_gcd_potency=400.0,
        total_idle_s=0.2, idle_lost_potency=10.0,
        total_clip_s=0.1, clip_lost_potency=5.0)})
    _check("near-clean pacing emits nothing", quiet == [], f"got {quiet}")


def test_opener_is_zero_priced_note() -> None:
    print("\nTest: opener deviation → zero-priced ordering note")
    state = {"findings": [
        OpenerFinding(position=1, expected_id=16500, actual_id=7411,
                      summary="slot 1 off", lost_potency=460.0),
        # same-potency reorder: not even worth a note
        OpenerFinding(position=2, expected_id=7411, actual_id=7411,
                      summary="ok", lost_potency=0.0),
    ]}
    out = diagnostics_from_opener(state)
    _check("only the real deviation surfaces", len(out) == 1, f"got {len(out)}")
    if out:
        _check("zero-priced", out[0].lost_potency == 0.0, f"got {out[0].lost_potency}")
        _check("kind is opener", out[0].kind == "opener", out[0].kind)


def test_buff_window_scales_value() -> None:
    print("\nTest: missed cast inside a buff window is worth more")
    ideal = [(0.0, DRILL), (20.0, DRILL)]
    actual = [(0.0, DRILL)]                       # missed the one at 20
    plain = compute_missed_cast_improvements(actual, ideal, MCH, None, 100.0)
    buffed = compute_missed_cast_improvements(
        actual, ideal, MCH, [(18.0, 26.0, 1.10)], 100.0)
    _check("plain ~340", abs(plain[0].lost_potency - 340.0) < 0.01)
    # 660*1.10 - 320 = 726 - 320 = 406
    _check("buffed ~406 (660*1.10-320)",
           abs(buffed[0].lost_potency - 406.0) < 0.01, f"got {buffed[0].lost_potency}")


def test_filler_quality_prices_dualcast_shortfall() -> None:
    print("\nTest: filler-quality card prices the 440-vs-filler shortfall (RDM)")
    # Ideal runs 4 Verthunder III + 4 Veraero III; player runs 2 VT3 + 4 VA3
    # (2 fewer 440s) with Jolt III backfilling. RDM filler_gcd_potency=380, so a
    # 440 is worth 60p above filler → 2 × 60 = 120p.
    ideal = ([(float(i), VERTHUNDER_III) for i in range(4)]
             + [(10.0 + i, VERAERO_III) for i in range(4)])
    actual = ([(0.0, VERTHUNDER_III), (1.0, VERTHUNDER_III)]
              + [(10.0 + i, VERAERO_III) for i in range(4)]
              + [(20.0, JOLT_III), (21.0, JOLT_III)])
    out = improvements_from_gcd_quality(actual, ideal, RDM, 300.0)
    _check("one aggregate card", len(out) == 1, f"got {len(out)}")
    if out:
        card = out[0]
        _check("kind is filler", card.kind == "filler", card.kind)
        _check("aggregate at t=0 (non-clickable)", card.time_s == 0.0, f"got {card.time_s}")
        _check("prices 2 fewer 440 × (440−380) = 120p",
               abs(card.lost_potency - 120.0) < 1e-6, f"got {card.lost_potency}")
        _check("breaks down per ability (children)",
               len(card.children) >= 1, f"got {len(card.children)}")
        _check("children sum to the card total",
               abs(sum(c.lost_potency for c in card.children) - card.lost_potency) < 1e-6,
               f"got {[c.lost_potency for c in card.children]}")

    # No shortfall (player matched/exceeded the ideal) → no card.
    _check("no card when counts match",
           improvements_from_gcd_quality(ideal, ideal, RDM, 300.0) == [],
           "expected empty")


def test_filler_quality_noop_without_optin() -> None:
    print("\nTest: jobs without filler_quality_gcds emit no card (MCH byte-identical)")
    HEATED = 7411  # a GCD MCH casts; MCH declares no filler_quality_gcds
    ideal = [(float(i), HEATED) for i in range(6)]
    actual = [(0.0, HEATED)]
    _check("MCH (empty filler_quality_gcds) → no filler card",
           improvements_from_gcd_quality(actual, ideal, MCH, 100.0) == [],
           "expected empty")


def test_below_floor_dropped() -> None:
    print("\nTest: sub-threshold suggestions are dropped")
    ideal = [(0.0, DRILL), (20.0, DRILL)]
    actual = [(0.0, DRILL)]
    # marginal 340; floor 400 -> dropped
    out = compute_missed_cast_improvements(
        actual, ideal, MCH, None, 100.0, min_potency=400.0)
    _check("dropped below floor", out == [], f"got {out}")


def main() -> int:
    test_unmatched_greedy()
    test_no_miss_when_counts_match()
    test_missed_gcd_tool_marginal()
    test_missed_damaging_ogcd_full_value()
    test_missed_enabler_priced_from_sim_value()
    test_wildfire_window_underfill_priced()
    test_hypercharge_underfill_priced_from_sim_value()
    test_reconcile_caps_at_budget_and_adds_residual()
    test_reconcile_residual_carries_folded_children()
    test_reconcile_residual_absorbs_extra_children()
    test_group_attaches_constituents_as_children()
    test_clipping_splits_idle_and_clip()
    test_filler_quality_prices_dualcast_shortfall()
    test_filler_quality_noop_without_optin()
    test_opener_is_zero_priced_note()
    test_buff_window_scales_value()
    test_below_floor_dropped()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
