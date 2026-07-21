"""Unit tests for the silent-despawn tail cap in Tier-A downtime (no network).

Ultimate boss relays (first seen on Dancing Mad) remove a phase's boss actor
without a closing `targetable=0`, so its spawn-opened targetability tail runs
to fight end and masks every later transition's real downtime. The cap closes
such tails at last observed activity + grace — but only with positive
evidence, so everything else stays byte-identical.

Exercises:
  - actor_targetable_intervals: spawn-opened tail capped by activity; None
    activity / present-at-pull opening / explicit close all unchanged.
  - enemy_last_activity: per-actor max over enemy casts + player damage.
  - compute_downtime_windows: the Dancing-Mad-shaped relay recovers the
    transition windows; without activity it reproduces the old (masked) result.
  - fetch_tier_a_windows: evidence fetch failure disables the cap.

Run from python/:  python tests/test_tier_a_silent_despawn.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.downtime_sources import (
    SILENT_DESPAWN_TAIL_GRACE_S,
    actor_targetable_intervals,
    compute_downtime_windows,
    enemy_last_activity,
    fetch_tier_a_windows,
)


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


def _ev(timestamp: int, target_id: int, targetable: int) -> dict:
    return {
        "timestamp": timestamp,
        "type": "targetabilityupdate",
        "sourceID": target_id,
        "targetID": target_id,
        "targetable": targetable,
    }


# --- actor_targetable_intervals: the tail cap --------------------------------

def test_spawn_tail_capped_by_activity() -> None:
    print()
    print("Test: spawn-opened tail capped at last activity + grace")
    # Spawn at 10s, never closed, last activity 50s, fight 200s.
    out = actor_targetable_intervals([_ev(10_000, 1, 1)], 0, 200_000,
                                     spawn_tail_last_activity_s=50.0)
    want = [(10.0, 50.0 + SILENT_DESPAWN_TAIL_GRACE_S)]
    _check("capped at activity+grace", out == want, f"got {out}")


def test_spawn_tail_without_activity_unchanged() -> None:
    print()
    print("Test: no activity evidence (None) => historic fight-end tail")
    out = actor_targetable_intervals([_ev(10_000, 1, 1)], 0, 200_000,
                                     spawn_tail_last_activity_s=None)
    _check("tail to fight end", out == [(10.0, 200.0)], f"got {out}")


def test_present_at_pull_tail_never_capped() -> None:
    print()
    print("Test: implicit present-at-pull opening is never capped")
    # First event is a 0 at 40s, reopens... no: only event is a 0 at 40s —
    # present from pull, closed at 40. Then no open tail at all.
    out = actor_targetable_intervals([_ev(40_000, 1, 0)], 0, 200_000,
                                     spawn_tail_last_activity_s=15.0)
    _check("closed interval untouched", out == [(0.0, 40.0)], f"got {out}")
    # No events at all: fight-long, even with activity supplied.
    out2 = actor_targetable_intervals([], 0, 200_000,
                                      spawn_tail_last_activity_s=15.0)
    _check("no-events actor untouched (Tyrant invariant)",
           out2 == [(0.0, 200.0)], f"got {out2}")


def test_reopened_tail_is_spawn_opened() -> None:
    print()
    print("Test: a tail reopened by an explicit 1 gets the cap too")
    # Present from pull, closed at 30s, reopens at 50s, silent despawn ~90s.
    evs = [_ev(30_000, 1, 0), _ev(50_000, 1, 1)]
    out = actor_targetable_intervals(evs, 0, 200_000,
                                     spawn_tail_last_activity_s=90.0)
    want = [(0.0, 30.0), (50.0, 90.0 + SILENT_DESPAWN_TAIL_GRACE_S)]
    _check("reopened tail capped", out == want, f"got {out}")


def test_activity_before_spawn_floors_at_last_event() -> None:
    print()
    print("Test: cap never precedes the last targetability event")
    # Inert prop: spawn at 100s, no activity at all (-inf sentinel).
    out = actor_targetable_intervals([_ev(100_000, 1, 1)], 0, 200_000,
                                     spawn_tail_last_activity_s=float("-inf"))
    want = [(100.0, 100.0 + SILENT_DESPAWN_TAIL_GRACE_S)]
    _check("inert prop capped at spawn+grace", out == want, f"got {out}")


def test_activity_past_fight_end_clamps() -> None:
    print()
    print("Test: cap clamps at fight end (final boss burned to the kill)")
    out = actor_targetable_intervals([_ev(150_000, 1, 1)], 0, 200_000,
                                     spawn_tail_last_activity_s=198.0)
    _check("tail reaches fight end", out == [(150.0, 200.0)], f"got {out}")


# --- enemy_last_activity ------------------------------------------------------

def test_enemy_last_activity_merges_sources() -> None:
    print()
    print("Test: enemy_last_activity = max(own casts, player damage landed)")
    casts = [
        {"timestamp": 30_000, "sourceID": 1},
        {"timestamp": 80_000, "sourceID": 1},
        {"timestamp": 90_000, "sourceID": 99},   # not an enemy — ignored
    ]
    dmg = [
        {"timestamp": 85_000, "targetID": 1},
        {"timestamp": 40_000, "targetID": 2},    # cast-less burn target
    ]
    out = enemy_last_activity(casts, dmg, 0, {1, 2})
    _check("merged per-actor max", out == {1: 85.0, 2: 40.0}, f"got {out}")


# --- compute_downtime_windows: the Dancing Mad relay --------------------------

def _dancing_mad_events() -> list[dict]:
    """The observed Dancing Mad signature, scaled down: boss A present from
    pull and closed explicitly; boss B spawns and silently despawns; bosses
    C+D overlap and close explicitly; boss E spawns and holds to the kill."""
    return [
        _ev(100_000, 1, 0),   # A untargetable at 100s (was up 0-100)
        _ev(110_000, 2, 1),   # B spawns 110s — never closes (silent despawn)
        _ev(200_000, 3, 1),   # C spawns 200s
        _ev(200_000, 4, 1),   # D spawns 200s
        _ev(300_000, 3, 0),   # C leaves 300s
        _ev(305_000, 4, 0),   # D leaves 305s
        _ev(330_000, 5, 1),   # E spawns 330s, holds to kill (400s)
    ]


def test_relay_without_activity_reproduces_masked_result() -> None:
    print()
    print("Test: relay WITHOUT activity => B's phantom tail masks everything")
    win = compute_downtime_windows(_dancing_mad_events(), 0, 400_000,
                                   {1, 2, 3, 4, 5}, {1, 2, 3, 4, 5})
    _check("only the A->B handoff visible",
           win == [(100.0, 110.0)], f"got {win}")


def test_relay_with_activity_recovers_transitions() -> None:
    print()
    print("Test: relay WITH activity => B capped, transitions recovered")
    # B last acts at 170s; E is burned to the kill.
    activity = {1: 99.0, 2: 170.0, 3: 299.0, 4: 304.0, 5: 399.0}
    win = compute_downtime_windows(_dancing_mad_events(), 0, 400_000,
                                   {1, 2, 3, 4, 5}, {1, 2, 3, 4, 5},
                                   last_activity_s=activity)
    g = SILENT_DESPAWN_TAIL_GRACE_S
    want = [(100.0, 110.0), (170.0 + g, 200.0), (305.0, 330.0)]
    _check("transition windows recovered", win == want, f"got {win}")


# --- fetch_tier_a_windows: evidence fetch failure disables the cap ------------

class _FakeClient:
    """Targetability succeeds; enemy-casts fetch optionally raises."""

    def __init__(self, activity_ok: bool):
        self._activity_ok = activity_ok

    def get_targetability_events(self, code, start, end):
        return _dancing_mad_events()

    def get_enemy_cast_events(self, code, start, end):
        if not self._activity_ok:
            raise RuntimeError("boom")
        return [{"timestamp": 170_000, "sourceID": 2},
                {"timestamp": 399_000, "sourceID": 5}]

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        return []


def _fixture_fight() -> tuple[dict, dict]:
    report = {"masterData": {"actors": [
        {"id": i, "subType": "Boss"} for i in (1, 2, 3, 4, 5)
    ]}}
    fight = {"startTime": 0, "endTime": 400_000,
             "enemyNPCs": [{"id": i} for i in (1, 2, 3, 4, 5)],
             "friendlyPlayers": [7]}
    return report, fight


def test_fetch_gates_on_evidence() -> None:
    print()
    print("Test: fetch_tier_a_windows — evidence failure => historic windows")
    report, fight = _fixture_fight()
    win, fetched = fetch_tier_a_windows(_FakeClient(activity_ok=False),
                                        "R", report, fight, actor={"id": 7})
    _check("fetched flag still true", fetched is True)
    _check("cap disabled on evidence failure",
           win == [(100.0, 110.0)], f"got {win}")

    win2, fetched2 = fetch_tier_a_windows(_FakeClient(activity_ok=True),
                                          "R", report, fight, actor={"id": 7})
    g = SILENT_DESPAWN_TAIL_GRACE_S
    # Actors 1/3/4 close explicitly (no cap needed); 2 caps at 170+g; 5 acts
    # to 399 so its tail clamps at fight end.
    want = [(100.0, 110.0), (170.0 + g, 200.0), (305.0, 330.0)]
    _check("cap active with evidence", win2 == want, f"got {win2}")


def main() -> int:
    for fn in [
        test_spawn_tail_capped_by_activity,
        test_spawn_tail_without_activity_unchanged,
        test_present_at_pull_tail_never_capped,
        test_reopened_tail_is_spawn_opened,
        test_activity_before_spawn_floors_at_last_event,
        test_activity_past_fight_end_clamps,
        test_enemy_last_activity_merges_sources,
        test_relay_without_activity_reproduces_masked_result,
        test_relay_with_activity_recovers_transitions,
        test_fetch_gates_on_evidence,
    ]:
        fn()
    print()
    print(f"{len(_PASSED)} passed, {len(_FAILED)} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
