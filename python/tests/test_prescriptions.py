"""Unit tests for the per-kind prescription strings on Improvements
(jobs/_core/improvements.py::Improvement.prescription).

Every producer that has a real rule templates a short imperative advice line
from numbers it already holds; kinds without a rule leave the field None (the
UI renders nothing — never filler copy).

Run from python/:  python tests/test_prescriptions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass, field

from jobs._core.improvements import (
    Improvement,
    compute_missed_cast_improvements,
    diagnostics_from_opener,
    group_families,
    group_improvements,
    improvements_from_cadence,
    improvements_from_clipping,
    improvements_from_deaths,
    improvements_from_hypercharge_windows,
    improvements_from_overcap,
    improvements_from_tincture,
    improvements_from_wildfire_windows,
    reconcile_to_budget,
)
from jobs._aspects.clipping import ClippingFinding
from jobs._aspects.opener import OpenerFinding
from jobs.machinist.data import JOB_DATA as MCH

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


DRILL = 16498         # GCD tool, 660p
WILDFIRE = 2878       # enabler oGCD, no direct potency
DOUBLE_CHECK = 36979  # damaging oGCD, full value when missed


def test_missed_gcd_vs_ogcd_wording() -> None:
    print("\nTest: missed-cast prescriptions distinguish GCD vs oGCD")
    ideal = [(0.0, DRILL), (20.0, DRILL)]
    actual = [(0.0, DRILL)]
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 100.0)
    _check("GCD tool says 'Fit one more' + filler basis",
           out[0].prescription is not None
           and out[0].prescription.startswith("Fit one more Drill")
           and "filler" in out[0].prescription,
           f"got {out[0].prescription!r}")

    ideal = [(0.0, DOUBLE_CHECK), (20.0, DOUBLE_CHECK)]
    actual = [(0.0, DOUBLE_CHECK)]
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 100.0)
    _check("damaging oGCD says 'Weave one more' + free weave",
           out[0].prescription is not None
           and out[0].prescription.startswith("Weave one more")
           and "Free weave" in out[0].prescription,
           f"got {out[0].prescription!r}")


def test_missed_enabler_prescription() -> None:
    print("\nTest: enabler miss (no sim value) advises keeping it on cooldown")
    ideal = [(0.0, WILDFIRE), (120.0, WILDFIRE)]
    actual = [(0.0, WILDFIRE)]
    out = compute_missed_cast_improvements(actual, ideal, MCH, None, 200.0)
    _check("one enabler note", len(out) == 1 and out[0].kind == "missed_enabler",
           f"got {out}")
    _check("prescription = keep on cooldown",
           out[0].prescription == "Keep Wildfire on cooldown; one more fits "
                                  "here.",
           f"got {out[0].prescription!r}")


def test_wildfire_lead_scales_with_short() -> None:
    print("\nTest: Wildfire prescription lead = short × 1.5s Blazing cadence")

    @dataclass
    class _W:
        cast_time_s: float
        hits: int

    out = improvements_from_wildfire_windows(
        {"windows": [_W(100.0, 5), _W(330.0, 3)]})
    by_t = {round(i.time_s): i for i in out}
    _check("short 1 → ~1.5s earlier, names the sixth weaponskill",
           by_t[100].prescription is not None
           and "~1.5s earlier" in by_t[100].prescription
           and "sixth weaponskill" in by_t[100].prescription,
           f"got {by_t[100].prescription!r}")
    _check("short 3 → ~4.5s earlier, names the fourth weaponskill",
           by_t[330].prescription is not None
           and "~4.5s earlier" in by_t[330].prescription
           and "fourth weaponskill" in by_t[330].prescription,
           f"got {by_t[330].prescription!r}")


def test_hypercharge_last_shot_variants() -> None:
    print("\nTest: Hypercharge prescription cites the last shot when known")

    @dataclass
    class _HC:
        cast_time_s: float
        hits: int
        cut_short: bool = False
        last_shot_s: float = 0.0

    enabler = {17209: 650.0}
    out = improvements_from_hypercharge_windows(
        {"windows": [_HC(220.0, 3, last_shot_s=224.5)]}, enabler)
    _check("with last_shot_s → names the time",
           out[0].prescription is not None
           and "stopped 2 short after 3:44" in out[0].prescription,
           f"got {out[0].prescription!r}")

    out = improvements_from_hypercharge_windows(
        {"windows": [_HC(220.0, 4)]}, enabler)
    _check("without last_shot_s → no dangling time",
           out[0].prescription is not None
           and out[0].prescription.endswith("stopped 1 short."),
           f"got {out[0].prescription!r}")


def test_idle_clip_prescriptions_cite_worst() -> None:
    print("\nTest: idle/clip aggregates prescribe their worst stretch")
    f = ClippingFinding(
        effective_gcd_s=2.5, avg_gcd_potency=400.0,
        total_idle_s=3.5, idle_lost_gcds=1.4, idle_lost_potency=560.0,
        worst_idle=[(45.0, 1.9), (71.0, 1.1)],
        total_clip_s=0.9, clip_lost_gcds=0.36, clip_lost_potency=144.0,
        worst_clips=[(62.0, 0.55, 3), (41.0, 0.35, 3)],
    )
    out = improvements_from_clipping({"clipping": f})
    idle = next(i for i in out if i.kind == "idle")
    clip = next(i for i in out if i.kind == "clip")
    _check("idle cites the 1.9s gap at 0:45; the title holds the total",
           idle.prescription is not None
           and "1.9s gap at 0:45" in idle.prescription
           and "3.5s" in idle.summary,
           f"got {idle.prescription!r} / {idle.summary!r}")
    _check("clip cites the 3-oGCD weave at 1:02",
           clip.prescription is not None
           and "Trim a weave at 1:02" in clip.prescription
           and "3 oGCDs" in clip.prescription,
           f"got {clip.prescription!r}")


def test_overcap_prescription() -> None:
    print("\nTest: overcap prescribes spending the gauge before the waste")

    @dataclass
    class _O:
        ability_id: int
        ability_name: str
        time_s: float
        lost_potency: float
        gauge: str
        wasted: str

    out = improvements_from_overcap({"findings": [
        _O(7411, "Heat Blast", 80.0, 120.0, "heat", "10 heat")]})
    _check("prescribes spend-before",
           out[0].prescription == "Spend heat before 1:20. Heat Blast "
                                  "wasted 10 heat.",
           f"got {out[0].prescription!r}")


def test_tincture_variants() -> None:
    print("\nTest: tincture prescriptions — missing pot vs misplaced pot")
    missing = improvements_from_tincture({
        "tincture_loss": 540.0, "tincture_optimal_count": 2,
        "observed_tincture_windows": [(10.0, 40.0)],
        "tincture_loss_time_s": 242.0})
    _check("under-potted → pot again on cooldown",
           missing[0].prescription == "Pot again the moment it's back up. "
                                      "1 more fit on cooldown.",
           f"got {missing[0].prescription!r}")
    misplaced = improvements_from_tincture({
        "tincture_loss": 300.0, "tincture_optimal_count": 1,
        "observed_tincture_windows": [(10.0, 40.0)],
        "tincture_loss_time_s": 122.0})
    _check("misplaced → shift into the burst at its time",
           misplaced[0].prescription == "Shift the pot into your burst at 2:02.",
           f"got {misplaced[0].prescription!r}")


def test_death_prescription() -> None:
    print("\nTest: death card prescribes surviving the hit")
    out = improvements_from_deaths([(100.0, 115.0)], [(105.0, DRILL)], MCH)
    _check("survive-this-hit wording; the title carries duration + time",
           out[0].prescription == "Survive this hit."
           and "Died at 1:40: 15s recovering" in out[0].summary,
           f"got {out[0].prescription!r} / {out[0].summary!r}")


def test_cadence_prescription() -> None:
    print("\nTest: cadence card prescribes pressing on cooldown with the count")
    ideal = [(float(i * 3), DRILL) for i in range(6)]
    player = [(float(i * 3), DRILL) for i in range(4)]
    out = improvements_from_cadence(player, ideal, MCH, {"clipping": None})
    _check("one cadence card", len(out) == 1 and out[0].kind == "cadence",
           f"got {out}")
    _check("title cites ~2 GCDs; prescription is press-on-ready",
           "~2 GCDs" in out[0].summary
           and out[0].prescription == "Press the next GCD the moment "
                                      "it's ready.",
           f"got {out[0].prescription!r} / {out[0].summary!r}")


def test_grouped_missed_cast_prescription() -> None:
    print("\nTest: grouped ×N missed-cast parent prescribes fitting N more")
    items = [
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 60.0, 180.0,
                    "Missed Double Check — fit one around 1:00",
                    prescription="Weave one more Double Check around 1:00. "
                                 "Free weave, ~180p."),
        Improvement("missed_cast", DOUBLE_CHECK, "Double Check", 90.0, 180.0,
                    "Missed Double Check — fit one around 1:30",
                    prescription="Weave one more Double Check around 1:30. "
                                 "Free weave, ~180p."),
    ]
    out = group_improvements(items)
    _check("one grouped card", len(out) == 1 and len(out[0].children) == 2,
           f"got {out}")
    _check("parent prescribes 'Fit 2 more Double Check'",
           out[0].prescription is not None
           and out[0].prescription.startswith("Fit 2 more Double Check")
           and "~180p" in out[0].prescription,
           f"got {out[0].prescription!r}")
    _check("children keep their own prescriptions",
           all(c.prescription for c in out[0].children),
           f"got {[c.prescription for c in out[0].children]}")


def test_residual_prescription_and_child_preservation() -> None:
    print("\nTest: residual card carries the diffuse copy; folded kids keep theirs")
    cards = [
        Improvement("missed_cast", DRILL, "Drill", 40.0, 340.0, "Missed Drill",
                    prescription="Fit one more Drill around 0:40 — worth "
                                 "~340p over the filler that backfills it."),
        Improvement("missed_cast", DRILL, "Drill", 80.0, 340.0, "Missed Drill",
                    prescription="Fit one more Drill around 1:20 — worth "
                                 "~340p over the filler that backfills it."),
    ]
    out = reconcile_to_budget(cards, 500.0)
    residual = next(i for i in out if i.kind == "residual")
    _check("residual prescription is the diffuse copy",
           residual.prescription is not None
           and residual.prescription.startswith("No single cast is at fault"),
           f"got {residual.prescription!r}")
    _check("folded child kept its prescription",
           bool(residual.children)
           and residual.children[0].prescription is not None,
           f"got {[c.prescription for c in residual.children]}")


def test_pacing_umbrella_prescription() -> None:
    print("\nTest: pacing umbrella leads with its biggest member's advice")
    cards = [
        Improvement("idle", 0, "", 45.0, 560.0, "Time spent idle: 3.5s",
                    prescription="Close the 1.9s gap at 0:45 — your longest; "
                                 "3.5s idle total (~1 GCDs)."),
        Improvement("cadence", 0, "", 0.0, 200.0, "Loose GCD pacing",
                    prescription="Press the next GCD the moment it's ready — "
                                 "the optimal line fits ~1 more GCDs with no "
                                 "single gap to point at."),
    ]
    out = group_families(cards)
    _check("one umbrella", len(out) == 1 and out[0].kind == "pacing", f"got {out}")
    _check("umbrella prefixes the biggest member's prescription",
           out[0].prescription is not None
           and out[0].prescription.startswith("Start with the biggest piece: "
                                              "close the 1.9s gap"),
           f"got {out[0].prescription!r}")


def test_no_rule_kinds_stay_none() -> None:
    print("\nTest: kinds without a rule leave prescription None")
    opener = diagnostics_from_opener({"findings": [
        OpenerFinding(position=1, expected_id=16500, actual_id=7411,
                      summary="slot 1 off", lost_potency=460.0)]})
    _check("opener note has no prescription",
           opener[0].prescription is None, f"got {opener[0].prescription!r}")
    _check("bare Improvement defaults to None",
           Improvement("align", 0, "", 10.0, 100.0, "x").prescription is None)


def main() -> int:
    test_missed_gcd_vs_ogcd_wording()
    test_missed_enabler_prescription()
    test_wildfire_lead_scales_with_short()
    test_hypercharge_last_shot_variants()
    test_idle_clip_prescriptions_cite_worst()
    test_overcap_prescription()
    test_tincture_variants()
    test_death_prescription()
    test_cadence_prescription()
    test_grouped_missed_cast_prescription()
    test_residual_prescription_and_child_preservation()
    test_pacing_umbrella_prescription()
    test_no_rule_kinds_stay_none()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
