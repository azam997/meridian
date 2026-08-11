"""Tests for boss phase segmentation (`jobs/_core/phases.py`).

Pure computation over synthetic report/fight dicts — no client, no network.
Anchored on the LIVE Dancing Mad (encounter 1085) shape probed from FFLogs:
fight span 36810158–37929573 ms, phaseTransitions at
[36810158, 37018971, 37238298, 37543177, 37706313].

Run from python/:  python tests/test_phases.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.phases import (
    Phase,
    downtime_overlap_s,
    encounter_phase_names,
    phase_segments,
    split_casts_by_phase,
)

_PASSED: list[str] = []
_FAILED: list = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


# --- Live Dancing Mad shape --------------------------------------------------

_DM_START = 36_810_158
_DM_END = 37_929_573
_DM_TRANSITIONS = [
    {"id": 1, "startTime": 36_810_158},
    {"id": 2, "startTime": 37_018_971},
    {"id": 3, "startTime": 37_238_298},
    {"id": 4, "startTime": 37_543_177},
    {"id": 5, "startTime": 37_706_313},
]
_DM_NAMES = [
    {"id": 1, "name": "P1: Kefka", "isIntermission": False},
    {"id": 2, "name": "P2: Forsaken Kefka", "isIntermission": False},
    {"id": 3, "name": "P3: Exdeath and Chaos", "isIntermission": False},
    {"id": 4, "name": "P4: Kefka Says", "isIntermission": False},
    {"id": 5, "name": "P5: Ultima Kefka", "isIntermission": False},
]


def _dm_report(names=_DM_NAMES) -> dict:
    return {"phases": [{"encounterID": 1085, "separatesWipes": True,
                        "phases": names}]} if names is not None else {}


def _dm_fight(end=_DM_END, transitions=_DM_TRANSITIONS) -> dict:
    return {"encounterID": 1085, "startTime": _DM_START, "endTime": end,
            "phaseTransitions": transitions}


def test_dancing_mad_kill() -> None:
    ph = phase_segments(_dm_report(), _dm_fight())
    _check("5 phases", len(ph) == 5, f"got {len(ph)}")
    _check("P1 anchored at fight start (0s)", abs(ph[0].start_s) < 1e-6,
           f"got {ph[0].start_s}")
    _check("P1 name from report", ph[0].name == "P1: Kefka", ph[0].name)
    _check("P4 name from report", ph[3].name == "P4: Kefka Says", ph[3].name)
    # P2 starts at (37018971-36810158)/1000 = 208.813s
    _check("P2 start ms->s", abs(ph[1].start_s - 208.813) < 1e-3,
           f"got {ph[1].start_s}")
    # Contiguity: each phase's end == the next's start.
    _check("contiguous", all(abs(ph[i].end_s - ph[i + 1].start_s) < 1e-9
                             for i in range(len(ph) - 1)))
    # Last phase closes at the fight end.
    _check("last phase ends at fight end",
           abs(ph[-1].end_s - (_DM_END - _DM_START) / 1000.0) < 1e-6,
           f"got {ph[-1].end_s}")
    _check("ids 1..5", [p.id for p in ph] == [1, 2, 3, 4, 5])


def test_missing_names_fallback() -> None:
    """No report `phases` block -> names fall back to P{id}."""
    ph = phase_segments(_dm_report(names=None), _dm_fight())
    _check("fallback names", [p.name for p in ph] == ["P1", "P2", "P3", "P4", "P5"],
           str([p.name for p in ph]))


def test_unsorted_and_duplicate_transitions() -> None:
    """Out-of-order transitions sort; a duplicated timestamp is dropped."""
    shuffled = [_DM_TRANSITIONS[2], _DM_TRANSITIONS[0], _DM_TRANSITIONS[4],
                dict(_DM_TRANSITIONS[0]),  # duplicate of id=1 timestamp
                _DM_TRANSITIONS[1], _DM_TRANSITIONS[3]]
    ph = phase_segments(_dm_report(), _dm_fight(transitions=shuffled))
    _check("dedup + sort -> 5 phases", len(ph) == 5, f"got {len(ph)}")
    _check("sorted by start", all(ph[i].start_s <= ph[i + 1].start_s
                                  for i in range(len(ph) - 1)))


def test_savage_no_transitions() -> None:
    """A fight with no phaseTransitions -> no segments (Savage stays clean)."""
    fight = {"encounterID": 101, "startTime": 1000, "endTime": 500_000}
    _check("no transitions -> ()", phase_segments({}, fight) == ())
    _check("empty transitions -> ()",
           phase_segments({}, {**fight, "phaseTransitions": []}) == ())


def test_wipe_full_span() -> None:
    """A wipe: full_end_ms extends the last phase past the (earlier) scored
    end so the phases the raid reached are still covered."""
    # Raid reached P3, wiped at 37_300_000 (mid-P3). full_end = wipe end.
    wipe_transitions = _DM_TRANSITIONS[:3]  # only P1..P3 reached
    ph = phase_segments(_dm_report(), _dm_fight(
        end=37_300_000, transitions=wipe_transitions), full_end_ms=37_300_000)
    _check("3 reached phases", len(ph) == 3, f"got {len(ph)}")
    _check("last phase ends at wipe end",
           abs(ph[-1].end_s - (37_300_000 - _DM_START) / 1000.0) < 1e-6,
           f"got {ph[-1].end_s}")


def test_downtime_overlap() -> None:
    p = Phase(id=2, name="P2", start_s=100.0, end_s=200.0, is_intermission=False)
    # windows: one fully inside, one straddling the start, one outside.
    windows = [(120.0, 140.0), (90.0, 110.0), (300.0, 320.0)]
    _check("overlap sums clipped intersections",
           abs(downtime_overlap_s(p, windows) - (20.0 + 10.0)) < 1e-9,
           str(downtime_overlap_s(p, windows)))


def test_encounter_phase_names_matching() -> None:
    report = {"phases": [
        {"encounterID": 999, "phases": [{"id": 1, "name": "Other"}]},
        {"encounterID": 1085, "phases": [
            {"id": 1, "name": "P1: Kefka", "isIntermission": False},
            {"id": 2, "name": "Break", "isIntermission": True}]},
    ]}
    names = encounter_phase_names(report, 1085)
    _check("matches by encounterID", names.get(1) == ("P1: Kefka", False), str(names))
    _check("intermission flag carried", names.get(2) == ("Break", True), str(names))
    # Single-block fallback when the id doesn't match.
    solo = {"phases": [{"encounterID": 42, "phases": [{"id": 1, "name": "Solo"}]}]}
    _check("single-block fallback", encounter_phase_names(solo, 999).get(1)
           == ("Solo", False), str(encounter_phase_names(solo, 999)))
    _check("no block -> empty", encounter_phase_names({}, 1085) == {})


def test_split_casts_by_phase() -> None:
    ph = phase_segments(_dm_report(), _dm_fight())
    casts = [(0.0, 100), (208.813, 200), (250.0, 300), (1119.0, 400)]
    buckets = split_casts_by_phase(casts, ph)
    _check("cast at 0 -> P1", buckets[0] == [(0.0, 100)], str(buckets[0]))
    _check("cast at P2 boundary -> P2",
           (208.813, 200) in buckets[1] and (250.0, 300) in buckets[1],
           str(buckets[1]))
    _check("trailing cast -> last phase", buckets[4] == [(1119.0, 400)],
           str(buckets[4]))
    _check("no phases -> empty buckets", split_casts_by_phase(casts, ()) == [])


def main() -> None:
    for fn in [test_dancing_mad_kill, test_missing_names_fallback,
               test_unsorted_and_duplicate_transitions, test_savage_no_transitions,
               test_wipe_full_span, test_downtime_overlap,
               test_encounter_phase_names_matching, test_split_casts_by_phase]:
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{len(_PASSED)} checks passed, {len(_FAILED)} failed")
    if _FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
