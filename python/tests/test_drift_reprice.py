"""Unit tests for the sim-priced Drift table
(sidecar.main._reprice_drift_from_sim): drift only costs potency once a use
no longer fits before the kill, priced on the missed-cast cards' basis.

Run from python/:  python tests/test_drift_reprice.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sidecar.main as M
from jobs._aspects.drift import DriftFinding
from jobs._core.aspect import AspectResult, Track
from jobs._core.module_result import ModuleResult
from jobs.machinist.data import JOB_DATA as MCH

DRILL = 16498         # GCD tool, 660p (filler 220 -> net 440)
REASSEMBLE = 2876     # enabler, 0 direct potency
DOUBLE_CHECK = 36979  # damaging oGCD, 180p full

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _you(findings: list[DriftFinding], casts: list[tuple[float, int]],
         enabler: dict[int, float] | None = None) -> ModuleResult:
    return ModuleResult(
        label="You", fight_duration_s=300.0,
        aspects={
            "Drift": AspectResult(name="Drift",
                                  track=Track(name="Drift", events=[]),
                                  state={"findings": findings}),
            "Scoring": AspectResult(
                name="Scoring", track=Track(name="Scoring", events=[]),
                state={"enabler_net_values": enabler or {}}),
        },
        norm_casts=tuple(casts))


def _finding(aid: int, name: str, casts: int,
             heuristic_p: float) -> DriftFinding:
    return DriftFinding(ability_id=aid, ability_name=name, casts=casts,
                        capped_seconds=14.2, lost_casts=1.0,
                        lost_potency=heuristic_p)


def test_no_use_lost_reads_zero() -> None:
    print("\nTest: equal counts -> the heuristic -200p becomes 0")
    you = _you([_finding(REASSEMBLE, "Reassemble", 12, 200.0)],
               [(float(i * 20), REASSEMBLE) for i in range(12)],
               enabler={REASSEMBLE: 200.0})
    ideal = [(float(i * 20), REASSEMBLE) for i in range(12)]
    M._reprice_drift_from_sim(you, ideal, MCH)
    f = you.aspects["Drift"].state["findings"][0]
    _check("lost potency 0 when every use still fits",
           f.lost_potency == 0.0 and f.lost_casts == 0.0,
           f"got {f.lost_potency} / {f.lost_casts}")
    _check("capped seconds kept as the factual observation",
           abs(f.capped_seconds - 14.2) < 1e-9, f"got {f.capped_seconds}")


def test_deficit_priced_on_card_basis() -> None:
    print("\nTest: a real deficit prices GCD net-of-filler / oGCD full / enabler net")
    findings = [
        _finding(DRILL, "Drill", 10, 660.0),
        _finding(DOUBLE_CHECK, "Double Check", 5, 180.0),
        _finding(REASSEMBLE, "Reassemble", 3, 200.0),
    ]
    casts = ([(float(i * 20), DRILL) for i in range(10)]
             + [(float(i * 30), DOUBLE_CHECK) for i in range(5)]
             + [(float(i * 55), REASSEMBLE) for i in range(3)])
    ideal = ([(float(i * 20), DRILL) for i in range(11)]          # +1
             + [(float(i * 30), DOUBLE_CHECK) for i in range(7)]  # +2
             + [(float(i * 55), REASSEMBLE) for i in range(4)])   # +1
    you = _you(findings, casts, enabler={REASSEMBLE: 210.0})
    M._reprice_drift_from_sim(you, ideal, MCH)
    by_id = {f.ability_id: f for f in you.aspects["Drift"].state["findings"]}
    net_drill = 660.0 - float(MCH.filler_gcd_potency)
    _check("GCD tool priced net of filler",
           abs(by_id[DRILL].lost_potency - net_drill) < 0.11,
           f"got {by_id[DRILL].lost_potency} want ~{net_drill}")
    _check("damaging oGCD priced at full value ×2",
           abs(by_id[DOUBLE_CHECK].lost_potency - 360.0) < 0.11,
           f"got {by_id[DOUBLE_CHECK].lost_potency}")
    _check("enabler priced at sim-derived net value",
           abs(by_id[REASSEMBLE].lost_potency - 210.0) < 0.11,
           f"got {by_id[REASSEMBLE].lost_potency}")
    _check("rows re-sorted, priced first",
           you.aspects["Drift"].state["findings"][0].lost_potency > 0,
           "zero row leads")


def test_sim_less_untouched() -> None:
    print("\nTest: no idealized timeline -> heuristic pricing stays")
    you = _you([_finding(REASSEMBLE, "Reassemble", 12, 200.0)],
               [(0.0, REASSEMBLE)])
    M._reprice_drift_from_sim(you, [], MCH)
    f = you.aspects["Drift"].state["findings"][0]
    _check("heuristic kept for sim-less jobs",
           f.lost_potency == 200.0, f"got {f.lost_potency}")


def main() -> int:
    test_no_use_lost_reads_zero()
    test_deficit_priced_on_card_basis()
    test_sim_less_untouched()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
