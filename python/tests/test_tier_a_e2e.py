"""End-to-end tests for Tier-A downtime through the analyze_pull pipeline.

Uses a stub client that returns synthetic targetability events alongside
a normal cast stream so we can verify:

  - When the stub serves a Vamp-Fatale-shape window, `analyze_pull`
    surfaces it on `ModuleResult.downtime_windows` with
    `downtime_source == "targetability"`.
  - Drift attribution stops penalizing capped cooldowns that overlap the
    confirmed window.
  - Clip attribution stops counting the gap that overlaps the confirmed
    window.
  - When the stub serves NO targetability events (boss confirmed always
    targetable, Tyrant-shape), `downtime_source` is still
    "targetability" — not the heuristic fallback.
  - When the stub doesn't implement targetability at all, the heuristic
    fallback kicks in.

Run from python/:  python tests/test_tier_a_e2e.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull


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


# --- Synthetic MCH fixture -------------------------------------------------

# Build a minimal cast stream for a 200-second fight: AA every 40s, Drill
# every 20s (with both charges initialized), Heated combo filling between,
# plus enough HC heat to make scoring work. The numbers don't have to be
# realistic — we're testing the downtime plumbing, not the scoring math.

PLAYER_ID = 100
BOSS_ID = 500
FIGHT_START_MS = 1_000_000
FIGHT_END_MS = 1_200_000  # 200s fight


def _build_cast_events() -> list[dict[str, Any]]:
    """200s of casts: GCD every 2.5s alternating split/slug/clean +
    interspersed cooldown tools. Just dense enough that any 30s+ gap is
    obviously structural."""
    events: list[dict[str, Any]] = []
    aids = [7411, 7412, 7413]   # heated combo
    for i in range(80):
        t = FIGHT_START_MS + int(i * 2_500)
        events.append({
            "timestamp": t, "type": "cast", "sourceID": PLAYER_ID,
            "targetID": BOSS_ID, "abilityGameID": aids[i % 3], "fight": 1,
        })
    # Add a Drill cast at t=10s and t=30s so cooldown-drift math has data
    events.append({"timestamp": FIGHT_START_MS + 10_000, "type": "cast",
                    "sourceID": PLAYER_ID, "targetID": BOSS_ID,
                    "abilityGameID": 16498, "fight": 1})
    events.append({"timestamp": FIGHT_START_MS + 30_000, "type": "cast",
                    "sourceID": PLAYER_ID, "targetID": BOSS_ID,
                    "abilityGameID": 16498, "fight": 1})
    return events


class _StubClient:
    """Synthetic FFLogs client. `targetability_events` is the only knob —
    set on construction to control the Tier-A behavior under test.

    `supports_targetability=False` makes the method raise like a real
    older client without the endpoint.
    """

    def __init__(self, targetability_events: list[dict] | None,
                 supports_targetability: bool = True,
                 enemy_npc_ids: tuple[int, ...] = (BOSS_ID,)):
        self._cast_events = _build_cast_events()
        self._tgt_events = targetability_events or []
        self._enemy_npc_ids = enemy_npc_ids
        self._supports_targetability = supports_targetability

    def get_report_summary(self, code: str) -> dict:
        return {
            "title": "Tier A e2e",
            "startTime": FIGHT_START_MS,
            "endTime": FIGHT_END_MS,
            "fights": [{
                "id": 1,
                "name": "Synth",
                "encounterID": 999,
                "difficulty": 101,
                "kill": True,
                "startTime": FIGHT_START_MS,
                "endTime": FIGHT_END_MS,
                "friendlyPlayers": [PLAYER_ID],
                "enemyNPCs": [{"id": nid, "gameID": nid, "petOwner": None}
                              for nid in self._enemy_npc_ids],
            }],
            "masterData": {
                "actors": [
                    {"id": PLAYER_ID, "name": "TestPlayer",
                     "server": "Test", "type": "Player",
                     "subType": "Machinist", "petOwner": None,
                     "gameID": 31},
                    {"id": BOSS_ID, "name": "TestBoss",
                     "server": "Test", "type": "NPC",
                     "subType": "Boss", "petOwner": None,
                     "gameID": 9999},
                ],
            },
        }

    def get_events(self, code, start, end, source_id,
                   data_type="Casts", ability_id=None) -> list[dict]:
        return [e for e in self._cast_events
                if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end) -> list[dict]:
        if not self._supports_targetability:
            raise AttributeError("legacy client doesn't support targetability")
        return [e for e in self._tgt_events
                if start <= e["timestamp"] <= end]


def _ev(t_rel_s: float, target_id: int, targetable: int) -> dict:
    return {
        "timestamp": FIGHT_START_MS + int(t_rel_s * 1000),
        "type": "targetabilityupdate",
        "sourceID": target_id,
        "targetID": target_id,
        "abilityGameID": 0,
        "fight": 1,
        "targetable": targetable,
    }


# --- Tests -----------------------------------------------------------------

def test_vamp_fatale_window_surfaces() -> None:
    """50s untargetable window (Vamp Fatale-shape) — analyzed run should
    show that window on ModuleResult and source=targetability."""
    print()
    print("Test: Tier-A — Vamp-Fatale-shape window surfaces end-to-end")
    events = [_ev(60.0, BOSS_ID, 0), _ev(110.0, BOSS_ID, 1)]
    client = _StubClient(events)
    mr = analyze_pull("Machinist", client, "code", 1, None, "test")
    _check("downtime_source == targetability",
           mr.downtime_source == "targetability",
           f"got {mr.downtime_source!r}")
    _check("one window present",
           len(mr.downtime_windows) == 1,
           f"got {len(mr.downtime_windows)}")
    if mr.downtime_windows:
        s, e = mr.downtime_windows[0]
        _check("window 60.0s -> 110.0s",
               abs(s - 60.0) < 0.01 and abs(e - 110.0) < 0.01,
               f"got ({s}, {e})")


def test_drift_attribution_subtracts_window() -> None:
    """When Tier A reports a window, Drift should NOT penalize cooldowns
    sitting capped during that window. Compare a no-window run to a
    with-window run and confirm drift cost dropped."""
    print()
    print("Test: Tier-A — Drift attribution subtracts confirmed window")
    # No targetability events => all 200s is uptime, big drift on Drill
    # (we only cast it twice).
    no_window = analyze_pull("Machinist", _StubClient([]), "code", 1,
                              None, "no_window")
    # 100s of confirmed downtime in the middle => Drill drift should be
    # roughly halved because the cap interval overlaps the window.
    with_window = analyze_pull(
        "Machinist", _StubClient([_ev(50.0, BOSS_ID, 0),
                                    _ev(150.0, BOSS_ID, 1)]),
        "code", 1, None, "with_window",
    )

    # The Drift aspect carries DriftFinding objects for every non-excluded
    # cooldown. We want the Drill (16498) finding specifically.
    def drill_drift(mr) -> float:
        findings = mr.aspects["Drift"].state.get("findings", [])
        for f in findings:
            if f.ability_id == 16498:
                return f.capped_seconds
        return 0.0

    no_w = drill_drift(no_window)
    with_w = drill_drift(with_window)
    _check(f"Drill capped_seconds dropped (no_window={no_w:.1f}s, with_window={with_w:.1f}s)",
           with_w < no_w - 30.0,    # at least 30s less drift attributed
           f"no={no_w}  with={with_w}")


def test_no_events_means_targetability_not_fallback() -> None:
    """Empty events with the method present should be the 'confirmed
    always targetable' signal — source stays 'targetability', not
    'fallback_heuristic'. (Tyrant-shape behavior.)"""
    print()
    print("Test: Tier-A — empty events => source still 'targetability'")
    mr = analyze_pull("Machinist", _StubClient([]), "code", 1, None, "tyrant")
    _check("source == targetability", mr.downtime_source == "targetability",
           f"got {mr.downtime_source!r}")
    _check("no windows", mr.downtime_windows == [],
           f"got {mr.downtime_windows}")


def test_missing_method_falls_back_to_heuristic() -> None:
    """If the client predates Tier A (no method at all), the fallback
    heuristic should engage."""
    print()
    print("Test: Tier-A — missing method => fallback heuristic")
    mr = analyze_pull("Machinist",
                       _StubClient([], supports_targetability=False),
                       "code", 1, None, "legacy_client")
    _check("source == fallback_heuristic",
           mr.downtime_source == "fallback_heuristic",
           f"got {mr.downtime_source!r}")


def test_add_gap_masked_by_live_boss() -> None:
    """An add going untargetable while the boss is still up is not
    downtime — the player still has the boss to hit. The boss never
    flips here, so it covers the whole fight."""
    print()
    print("Test: Tier-A - add gap masked by always-up boss")
    # A non-boss enemy goes untargetable; the boss does NOT flip.
    events = [_ev(60.0, 700, 0), _ev(120.0, 700, 1)]
    client = _StubClient(events, enemy_npc_ids=(BOSS_ID, 700))
    mr = analyze_pull("Machinist", client, "code", 1, None, "addflip")
    _check("source == targetability (events fetched)",
           mr.downtime_source == "targetability",
           f"got {mr.downtime_source!r}")
    _check("no downtime (boss stayed targetable)",
           mr.downtime_windows == [], f"got {mr.downtime_windows}")


def test_boss_gap_covered_by_add_spawn() -> None:
    """The M9S Vamp Fatale case end-to-end: the boss leaves and an add
    spawns to cover the gap, so the long boss-untargetable window is NOT
    counted as downtime — only the sliver before the add appears."""
    print()
    print("Test: Tier-A - boss gap covered by spawning add")
    events = [
        _ev(60.0, BOSS_ID, 0),     # boss leaves
        _ev(110.0, BOSS_ID, 1),    # boss returns
        _ev(61.0, 700, 1),         # add spawns targetable 1s later
    ]
    client = _StubClient(events, enemy_npc_ids=(BOSS_ID, 700))
    mr = analyze_pull("Machinist", client, "code", 1, None, "addcover")
    _check("one sliver window", len(mr.downtime_windows) == 1,
           f"got {mr.downtime_windows}")
    if mr.downtime_windows:
        s, e = mr.downtime_windows[0]
        _check("downtime only 60.0s -> 61.0s",
               abs(s - 60.0) < 0.01 and abs(e - 61.0) < 0.01,
               f"got ({s}, {e})")


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_vamp_fatale_window_surfaces()
    test_drift_attribution_subtracts_window()
    test_no_events_means_targetability_not_fallback()
    test_missing_method_falls_back_to_heuristic()
    test_add_gap_masked_by_live_boss()
    test_boss_gap_covered_by_add_spawn()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
