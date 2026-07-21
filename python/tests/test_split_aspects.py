"""Unit tests for the job-agnostic split aspects in jobs/_aspects/.

These tests exercise each aspect directly (not through ExecutionAspect)
so that the underlying math is locked in before ExecutionAspect is
retired. Two layers per aspect:

  1. Pure-function tests with synthetic norm_casts inputs — fast,
     deterministic, exercises edge cases without needing a fixture.
  2. Fixture-driven smoke tests through .analyze() with the same
     MockClient pattern test_execution.py uses — proves the aspect
     runs cleanly against real-shaped cast streams and emits the
     state keys the contract relies on.

Run from python/:  python tests/test_split_aspects.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._aspects.abilities import AbilityTimelineAspect
from jobs._aspects.alignment import (
    AlignmentAspect,
    compute_burst_alignment,
)
from jobs._aspects.clipping import ClippingAspect, compute_clipping
from jobs._aspects.drift import DriftAspect, compute_drift_for_ability
from jobs._aspects.opener import OpenerAspect, compute_opener
from jobs._aspects.overcap import (
    OvercapAspect,
    compute_overcap,
    compute_overcap_for_gauge,
)
from jobs._core.job import CDRRule, GaugeModel, JobData
from jobs.machinist.data import JOB_DATA as MCH_DATA


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- MockClient -------------------------------------------------------------

class MockClient:
    """Returns canned cast events. Same shape as the one in
    test_execution.py — duplicated here so this file stays standalone."""

    def __init__(self, cast_events: list[dict]):
        self._events = cast_events

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        return [
            e for e in self._events
            if start <= e.get("timestamp", 0) <= end
        ]


def _fight(fixture: dict) -> dict:
    return {
        "startTime": fixture["fight_start_ms"],
        "endTime": fixture["fight_end_ms"],
        "id": fixture["fight_id"],
    }


def _actor(fixture: dict) -> dict:
    return {"id": fixture["source_id"], "name": fixture["label"]}


def _load_fixtures() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES_DIR.glob("*.json"))
    }


# --- Test harness -----------------------------------------------------------

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


# --- Pure-function tests ----------------------------------------------------

def test_compute_drift_pure_no_drift() -> None:
    """A perfect cast-on-cooldown stream produces zero capped seconds."""
    print()
    print("Test: compute_drift_for_ability — perfect on-CD cadence")
    # Air Anchor: 40s recast, 1 charge. Cast every 40s on the dot.
    norm_casts = [(0.0, 16500), (40.0, 16500), (80.0, 16500), (120.0, 16500)]
    finding = compute_drift_for_ability(
        ability_id=16500,
        data=MCH_DATA,
        norm_casts=norm_casts,
        fight_duration_s=130.0,
        downtime_windows=[],
    )
    _check("no drift on perfect cadence",
           finding.capped_seconds < 1.0,
           f"got capped_seconds={finding.capped_seconds}")
    _check("casts counted correctly", finding.casts == 4,
           f"got {finding.casts}")
    _check("lost_potency near zero", finding.lost_potency < 100,
           f"got {finding.lost_potency}")


def test_compute_drift_pure_capped() -> None:
    """One cast then nothing for 200s → ~160s of cap waiting after recast
    finishes."""
    print()
    print("Test: compute_drift_for_ability — long cap interval")
    # Air Anchor: 40s recast. Cast at t=0, then never again. Fight duration 200s.
    # After first cast: 40s rebuild + 160s sitting capped = 160s capped.
    norm_casts = [(0.0, 16500)]
    finding = compute_drift_for_ability(
        ability_id=16500,
        data=MCH_DATA,
        norm_casts=norm_casts,
        fight_duration_s=200.0,
        downtime_windows=[],
    )
    _check("capped seconds within tolerance of 160s",
           150.0 <= finding.capped_seconds <= 165.0,
           f"got {finding.capped_seconds}")
    _check("lost_potency > 1000 (4 missed casts × 660p)",
           finding.lost_potency > 1000,
           f"got {finding.lost_potency}")


def test_compute_drift_pure_downtime_excluded() -> None:
    """Downtime windows must not count toward capped time."""
    print()
    print("Test: compute_drift_for_ability — downtime excluded")
    # Air Anchor at t=0, no further casts for 200s, downtime covers t=40..200
    # (everything after the first cast except a tiny rebuild slice).
    norm_casts = [(0.0, 16500)]
    finding_no_dt = compute_drift_for_ability(
        ability_id=16500, data=MCH_DATA, norm_casts=norm_casts,
        fight_duration_s=200.0, downtime_windows=[],
    )
    finding_with_dt = compute_drift_for_ability(
        ability_id=16500, data=MCH_DATA, norm_casts=norm_casts,
        fight_duration_s=200.0, downtime_windows=[(40.0, 200.0)],
    )
    _check("downtime cuts capped seconds to near zero",
           finding_with_dt.capped_seconds < 5.0,
           f"with dt: {finding_with_dt.capped_seconds}")
    _check("downtime case strictly < no-downtime case",
           finding_with_dt.capped_seconds < finding_no_dt.capped_seconds,
           f"with={finding_with_dt.capped_seconds} "
           f"without={finding_no_dt.capped_seconds}")


def test_compute_drift_pure_dead_window_excluded() -> None:
    """Death windows, like downtime, must not count toward capped time —
    a cooldown sitting idle while the player is dead isn't drift."""
    print()
    print("Test: compute_drift_for_ability — dead window excluded")
    norm_casts = [(0.0, 16500)]   # Air Anchor at t=0, none after
    base = compute_drift_for_ability(
        ability_id=16500, data=MCH_DATA, norm_casts=norm_casts,
        fight_duration_s=200.0, downtime_windows=[],
    )
    dead = compute_drift_for_ability(
        ability_id=16500, data=MCH_DATA, norm_casts=norm_casts,
        fight_duration_s=200.0, downtime_windows=[],
        dead_windows=[(40.0, 200.0)],
    )
    _check("dead window cuts capped seconds to near zero",
           dead.capped_seconds < 5.0, f"got {dead.capped_seconds}")
    _check("dead case strictly < no-dead case",
           dead.capped_seconds < base.capped_seconds,
           f"dead={dead.capped_seconds} base={base.capped_seconds}")


def test_compute_drift_pure_charge_sharing() -> None:
    """Bioblaster consumes a Drill charge — drift on Drill must count it."""
    print()
    print("Test: compute_drift_for_ability — Drill/Bioblaster shared charges")
    # Bioblaster is 16499 (MCH_DATA.charge_sharing maps it to Drill 16498).
    # Two-charge ability; consume both via Drill+Bioblaster, then sit at 0.
    norm_casts = [
        (0.0, 16498),   # Drill (charge 1)
        (1.0, 16499),   # Bioblaster (charge 2 — shared)
    ]
    finding = compute_drift_for_ability(
        ability_id=16498,
        data=MCH_DATA,
        norm_casts=norm_casts,
        fight_duration_s=10.0,
        downtime_windows=[],
    )
    _check("Drill casts counted (excludes Bioblaster)",
           finding.casts == 1, f"got {finding.casts}")
    _check("shared consumers counted",
           finding.shared_consumers == 1,
           f"got {finding.shared_consumers}")


def test_compute_drift_pure_cdr_rule() -> None:
    """Blazing Shot reduces DC/CM cooldown by 15s. Verify the CDR rule
    actually trims drift on the DC drift detector."""
    print()
    print("Test: compute_drift_for_ability — Blazing Shot CDR on DC")
    # DC (36979): 30s recast, 3 charges. Cast once at t=0, then sit at 0
    # for 100s — should accumulate drift WITHOUT any CDR triggers.
    norm_casts_no_cdr = [(0.0, 36979)]
    finding_no_cdr = compute_drift_for_ability(
        ability_id=36979, data=MCH_DATA,
        norm_casts=norm_casts_no_cdr,
        fight_duration_s=120.0, downtime_windows=[],
    )
    # Same scenario, but Blazing Shots in between push the charge clock
    # forward — fewer cap seconds.
    norm_casts_with_cdr = [
        (0.0, 36979),
        (10.0, 36978),   # Blazing Shot — -15s on DC recast
        (12.0, 36978),
        (14.0, 36978),
    ]
    finding_with_cdr = compute_drift_for_ability(
        ability_id=36979, data=MCH_DATA,
        norm_casts=norm_casts_with_cdr,
        fight_duration_s=120.0, downtime_windows=[],
    )
    _check(
        "CDR triggers either reduce drift or accumulate cdr_overflow",
        (finding_with_cdr.capped_seconds <= finding_no_cdr.capped_seconds + 0.01)
        or (finding_with_cdr.cdr_overflow_seconds > 0),
        f"no_cdr={finding_no_cdr.capped_seconds} "
        f"with_cdr={finding_with_cdr.capped_seconds} "
        f"overflow={finding_with_cdr.cdr_overflow_seconds}",
    )


def test_compute_clipping_pure_clean_pacing() -> None:
    """A clean 2.5s-cadence stream produces near-zero idle and clip."""
    print()
    print("Test: compute_clipping — clean 2.5s cadence")
    # 20 casts of Heated Split Shot (a GCD with 220p, in MCH_DATA.potencies).
    norm_casts = [(i * 2.5, 7411) for i in range(20)]
    finding = compute_clipping(
        norm_casts=norm_casts,
        fight_duration_s=60.0,
        downtime_windows=[],
        skip_intervals=[],
        data=MCH_DATA,
    )
    _check("effective_gcd_s in [2.0, 2.6]",
           2.0 <= finding.effective_gcd_s <= 2.6,
           f"got {finding.effective_gcd_s}")
    _check("near-zero total idle",
           finding.total_idle_s < 0.5,
           f"got {finding.total_idle_s}")
    _check("near-zero total clip",
           finding.total_clip_s < 0.5,
           f"got {finding.total_clip_s}")
    _check("near-zero idle lost_potency",
           finding.idle_lost_potency < 100,
           f"got {finding.idle_lost_potency}")


def test_compute_clipping_empty_gaps_are_idle_not_clip() -> None:
    """A 3.0s cadence with NO weaves is *idle*, not clipping — there's no oGCD
    animation lock to blame for the late GCD."""
    print()
    print("Test: compute_clipping — empty over-long gaps read as idle")
    # 20 GCDs at 3.0s, no oGCDs — eff GCD caps to 2.6, ~0.4s excess per pair.
    norm_casts = [(i * 3.0, 7411) for i in range(20)]
    finding = compute_clipping(
        norm_casts=norm_casts,
        fight_duration_s=60.0,
        downtime_windows=[],
        skip_intervals=[],
        data=MCH_DATA,
    )
    _check("total_idle_s >= 5s (cumulative across 19 pairs)",
           finding.total_idle_s >= 5.0,
           f"got {finding.total_idle_s}")
    _check("no clip without weaves to blame",
           finding.total_clip_s == 0.0,
           f"got {finding.total_clip_s}")
    _check("worst_idle list populated",
           len(finding.worst_idle) > 0,
           f"got {len(finding.worst_idle)}")
    _check("idle_lost_potency > 0",
           finding.idle_lost_potency > 0,
           f"got {finding.idle_lost_potency}")


def test_compute_clipping_true_weave_clip() -> None:
    """A late 3rd weave whose animation lock spills past GCD-ready clips the
    next GCD — that excess is *clip*, not idle."""
    print()
    print("Test: compute_clipping — over-weave pushes the GCD (true clip)")
    # GCDs at a 3.0s cadence (eff GCD caps to 2.6 → 0.4s excess/pair). In each
    # pair, weave three oGCDs with the LAST landing at +2.5s, so its 0.6s lock
    # ends at +3.1 — well past the +2.6 ready point → the excess is clipping.
    OGCDS = (36979, 36980, 2876)  # Double Check, Checkmate, Reassemble (bundled)
    norm_casts: list[tuple[float, int]] = []
    for i in range(20):
        t = i * 3.0
        norm_casts.append((t, 7411))                    # the GCD
        norm_casts.append((t + 0.7, OGCDS[0]))
        norm_casts.append((t + 1.4, OGCDS[1]))
        norm_casts.append((t + 2.5, OGCDS[2]))          # the late 3rd weave
    finding = compute_clipping(
        norm_casts=norm_casts, fight_duration_s=70.0,
        downtime_windows=[], skip_intervals=[], data=MCH_DATA,
    )
    _check("total_clip_s >= 5s (over-weave clipped every pair)",
           finding.total_clip_s >= 5.0, f"got {finding.total_clip_s}")
    _check("negligible idle (the excess is explained by weaving)",
           finding.total_idle_s < 1.0, f"got {finding.total_idle_s}")
    _check("clip_lost_potency > 0",
           finding.clip_lost_potency > 0, f"got {finding.clip_lost_potency}")
    _check("worst_clips records the weave count (3 oGCDs)",
           bool(finding.worst_clips) and finding.worst_clips[0][2] == 3,
           f"got {finding.worst_clips[:1]}")


def test_compute_clipping_excludes_dead_windows() -> None:
    """A long gap covered by a death window must not read as idle."""
    print()
    print("Test: compute_clipping — dead window excluded")
    # 10 clean 2.5s casts (0..22.5), a 30s death gap, then 10 more clean casts.
    norm_casts = [(i * 2.5, 7411) for i in range(10)]
    norm_casts += [(52.5 + i * 2.5, 7411) for i in range(10)]
    no_dead = compute_clipping(
        norm_casts=norm_casts, fight_duration_s=90.0,
        downtime_windows=[], skip_intervals=[], data=MCH_DATA,
    )
    with_dead = compute_clipping(
        norm_casts=norm_casts, fight_duration_s=90.0,
        downtime_windows=[], skip_intervals=[], data=MCH_DATA,
        dead_windows=[(24.0, 52.5)],   # died 1.5s after the last cast
    )
    _check("without the dead window the gap reads as big idle",
           no_dead.total_idle_s > 20.0, f"got {no_dead.total_idle_s}")
    _check("dead window removes essentially all of that idle",
           with_dead.total_idle_s < 1.0, f"got {with_dead.total_idle_s}")


def test_compute_clipping_pure_few_casts() -> None:
    """Fewer than 4 GCDs => no estimate, defaults returned."""
    print()
    print("Test: compute_clipping — too few GCDs to estimate")
    norm_casts = [(0.0, 7411), (2.5, 7412)]
    finding = compute_clipping(
        norm_casts=norm_casts,
        fight_duration_s=10.0,
        downtime_windows=[],
        skip_intervals=[],
        data=MCH_DATA,
    )
    _check("effective_gcd_s falls back to 2.5",
           finding.effective_gcd_s == 2.5,
           f"got {finding.effective_gcd_s}")
    _check("no idle or clip reported",
           finding.total_idle_s == 0.0 and finding.total_clip_s == 0.0
           and finding.idle_lost_potency == 0.0)


def test_compute_clipping_skip_intervals_honored() -> None:
    """Casts inside skip_intervals don't enter the pacing calculation."""
    print()
    print("Test: compute_clipping — skip_intervals exclude pairs")
    # 10 normal-pacing casts then 5 Blazing Shots at 1.5s inside a skip
    # window — without the skip, those pairs would skew the calculation.
    norm_casts = [(i * 2.5, 7411) for i in range(10)]
    norm_casts += [(25.0 + i * 1.5, 36978) for i in range(5)]
    finding_no_skip = compute_clipping(
        norm_casts=norm_casts, fight_duration_s=50.0,
        downtime_windows=[], skip_intervals=[], data=MCH_DATA,
    )
    finding_with_skip = compute_clipping(
        norm_casts=norm_casts, fight_duration_s=50.0,
        downtime_windows=[], skip_intervals=[(25.0, 33.0)], data=MCH_DATA,
    )
    _check(
        "skip-interval branch produces <= idle than the no-skip branch",
        finding_with_skip.total_idle_s <= finding_no_skip.total_idle_s + 1e-6,
        f"with_skip={finding_with_skip.total_idle_s} "
        f"no_skip={finding_no_skip.total_idle_s}",
    )


def test_compute_overcap_pure_heat() -> None:
    """Three Heated Split Shots in a row push heat from 0 -> 15 (no
    overcap). After two Wildfires-worth of heat building (20 casts),
    overcap should fire."""
    print()
    print("Test: compute_overcap_for_gauge — heat overcap detection")
    heat_gauge = next(g for g in MCH_DATA.gauges if g.name == "heat")
    # 21 Heated Split Shots × 5 heat each = 105, over the 100 cap by 5.
    norm_casts = [(i * 2.5, 7411) for i in range(21)]
    findings = compute_overcap_for_gauge(norm_casts, heat_gauge)
    _check("at least one heat overcap finding emitted",
           len(findings) >= 1,
           f"got {len(findings)}")
    if findings:
        _check("first finding's gauge is 'heat'", findings[0].gauge == "heat")
        _check("wasted > 0", findings[0].wasted > 0)


def test_compute_overcap_pure_no_overcap() -> None:
    """A spender resets the counter — three generators with a spender
    between them should NOT overcap."""
    print()
    print("Test: compute_overcap_for_gauge — spender prevents overcap")
    heat_gauge = next(g for g in MCH_DATA.gauges if g.name == "heat")
    # 11 generators (55 heat), then Hypercharge (-50), then 11 more (55 = 60 final).
    norm_casts = [(i * 2.5, 7411) for i in range(11)]
    norm_casts.append((30.0, 17209))   # Hypercharge — spends 50 heat
    norm_casts += [(35.0 + i * 2.5, 7411) for i in range(11)]
    findings = compute_overcap_for_gauge(norm_casts, heat_gauge)
    _check("no overcap when spender resets gauge",
           len(findings) == 0,
           f"got {len(findings)} findings")


def test_compute_overcap_pure_prepull_no_finding() -> None:
    """Pre-pull casts (t<0) feed the counter but never fire findings."""
    print()
    print("Test: compute_overcap_for_gauge — pre-pull silent")
    heat_gauge = next(g for g in MCH_DATA.gauges if g.name == "heat")
    # All pre-pull casts.
    norm_casts = [(-5.0 + i * 0.1, 7411) for i in range(25)]
    findings = compute_overcap_for_gauge(norm_casts, heat_gauge)
    _check("pre-pull generators don't fire overcap findings",
           len(findings) == 0,
           f"got {len(findings)}")


def test_compute_overcap_multi_gauge() -> None:
    """compute_overcap covers every gauge in JobData; MCH has heat + battery."""
    print()
    print("Test: compute_overcap — both gauges traversed for MCH")
    # 21 Heated Split Shots (heat overcap) + 6 Air Anchors (120 battery, cap 100).
    norm_casts = [(i * 2.5, 7411) for i in range(21)]
    norm_casts += [(60.0 + i * 5.0, 16500) for i in range(6)]
    findings = compute_overcap(norm_casts, MCH_DATA)
    gauges_hit = {f.gauge for f in findings}
    _check("both heat and battery overcap findings emitted",
           "heat" in gauges_hit and "battery" in gauges_hit,
           f"got {gauges_hit}")


def test_compute_opener_pure_match() -> None:
    """Casting the canonical opener produces zero findings."""
    print()
    print("Test: compute_opener — perfect opener produces empty list")
    norm_casts = [(i * 2.5, aid)
                  for i, aid in enumerate(MCH_DATA.canonical_opener)]
    findings = compute_opener(norm_casts, MCH_DATA)
    _check("no opener findings on canonical opener",
           findings == [], f"got {len(findings)} findings")


def test_compute_opener_pure_substitution() -> None:
    """Replacing a 660p tool with a 220p combo step yields lost_potency=440."""
    print()
    print("Test: compute_opener — Drill->Heated Split substitution")
    # Canonical position 3 is Drill (16498, 660p).
    # Substitute Heated Split (7411, 220p) at that slot.
    casts = list(MCH_DATA.canonical_opener)
    casts[2] = 7411
    norm_casts = [(i * 2.5, aid) for i, aid in enumerate(casts)]
    findings = compute_opener(norm_casts, MCH_DATA)
    _check("exactly one deviation flagged",
           len(findings) == 1, f"got {len(findings)}")
    if findings:
        _check("position is 3",
               findings[0].position == 3,
               f"got {findings[0].position}")
        _check("lost_potency = 660 - 220 = 440",
               findings[0].lost_potency == 440.0,
               f"got {findings[0].lost_potency}")


def test_compute_opener_pure_same_potency_swap() -> None:
    """Swapping two same-potency 660p tools costs 0p."""
    print()
    print("Test: compute_opener — same-potency reorder costs 0p")
    # Canonical: AA(16500), Split(7411), Drill(16498), ...
    # Swap AA and Drill — both 660p.
    casts = list(MCH_DATA.canonical_opener)
    casts[0], casts[2] = casts[2], casts[0]
    norm_casts = [(i * 2.5, aid) for i, aid in enumerate(casts)]
    findings = compute_opener(norm_casts, MCH_DATA)
    # The swap produces two flagged deviations but both with cost 0.
    total_cost = sum(f.lost_potency for f in findings)
    _check("total cost of 660p<->660p swap is 0",
           total_cost == 0.0,
           f"got {total_cost}")


def test_compute_burst_alignment_no_burst_abilities() -> None:
    """Job with no burst_abilities should produce an empty list."""
    print()
    print("Test: compute_burst_alignment - empty burst_abilities means no findings")
    empty_data = JobData(job_name="Empty", patch_version="7.x")
    norm_casts = [(i * 5.0, 16498) for i in range(20)]
    findings = compute_burst_alignment(norm_casts, fight_duration_s=120.0,
                                       data=empty_data)
    _check("empty list", findings == [], f"got {len(findings)}")


def test_compute_burst_alignment_flag_near_burst() -> None:
    """A high-potency tool cast just before a synthetic 2-min window
    should be flagged."""
    print()
    print("Test: compute_burst_alignment — flag pre-burst tool cast")
    # Drill at t=116 — 4s before the synthetic 2:00 burst window.
    norm_casts = [(116.0, 16498)]
    findings = compute_burst_alignment(norm_casts, fight_duration_s=180.0,
                                       data=MCH_DATA)
    _check("at least one finding emitted",
           len(findings) >= 1, f"got {len(findings)}")
    if findings:
        _check("finding flagged is burst_misalign",
               findings[0].kind == "burst_misalign",
               f"got {findings[0].kind}")


def test_compute_burst_alignment_no_flag_safely_offset() -> None:
    """A tool cast well before the window should not be flagged."""
    print()
    print("Test: compute_burst_alignment — no flag on safely-offset cast")
    # Drill at t=60 — 60s before burst, far outside the 8s shift window.
    norm_casts = [(60.0, 16498)]
    findings = compute_burst_alignment(norm_casts, fight_duration_s=180.0,
                                       data=MCH_DATA)
    _check("no findings", findings == [], f"got {len(findings)}")


# --- Composition-aware alignment -------------------------------------------

def test_compute_burst_alignment_no_providers_suppressed() -> None:
    """A known party with no raid-buff providers => no alignment advice
    (we never invent a buff that wasn't there)."""
    print()
    print("Test: compute_burst_alignment — no providers => suppressed")
    norm_casts = [(116.0, 16498)]   # would flag under the synthetic path
    findings = compute_burst_alignment(
        norm_casts, fight_duration_s=180.0, data=MCH_DATA,
        party_jobs=["Warrior", "WhiteMage", "Paladin", "Sage"],
    )
    _check("no findings (no providers)", findings == [], f"got {len(findings)}")


def test_compute_burst_alignment_benefit_scales_with_comp() -> None:
    """Benefit reflects the providers actually present: a richer comp
    yields a larger uplift than a single-provider comp."""
    print()
    print("Test: compute_burst_alignment — benefit scales with comp")
    norm_casts = [(116.0, 16498)]
    fewer = compute_burst_alignment(
        norm_casts, 180.0, MCH_DATA, party_jobs=["Astrologian", "Dancer"])
    many = compute_burst_alignment(
        norm_casts, 180.0, MCH_DATA,
        party_jobs=["Scholar", "Dragoon", "RedMage", "Astrologian"])
    _check("two-provider comp flags one", len(fewer) == 1, f"got {len(fewer)}")
    _check("multi-provider comp flags one", len(many) == 1, f"got {len(many)}")
    if fewer and many:
        _check("richer comp => larger benefit",
               many[0].lost_potency > fewer[0].lost_potency,
               f"fewer={fewer[0].lost_potency:.0f} many={many[0].lost_potency:.0f}")
        _check("summary names a real provider buff, not a comp suggestion",
               "Chain Stratagem" in many[0].summary
               and "bring" not in many[0].summary.lower(),
               f"got {many[0].summary!r}")


def test_compute_burst_alignment_weak_single_provider_below_floor() -> None:
    """A lone weak provider (Reaper / Arcane Circle, +3% ≈ 20p on a 660p
    Drill) falls below the noise floor and is not flagged — we don't nag
    about a shift that's barely worth any potency."""
    print()
    print("Test: compute_burst_alignment — weak single provider suppressed")
    findings = compute_burst_alignment(
        [(116.0, 16498)], 180.0, MCH_DATA, party_jobs=["Reaper"])
    _check("weak single provider below floor => no finding",
           findings == [], f"got {len(findings)}")


def test_compute_burst_alignment_unknown_comp_falls_back() -> None:
    """party_jobs=None (comp unknown) keeps the synthetic behavior so the
    detector still works outside the pipeline."""
    print()
    print("Test: compute_burst_alignment — None comp uses synthetic fallback")
    norm_casts = [(116.0, 16498)]
    findings = compute_burst_alignment(
        norm_casts, 180.0, MCH_DATA, party_jobs=None)
    _check("synthetic fallback still flags", len(findings) == 1,
           f"got {len(findings)}")
    if findings:
        _check("summary marked synthetic",
               "synthetic" in findings[0].summary,
               f"got {findings[0].summary!r}")


def test_raid_buff_registry_present_providers() -> None:
    print()
    print("Test: raid_buffs.present_providers / combined_multiplier")
    from jobs._core.raid_buffs import combined_multiplier, present_providers
    jobs = ["Machinist", "Scholar", "Warrior", "Dragoon", "WhiteMage"]
    provs = present_providers(jobs)
    names = {p.job for p in provs}
    _check("picks Scholar + Dragoon only", names == {"Scholar", "Dragoon"},
           f"got {names}")
    _check("combined multiplier > 1 with providers",
           combined_multiplier(jobs) > 1.0)
    _check("combined multiplier == 1 with no providers",
           combined_multiplier(["Warrior", "WhiteMage"]) == 1.0)


def test_alignment_aspect_comp_aware_end_to_end() -> None:
    """AlignmentAspect derives comp from the report: a provider comp yields
    findings; a provider-less comp yields none but still reports
    comp_known=True."""
    print()
    print("Test: AlignmentAspect — comp derived from report")
    # Synthetic 200s cast stream with a Drill 4s before the 2:00 window.
    casts = [{"timestamp": 1_000_000 + int(116 * 1000), "type": "cast",
              "sourceID": 1, "targetID": 9, "abilityGameID": 16498}]

    class _C:
        def get_events(self, code, start, end, sid, data_type="Casts",
                       ability_id=None):
            return [e for e in casts if start <= e["timestamp"] <= end]

    fight = {"startTime": 1_000_000, "endTime": 1_200_000,
             "friendlyPlayers": [1, 2, 3]}

    def _report(*party_jobs: str) -> dict:
        actors = [{"id": 1, "type": "Player", "subType": "Machinist"}]
        for i, job in enumerate(party_jobs, start=2):
            actors.append({"id": i, "type": "Player", "subType": job})
        return {"masterData": {"actors": actors}}

    aspect = AlignmentAspect(MCH_DATA)
    with_prov = aspect.analyze(_C(), "c", fight, {"id": 1},
                               _report("Astrologian", "Dancer")).state
    no_prov = aspect.analyze(_C(), "c", fight, {"id": 1},
                             _report("Warrior", "WhiteMage")).state
    _check("provider comp => comp_known + findings",
           with_prov["comp_known"] and len(with_prov["findings"]) == 1,
           f"got {with_prov}")
    _check("provider comp lists the real buffs present",
           with_prov["providers"] == ["Divination", "Technical Finish"],
           f"got {with_prov['providers']}")
    _check("provider-less comp => comp_known True, no findings",
           no_prov["comp_known"] and no_prov["findings"] == []
           and no_prov["providers"] == [],
           f"got {no_prov}")


# --- Fixture-driven smoke tests --------------------------------------------

def _make_client(fix: dict) -> MockClient:
    return MockClient(fix["cast_events"])


def test_drift_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    """DriftAspect.analyze runs cleanly on every fixture and emits a
    state with one finding per non-excluded cooldown."""
    print()
    print("Test: DriftAspect.analyze on every fixture")
    aspect = DriftAspect(MCH_DATA)
    expected_count = sum(
        1 for aid in MCH_DATA.cooldowns
        if aid not in MCH_DATA.drift_exclusions
    )
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        findings = result.state.get("findings") or []
        _check(f"{name}: {len(findings)} drift findings = expected {expected_count}",
               len(findings) == expected_count,
               f"got {len(findings)}")
        _check(f"{name}: all lost_potency >= 0",
               all(f.lost_potency >= 0 for f in findings))


def test_clipping_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    """ClippingAspect emits a ClippingFinding with sane bounds."""
    print()
    print("Test: ClippingAspect.analyze on every fixture")
    aspect = ClippingAspect(MCH_DATA)
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        finding = result.state.get("clipping")
        _check(f"{name}: clipping present", finding is not None)
        if finding is None:
            continue
        _check(f"{name}: eff_gcd in [2.0, 2.6] (got {finding.effective_gcd_s:.2f})",
               2.0 <= finding.effective_gcd_s <= 2.6)
        _check(f"{name}: total_idle_s >= 0", finding.total_idle_s >= 0)
        _check(f"{name}: total_clip_s >= 0", finding.total_clip_s >= 0)
        _check(f"{name}: idle/clip lost_potency >= 0",
               finding.idle_lost_potency >= 0 and finding.clip_lost_potency >= 0)


def test_overcap_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    print()
    print("Test: OvercapAspect.analyze on every fixture")
    aspect = OvercapAspect(MCH_DATA)
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        findings = result.state.get("findings") or []
        # Every finding must belong to a known gauge.
        known_gauges = {g.name for g in MCH_DATA.gauges}
        all_known = all(f.gauge in known_gauges for f in findings)
        _check(f"{name}: every finding gauge in {known_gauges}", all_known)


def test_opener_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    print()
    print("Test: OpenerAspect.analyze on every fixture")
    aspect = OpenerAspect(MCH_DATA)
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        findings = result.state.get("findings") or []
        _check(f"{name}: findings is a list (got {len(findings)})",
               isinstance(findings, list))
        positions = [f.position for f in findings]
        _check(f"{name}: positions are 1-indexed and within opener length",
               all(1 <= p <= len(MCH_DATA.canonical_opener) for p in positions))


def test_alignment_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    print()
    print("Test: AlignmentAspect.analyze on every fixture")
    aspect = AlignmentAspect(MCH_DATA)
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        findings = result.state.get("findings") or []
        _check(f"{name}: every finding is burst_misalign",
               all(f.kind == "burst_misalign" for f in findings))
        _check(f"{name}: every finding has positive lost_potency",
               all(f.lost_potency >= 0 for f in findings))


def test_abilities_aspect_runs_on_fixtures(fixtures: dict[str, dict]) -> None:
    """AbilityTimelineAspect produces an Abilities track with one event
    per non-pet cast."""
    print()
    print("Test: AbilityTimelineAspect.analyze on every fixture")
    aspect = AbilityTimelineAspect()
    for name, fix in fixtures.items():
        try:
            result = aspect.analyze(
                _make_client(fix), fix["report_code"],
                _fight(fix), _actor(fix), report={},
            )
        except Exception as e:
            _check(f"{name}: analyze ran cleanly", False,
                   f"raised {type(e).__name__}: {e}")
            continue
        _check(f"{name}: track has events",
               len(result.track.events) > 0,
               f"got {len(result.track.events)}")


# --- Main ------------------------------------------------------------------

def main() -> int:
    fixtures = _load_fixtures()
    print(f"Loaded {len(fixtures)} fixtures from {FIXTURES_DIR}")

    # Pure-function tests (no fixtures needed)
    test_compute_drift_pure_no_drift()
    test_compute_drift_pure_capped()
    test_compute_drift_pure_downtime_excluded()
    test_compute_drift_pure_dead_window_excluded()
    test_compute_drift_pure_charge_sharing()
    test_compute_drift_pure_cdr_rule()
    test_compute_clipping_pure_clean_pacing()
    test_compute_clipping_empty_gaps_are_idle_not_clip()
    test_compute_clipping_true_weave_clip()
    test_compute_clipping_excludes_dead_windows()
    test_compute_clipping_pure_few_casts()
    test_compute_clipping_skip_intervals_honored()
    test_compute_overcap_pure_heat()
    test_compute_overcap_pure_no_overcap()
    test_compute_overcap_pure_prepull_no_finding()
    test_compute_overcap_multi_gauge()
    test_compute_opener_pure_match()
    test_compute_opener_pure_substitution()
    test_compute_opener_pure_same_potency_swap()
    test_compute_burst_alignment_no_burst_abilities()
    test_compute_burst_alignment_flag_near_burst()
    test_compute_burst_alignment_no_flag_safely_offset()
    test_compute_burst_alignment_no_providers_suppressed()
    test_compute_burst_alignment_benefit_scales_with_comp()
    test_compute_burst_alignment_weak_single_provider_below_floor()
    test_compute_burst_alignment_unknown_comp_falls_back()
    test_raid_buff_registry_present_providers()
    test_alignment_aspect_comp_aware_end_to_end()

    # Fixture-driven smoke tests
    test_drift_aspect_runs_on_fixtures(fixtures)
    test_clipping_aspect_runs_on_fixtures(fixtures)
    test_overcap_aspect_runs_on_fixtures(fixtures)
    test_opener_aspect_runs_on_fixtures(fixtures)
    test_alignment_aspect_runs_on_fixtures(fixtures)
    test_abilities_aspect_runs_on_fixtures(fixtures)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
