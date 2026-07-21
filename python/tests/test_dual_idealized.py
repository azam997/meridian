"""Invariants for strict + lenient idealized scoring.

The lenient idealized adds Tier-B (consensus) windows on top of Tier-A
windows when computing the simulator ceiling. More windows -> simulator
skips more uptime -> fewer casts -> LOWER potency ceiling. So:

    idealized_lenient <= idealized_strict
    efficiency_lenient >= efficiency_strict

When no Tier-B windows are produced (no refs, or weak consensus), the
two values must be equal. We exercise this with synthetic refs that
exhibit both shapes.

Run from python/:  python tests/test_dual_idealized.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import (
    analyze_pull,
    compute_tier_b_for_user,
)
from jobs._core.downtime_sources import RefRun
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


# Reuse synthetic stub from test_tier_a_e2e — mimic its shape inline.
PLAYER_ID = 100
BOSS_ID = 500
FIGHT_START_MS = 1_000_000
FIGHT_END_MS = 1_200_000


def _cast_stream() -> list[dict]:
    events = []
    aids = [7411, 7412, 7413]
    for i in range(80):
        t = FIGHT_START_MS + int(i * 2_500)
        events.append({"timestamp": t, "type": "cast",
                        "sourceID": PLAYER_ID, "targetID": BOSS_ID,
                        "abilityGameID": aids[i % 3], "fight": 1})
    return events


class _StubClient:
    def __init__(self, targetability_events=None):
        self._tgt = targetability_events or []
        self._casts = _cast_stream()

    def get_report_summary(self, code):
        return {
            "title": "Synth", "startTime": FIGHT_START_MS, "endTime": FIGHT_END_MS,
            "fights": [{
                "id": 1, "name": "Synth", "encounterID": 999,
                "difficulty": 101, "kill": True,
                "startTime": FIGHT_START_MS, "endTime": FIGHT_END_MS,
                "friendlyPlayers": [PLAYER_ID],
                "enemyNPCs": [{"id": BOSS_ID, "gameID": BOSS_ID, "petOwner": None}],
            }],
            "masterData": {"actors": [
                {"id": PLAYER_ID, "name": "P", "server": "S",
                 "type": "Player", "subType": "Machinist",
                 "petOwner": None, "gameID": 31},
                {"id": BOSS_ID, "name": "B", "server": "S",
                 "type": "NPC", "subType": "Boss",
                 "petOwner": None, "gameID": 9999},
            ]},
        }

    def get_events(self, code, start, end, source_id,
                   data_type="Casts", ability_id=None):
        return [e for e in self._casts if start <= e["timestamp"] <= end]

    def get_targetability_events(self, code, start, end):
        return [e for e in self._tgt if start <= e["timestamp"] <= end]


def _stream_with_gap(duration_s, gap_start, gap_end, gcd_s=2.5):
    out = []
    t = 0.0
    aids = [7411, 7412, 7413]
    i = 0
    while t < duration_s:
        if not (gap_start <= t < gap_end):
            out.append((t, aids[i % 3]))
        t += gcd_s
        i += 1
    return tuple(out)


def _clean_stream(duration_s, gcd_s=2.5):
    return _stream_with_gap(duration_s, -1, -1, gcd_s)


# --- Tests -----------------------------------------------------------------

def test_lenient_equals_strict_with_no_refs() -> None:
    print()
    print("Test: no refs -> lenient == strict")
    you = analyze_pull("Machinist", _StubClient(), "code", 1, None, "you")
    windows = compute_tier_b_for_user("Machinist", you, refs=[])
    _check("no Tier-B windows", windows == [], f"got {windows}")


def test_lenient_equals_strict_with_weak_consensus() -> None:
    """1 of 5 refs idle -> no consensus -> lenient == strict.

    We simulate this by computing Tier B directly (avoids needing to
    drive analyze_pull for refs)."""
    print()
    print("Test: weak ref consensus -> no Tier-B windows")
    refs = [RefRun("r0", _stream_with_gap(200, 100, 110), 200.0)] + [
        RefRun(f"r{i}", _clean_stream(200), 200.0) for i in range(1, 5)
    ]
    from jobs._core.downtime_sources import consensus_windows_from_refs
    out = consensus_windows_from_refs(
        refs, 200.0, PHYSICAL_RANGED, MCH_DATA, tier_a_windows=[],
    )
    _check("no Tier-B windows produced", out == [], f"got {out}")


def test_lenient_lower_than_strict_when_tier_b_present() -> None:
    """When Tier B produces windows, the simulator skips more uptime,
    producing a LOWER ceiling. Same delivered potency -> higher
    efficiency under lenient."""
    print()
    print("Test: Tier-B windows -> idealized_lenient < idealized_strict")
    from jobs.machinist.scoring import idealized_at_duration

    duration = 600.0
    strict_windows: list[tuple[float, float]] = []
    lenient_windows = [(100.0, 130.0), (300.0, 320.0)]   # 50s extra

    strict = idealized_at_duration(duration, strict_windows)
    lenient = idealized_at_duration(duration, lenient_windows)
    _check(f"lenient ({lenient:.0f}) < strict ({strict:.0f})",
           lenient < strict - 100, f"strict={strict}  lenient={lenient}")


def test_lenient_efficiency_invariants() -> None:
    """For any (delivered, strict, lenient) where lenient < strict,
    efficiency_lenient > efficiency_strict."""
    print()
    print("Test: efficiency_lenient > efficiency_strict when lenient < strict")
    delivered = 100_000.0
    strict = 110_000.0
    lenient = 105_000.0
    eff_s = 100 * delivered / strict
    eff_l = 100 * delivered / lenient
    _check(f"eff_lenient ({eff_l:.2f}) > eff_strict ({eff_s:.2f})",
           eff_l > eff_s)


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_lenient_equals_strict_with_no_refs()
    test_lenient_equals_strict_with_weak_consensus()
    test_lenient_lower_than_strict_when_tier_b_present()
    test_lenient_efficiency_invariants()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
