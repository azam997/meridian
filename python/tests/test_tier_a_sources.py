"""Unit tests for Tier-A downtime sources (no network).

Exercises:
  - pair_targetability_events: synthetic 0/1 flip events -> windows.
  - resolve_boss_actor_ids: per-fight intersection of bosses ∩ enemyNPCs.
  - fetch_tier_a_windows: end-to-end with a stubbed client that returns
    Vamp-Fatale-shaped events; confirms the 50.9s window is recovered.
  - CachedEventsClient caches targetability calls.

Run from python/:  python tests/test_tier_a_sources.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.cached_client import CachedEventsClient
from jobs._core.downtime_sources import (
    actor_targetable_intervals,
    fetch_tier_a_windows,
    resolve_boss_actor_ids,
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
        "abilityGameID": 0,
        "fight": 1,
        "targetable": targetable,
    }


# --- actor_targetable_intervals (spawn-aware reconstruction) ----------------

def test_actor_intervals_no_events_targetable_throughout() -> None:
    print()
    print("Test: actor_targetable_intervals — no events => [0, end]")
    out = actor_targetable_intervals([], 0, 10_000)
    _check("targetable whole fight", out == [(0.0, 10.0)], f"got {out}")


def test_actor_intervals_leading_zero_present_from_pull() -> None:
    print()
    print("Test: actor_targetable_intervals — leading 0 => present from pull")
    evs = [_ev(4_000, 1, 0), _ev(7_000, 1, 1)]
    out = actor_targetable_intervals(evs, 0, 10_000)
    _check("targetable [0,4] and [7,10]",
           out == [(0.0, 4.0), (7.0, 10.0)], f"got {out}")


def test_actor_intervals_leading_one_is_spawn() -> None:
    """A lone leading `1` means the enemy spawned then — it was NOT
    targetable from t=0 (the Coffinmaker case)."""
    print()
    print("Test: actor_targetable_intervals — leading 1 => spawn")
    out = actor_targetable_intervals([_ev(6_000, 1, 1)], 0, 10_000)
    _check("targetable [6,10] only", out == [(6.0, 10.0)], f"got {out}")


def test_actor_intervals_trailing_untargetable_has_no_tail() -> None:
    """An enemy that goes untargetable and never returns (dies/despawns)
    is targetable only up to the `0`; there is no trailing interval."""
    print()
    print("Test: actor_targetable_intervals — trailing 0 => no tail interval")
    out = actor_targetable_intervals([_ev(8_000, 1, 0)], 0, 10_000)
    _check("targetable [0,8] only", out == [(0.0, 8.0)], f"got {out}")


def test_actor_intervals_redundant_zero_doesnt_split() -> None:
    """A second `0` while already untargetable must not reopen/split an
    interval."""
    print()
    print("Test: actor_targetable_intervals — redundant 0 ignored")
    evs = [_ev(1_000, 1, 0), _ev(2_000, 1, 0), _ev(5_000, 1, 1)]
    out = actor_targetable_intervals(evs, 0, 10_000)
    _check("targetable [0,1] and [5,10]",
           out == [(0.0, 1.0), (5.0, 10.0)], f"got {out}")


# --- resolve_boss_actor_ids -----------------------------------------------

def test_resolve_boss_intersection() -> None:
    print()
    print("Test: resolve_boss_actor_ids - masterData INTERSECT enemyNPCs")
    report_summary = {
        "masterData": {
            "actors": [
                {"id": 28, "subType": "Boss"},
                {"id": 87, "subType": "Boss"},   # different fight
                {"id": 92, "subType": "Boss"},   # different fight
                {"id": 31, "subType": "NPC"},
                {"id": -1, "subType": "NPC"},
            ],
        },
    }
    fight = {
        "enemyNPCs": [
            {"id": 28}, {"id": 31}, {"id": 40},
        ],
    }
    out = resolve_boss_actor_ids(report_summary, fight)
    _check("only actor 28 (Boss in masterData AND in enemyNPCs)",
           out == {28}, f"got {out}")


def test_resolve_handles_missing_fields() -> None:
    print()
    print("Test: resolve_boss_actor_ids — missing fields don't crash")
    out = resolve_boss_actor_ids({}, {})
    _check("empty -> empty set", out == set(), f"got {out}")


# --- fetch_tier_a_windows --------------------------------------------------

class _StubClient:
    def __init__(self, events: list[dict]):
        self._events = events
        self.calls = 0

    def get_targetability_events(self, code, start, end):
        self.calls += 1
        return [e for e in self._events
                if start <= e["timestamp"] <= end]


def test_fetch_tier_a_vamp_fatale_shape() -> None:
    """Smoke test using the actual event shape recovered from the live
    probe against Vamp Fatale: actor 28 (boss) untargetable for 50.9s."""
    print()
    print("Test: fetch_tier_a_windows — Vamp-Fatale-shaped events")
    fight_start_ms = 2_097_823
    fight_end_ms   = 2_617_723
    # The probe recovered exactly these two events for the boss.
    untargetable_ms = fight_start_ms + 68_500
    targetable_ms   = fight_start_ms + 119_400
    events = [
        _ev(untargetable_ms, 28, 0),
        _ev(targetable_ms,   28, 1),
        # Add an "add" actor going targetable/untargetable that should be
        # ignored because it isn't in the boss list.
        _ev(fight_start_ms + 30_000, 33, 1),
        _ev(fight_start_ms + 60_000, 33, 0),
    ]
    report_summary = {
        "masterData": {
            "actors": [
                {"id": 28, "subType": "Boss"},
                {"id": 33, "subType": "NPC"},
                {"id": 99, "subType": "Boss"},   # not in this fight
            ],
        },
    }
    fight = {
        "startTime": fight_start_ms,
        "endTime":   fight_end_ms,
        "enemyNPCs": [{"id": 28}, {"id": 33}, {"id": 40}],
    }
    client = _StubClient(events)
    windows, fetched = fetch_tier_a_windows(client, "abc", report_summary, fight)
    _check("fetched flag True", fetched is True)
    _check("one window", len(windows) == 1, f"got {len(windows)}")
    if windows:
        s, e = windows[0]
        _check("window starts at ~68.5s",
               abs(s - 68.5) < 0.05, f"got {s}")
        _check("window ends at ~119.4s",
               abs(e - 119.4) < 0.05, f"got {e}")
        _check("duration ~50.9s",
               abs((e - s) - 50.9) < 0.1, f"got {e - s}")


def test_fetch_tier_a_no_events_means_confirmed_targetable() -> None:
    """An empty response is NOT a missing-data signal — it's a confirmed
    'boss never went untargetable'. The fetched flag stays True."""
    print()
    print("Test: fetch_tier_a_windows — empty events => confirmed targetable")
    report_summary = {
        "masterData": {"actors": [{"id": 28, "subType": "Boss"}]},
    }
    fight = {"startTime": 0, "endTime": 600_000,
             "enemyNPCs": [{"id": 28}]}
    client = _StubClient([])
    windows, fetched = fetch_tier_a_windows(client, "abc", report_summary, fight)
    _check("empty windows", windows == [], f"got {windows}")
    _check("fetched flag still True (confirmed signal)",
           fetched is True)


def test_fetch_tier_a_client_failure() -> None:
    """If the client raises, fetched=False so the caller can fall through
    to Tier C."""
    print()
    print("Test: fetch_tier_a_windows — client failure surfaces fetched=False")

    class _Boom:
        def get_targetability_events(self, *a, **kw):
            raise RuntimeError("network")

    report_summary = {
        "masterData": {"actors": [{"id": 28, "subType": "Boss"}]},
    }
    fight = {"startTime": 0, "endTime": 600_000,
             "enemyNPCs": [{"id": 28}]}
    windows, fetched = fetch_tier_a_windows(_Boom(), "abc", report_summary, fight)
    _check("fetched flag False", fetched is False)
    _check("windows empty", windows == [])


def test_fetch_tier_a_add_untargetable_while_boss_up() -> None:
    """An add going untargetable while the boss is still up must NOT
    produce downtime — the player still has the boss to hit. The boss
    here has no events, so it's targetable for the whole fight and covers
    the add's gap."""
    print()
    print("Test: fetch_tier_a_windows — add gap masked by always-up boss")
    fight_start = 0
    fight_end = 100_000
    events = [
        _ev(20_000, 50, 0),     # add goes untargetable
        _ev(60_000, 50, 1),
    ]
    report_summary = {
        "masterData": {"actors": [
            {"id": 28, "subType": "Boss"},
            {"id": 50, "subType": "NPC"},
        ]},
    }
    fight = {"startTime": fight_start, "endTime": fight_end,
             "enemyNPCs": [{"id": 28}, {"id": 50}]}
    windows, fetched = fetch_tier_a_windows(
        _StubClient(events), "abc", report_summary, fight,
    )
    _check("no downtime (boss always up)", windows == [], f"got {windows}")
    _check("fetched True", fetched is True)


def test_fetch_tier_a_add_covers_boss_gap() -> None:
    """The M9S Vamp Fatale case: the boss goes untargetable and an add
    spawns (lone leading `1`) to cover the gap. True downtime is only the
    sliver between the boss leaving and the add appearing — not the whole
    boss-untargetable window."""
    print()
    print("Test: fetch_tier_a_windows — add covers boss gap (Coffinmaker)")
    fight_start = 0
    fight_end = 200_000
    events = [
        _ev(68_000, 28, 0),       # boss leaves at 68s
        _ev(119_000, 28, 1),      # boss returns at 119s
        _ev(68_300, 40, 1),       # add SPAWNS targetable at 68.3s (lone 1)
    ]
    report_summary = {
        "masterData": {"actors": [
            {"id": 28, "subType": "Boss"},
            {"id": 40, "subType": "NPC"},
        ]},
    }
    fight = {"startTime": fight_start, "endTime": fight_end,
             "enemyNPCs": [{"id": 28}, {"id": 40}]}
    windows, fetched = fetch_tier_a_windows(
        _StubClient(events), "abc", report_summary, fight,
    )
    _check("one sliver window", len(windows) == 1, f"got {windows}")
    if windows:
        s, e = windows[0]
        _check("downtime only 68.0s -> 68.3s",
               abs(s - 68.0) < 0.01 and abs(e - 68.3) < 0.01,
               f"got ({s}, {e})")


# --- CachedEventsClient ----------------------------------------------------

def test_cached_targetability() -> None:
    print()
    print("Test: CachedEventsClient — get_targetability_events caches")

    class _Counter:
        def __init__(self):
            self.calls = 0

        def get_targetability_events(self, code, start, end):
            self.calls += 1
            return [{"timestamp": start, "type": "targetabilityupdate",
                     "targetID": 1, "targetable": 0}]

    inner = _Counter()
    cached = CachedEventsClient(inner)
    for _ in range(5):
        cached.get_targetability_events("rpt", 0, 10000)
    _check("5 identical calls -> 1 inner fetch",
           inner.calls == 1, f"got {inner.calls}")

    # Different range should hit the inner client again.
    cached.get_targetability_events("rpt", 0, 20000)
    _check("differing key -> additional inner fetch",
           inner.calls == 2, f"got {inner.calls}")

    # Mutation of returned list doesn't poison cache.
    out = cached.get_targetability_events("rpt", 0, 10000)
    out.append({"poison": True})
    fresh = cached.get_targetability_events("rpt", 0, 10000)
    _check("mutating returned list doesn't poison cache",
           all("poison" not in e for e in fresh))


# --- runner ----------------------------------------------------------------

def main() -> int:
    test_actor_intervals_no_events_targetable_throughout()
    test_actor_intervals_leading_zero_present_from_pull()
    test_actor_intervals_leading_one_is_spawn()
    test_actor_intervals_trailing_untargetable_has_no_tail()
    test_actor_intervals_redundant_zero_doesnt_split()
    test_resolve_boss_intersection()
    test_resolve_handles_missing_fields()
    test_fetch_tier_a_vamp_fatale_shape()
    test_fetch_tier_a_no_events_means_confirmed_targetable()
    test_fetch_tier_a_client_failure()
    test_fetch_tier_a_add_untargetable_while_boss_up()
    test_fetch_tier_a_add_covers_boss_gap()
    test_cached_targetability()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
