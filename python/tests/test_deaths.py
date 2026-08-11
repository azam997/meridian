"""Tests for player-death handling.

Three layers:
  1. Pure death-window reconstruction (`jobs/_core/deaths.py`).
  2. The priced Death improvement (`improvements_from_deaths`).
  3. Sidecar integration: a missed cast inside a dead window is suppressed
     (the Death card owns it), and the reconciled panel still sums to the
     recoverable gap.

Run from python/:  python tests/test_deaths.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.aspect import AspectResult, Track
from jobs._core.deaths import (
    compute_death_windows,
    read_deaths_from_report,
    resolve_deaths,
)
from jobs._core.improvements import _in_any, improvements_from_deaths
from jobs._core.module_result import ModuleResult
from jobs.machinist.data import JOB_DATA as MCH

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


DRILL = 16498        # GCD tool, 660p
SPLIT = 7411         # GCD combo step, 220p
DOUBLE_CHECK = 36979  # damaging oGCD, 180p
WILDFIRE = 2878      # enabler oGCD, no direct potency


# --- Stub clients -----------------------------------------------------------

class _DeathClient:
    """Returns canned Deaths events (report-relative ms baked by the caller)."""

    def __init__(self, events: list[dict]):
        self._events = events

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Deaths":
            return []
        return [e for e in self._events if start <= e["timestamp"] <= end]


class _RaisingClient:
    def get_events(self, *a, **k):
        raise RuntimeError("network down")


# --- compute_death_windows --------------------------------------------------

def test_window_recovery_next_cast() -> None:
    print("\nTest: dead window runs death -> next cast (resurrection)")
    casts = [(0.0, SPLIT), (5.0, SPLIT), (40.0, SPLIT), (42.5, SPLIT)]
    out = compute_death_windows([10.0], casts, 100.0)
    _check("window is (10, 40)", out == [(10.0, 40.0)], f"got {out}")


def test_window_no_recovery_runs_to_end() -> None:
    print("\nTest: a death with no later cast runs to fight end")
    casts = [(0.0, SPLIT), (5.0, SPLIT)]
    out = compute_death_windows([520.0], casts, 595.9)
    _check("window ends at fight end", out == [(520.0, 595.9)], f"got {out}")


def test_windows_coalesce() -> None:
    print("\nTest: two deaths before recovery coalesce into one window")
    casts = [(0.0, SPLIT), (50.0, SPLIT)]
    out = compute_death_windows([10.0, 20.0], casts, 100.0)
    _check("coalesced to (10, 50)", out == [(10.0, 50.0)], f"got {out}")


def test_prepull_casts_ignored_for_recovery() -> None:
    print("\nTest: pre-pull casts don't count as recovery")
    casts = [(-3.0, SPLIT), (30.0, SPLIT)]
    out = compute_death_windows([10.0], casts, 100.0)
    _check("recovers at the first in-fight cast (30s)",
           out == [(10.0, 30.0)], f"got {out}")


# --- resolve_deaths ---------------------------------------------------------

def test_resolve_filters_type_and_relativizes() -> None:
    print("\nTest: resolve_deaths reads Deaths events, fight-relative")
    start = 1_000_000
    fight = {"startTime": start, "endTime": start + 100_000}
    casts = [(0.0, SPLIT), (60.0, SPLIT)]
    events = [
        {"timestamp": start + 30_000, "type": "death", "targetID": 15},
        {"timestamp": start + 40_000, "type": "calculateddamage"},  # ignored
    ]
    out = resolve_deaths(_DeathClient(events), "c", fight, {"id": 15}, casts)
    _check("death at 30s, recover at 60s", out == [(30.0, 60.0)], f"got {out}")


def test_resolve_best_effort_on_error() -> None:
    print("\nTest: resolve_deaths returns [] when the fetch fails")
    fight = {"startTime": 0, "endTime": 100_000}
    out = resolve_deaths(_RaisingClient(), "c", fight, {"id": 1}, [(0.0, SPLIT)])
    _check("empty on error", out == [], f"got {out}")


def test_read_deaths_from_report() -> None:
    print("\nTest: read_deaths_from_report reads the stash")
    _check("missing stash -> []", read_deaths_from_report({}) == [])
    rep = {"__deaths__": {"windows": [(10.0, 30.0)]}}
    _check("reads stashed windows",
           read_deaths_from_report(rep) == [(10.0, 30.0)])


# --- improvements_from_deaths -----------------------------------------------

def test_death_card_prices_full_value() -> None:
    print("\nTest: Death card = full value of idealized casts in the window")
    # window (10, 30). In-window: DRILL@15, SPLIT@20, DOUBLE_CHECK@25 (oGCD).
    ideal = [(5.0, SPLIT), (15.0, DRILL), (20.0, SPLIT),
             (25.0, DOUBLE_CHECK), (40.0, DRILL)]
    out = improvements_from_deaths([(10.0, 30.0)], ideal, MCH)
    _check("one death card", len(out) == 1, f"got {len(out)}")
    im = out[0]
    expect = (MCH.potencies[DRILL] + MCH.potencies[SPLIT]
              + MCH.potencies[DOUBLE_CHECK])
    _check("priced at full-value sum of in-window casts (no filler subtraction)",
           abs(im.lost_potency - expect) < 0.01,
           f"got {im.lost_potency} want {expect}")
    _check("kind is death", im.kind == "death", im.kind)
    _check("located at the death moment (10s)", im.time_s == 10.0,
           f"got {im.time_s}")
    _check("summary names time + dead duration + GCD count (oGCD not counted)",
           "0:10" in im.summary and "20s" in im.summary
           and "~2 GCDs" in im.summary, f"got {im.summary!r}")


def test_death_card_enabler_from_net_value() -> None:
    print("\nTest: in-window enabler priced at its sim-derived net value")
    ideal = [(15.0, WILDFIRE), (16.0, DRILL)]
    out = improvements_from_deaths([(10.0, 30.0)], ideal, MCH,
                                   enabler_values={WILDFIRE: 1200.0})
    expect = 1200.0 + MCH.potencies[DRILL]
    _check("enabler net value + drill full value",
           len(out) == 1 and abs(out[0].lost_potency - expect) < 0.01,
           f"got {out}")


def test_death_card_empty_without_windows() -> None:
    print("\nTest: no windows -> no death cards")
    _check("empty", improvements_from_deaths([], [(5.0, DRILL)], MCH) == [])


def test_in_any_helper() -> None:
    print("\nTest: _in_any membership (half-open [s, e))")
    w = [(10.0, 30.0)]
    _check("inside", _in_any(15.0, w))
    _check("start inclusive", _in_any(10.0, w))
    _check("end exclusive", not _in_any(30.0, w))
    _check("outside", not _in_any(5.0, w))


# --- Sidecar integration: suppression + reconciliation ----------------------

def _you_with_death() -> ModuleResult:
    """Player cast DRILL at 0 and 30; the idealized fits four (0/30/60/100).
    The 60s one falls in the dead window (50, 80); the 100s one doesn't."""
    you = ModuleResult(
        label="You", fight_duration_s=200.0,
        death_windows=[(50.0, 80.0)],
        norm_casts=((0.0, DRILL), (30.0, DRILL)),
    )
    you.aspects["Scoring"] = AspectResult(
        name="Scoring", track=Track(name="Scoring", events=[]),
        state={"idealized_strict": 5000.0, "delivered_potency": 3000.0,
               "enabler_net_values": {}},
    )
    return you


def test_sidecar_suppresses_in_window_miss_and_reconciles() -> None:
    print("\nTest: _build_improvements suppresses in-window misses; sums to gap")
    import jobs
    jobs.get_job("Machinist")   # trigger lazy registration (analyze_pull does this in prod)
    from sidecar.main import _build_improvements
    you = _you_with_death()
    ideal = [(0.0, DRILL), (30.0, DRILL), (60.0, DRILL), (100.0, DRILL)]
    out = _build_improvements("Machinist", you, ideal)

    deaths = [im for im in out if im["kind"] == "death"]
    _check("exactly one death card at the death moment (50s)",
           len(deaths) == 1 and deaths[0]["timeSec"] == 50.0, f"got {deaths}")

    misses = [im for im in out if im["kind"] == "missed_cast"]
    _check("the in-window missed Drill (60s) was suppressed",
           all(not _in_any(im["timeSec"], [(50.0, 80.0)]) for im in misses),
           f"got {[im['timeSec'] for im in misses]}")
    _check("the out-of-window missed Drill (100s) survives",
           any(abs(im["timeSec"] - 100.0) < 0.01 for im in misses),
           f"got {[im['timeSec'] for im in misses]}")

    total = sum(float(im.get("lostPotency", 0) or 0) for im in out)
    _check("panel still sums to the recoverable gap (2000)",
           abs(total - 2000.0) < 1e-6, f"got {total}")


def main() -> int:
    test_window_recovery_next_cast()
    test_window_no_recovery_runs_to_end()
    test_windows_coalesce()
    test_prepull_casts_ignored_for_recovery()
    test_resolve_filters_type_and_relativizes()
    test_resolve_best_effort_on_error()
    test_read_deaths_from_report()
    test_death_card_prices_full_value()
    test_death_card_enabler_from_net_value()
    test_death_card_empty_without_windows()
    test_in_any_helper()
    test_sidecar_suppresses_in_window_miss_and_reconciles()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
