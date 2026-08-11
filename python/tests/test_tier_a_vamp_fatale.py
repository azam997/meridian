"""End-to-end Tier-A regression against the real Vamp Fatale fixture.

The other Tier-A tests use synthetic events; this one uses the actual
events captured from a live FFLogs query (vamp_fatale_topq_1.json).
Locks the recovered window so any future regression in the boss-actor
filtering or pairing logic is caught.

Run from python/:  python tests/test_tier_a_vamp_fatale.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "vamp_fatale_topq_1.json"


class _FixtureClient:
    """Serves report_summary / events / targetability_events directly
    from the fixture. Same shape as the contract-snapshot stub but with
    targetability support."""

    def __init__(self, fix: dict):
        self._fix = fix

    def get_report_summary(self, code):
        f = self._fix
        return {
            "title": f.get("label", ""),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Vamp Fatale",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": [f["source_id"]],
                "enemyNPCs": f.get("enemy_npcs") or [],
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"], "name": "P",
                    "server": "S", "type": "Player",
                    "subType": "Machinist", "petOwner": None,
                    "gameID": 31,
                }, *(f.get("master_npc_actors") or [])],
            },
        }

    def get_events(self, code, start, end, source_id,
                   data_type="Casts", ability_id=None):
        return [e for e in self._fix["cast_events"]
                if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end):
        return [e for e in (self._fix.get("targetability_events") or [])
                if start <= e["timestamp"] <= end]


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


def test_vamp_fatale_addphase_keeps_uptime() -> None:
    """The boss (id 28) goes untargetable t+68.5 -> t+119.4, but the add
    Coffinmaker (id 40) becomes targetable 0.3s later (t+68.8) and holds the
    window open. Under the "downtime = no enemy targetable" model the ~50.9s
    boss absence is NOT downtime — only the ~0.3s gap before the add spawns
    counts. Guards the M9S Vamp Fatale fix (CLAUDE.md, downtime three-tier
    model + memory: downtime-means-no-enemy-targetable).

    (Previously asserted ~50.9s — the pre-fix boss-only expectation. That
    assertion was stale: it contradicted the shipped fix and had been failing.
    Rewritten to lock in the correct add-keeps-uptime behavior.)
    """
    print()
    print("Test: Vamp Fatale add phase keeps uptime (boss-gone is not downtime)")
    if not FIXTURE_PATH.exists():
        print(f"  [SKIP] fixture missing: {FIXTURE_PATH}")
        return
    fix = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    n_tgt = len(fix.get("targetability_events") or [])
    _check(f"fixture carries targetability_events ({n_tgt})", n_tgt >= 2,
           f"got {n_tgt}")

    client = _FixtureClient(fix)
    mr = analyze_pull("Machinist", client, "code", fix["fight_id"],
                       ranking_name=None, label="vamp")
    _check("downtime_source == targetability",
           mr.downtime_source == "targetability",
           f"got {mr.downtime_source!r}")
    total = sum(e - s for s, e in mr.downtime_windows)
    # The 50.9s boss absence must NOT be counted — the add holds uptime.
    # Only the tiny pre-add-spawn gap (~0.3s) is real downtime.
    _check(f"boss absence not counted: total downtime << 50.9s (got {total:.1f}s)",
           total < 5.0,
           f"got {total:.1f}s — add phase mis-flagged as downtime")
    # No single window may span the (false) 50.9s boss-gone interval.
    for s, e in mr.downtime_windows:
        _check(f"no window spans the add phase ({s:.1f}-{e:.1f}s)",
               (e - s) < 5.0,
               f"window {s:.1f}-{e:.1f} covers the add phase")


def test_drift_attribution_uses_window() -> None:
    """Drift's downtime_source should match the resolved source — i.e.
    the targetability windows are flowing into the per-aspect state."""
    print()
    print("Test: Drift aspect sees source=targetability on Vamp Fatale")
    if not FIXTURE_PATH.exists():
        return
    fix = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    client = _FixtureClient(fix)
    mr = analyze_pull("Machinist", client, "code", fix["fight_id"],
                       ranking_name=None, label="vamp")
    drift_state = mr.aspects["Drift"].state
    _check("Drift state.downtime_source == targetability",
           drift_state.get("downtime_source") == "targetability",
           f"got {drift_state.get('downtime_source')!r}")
    _check("Drift state.downtime_windows non-empty",
           len(drift_state.get("downtime_windows") or []) >= 1)


def test_vamp_fatale_multi_target_windows_localized() -> None:
    """Vamp Fatale must NOT read as a whole-fight multi-target pull.

    Its transient adds — Coffinmaker (id 40), the Fatal Flails (46/47), the
    Charnel Cells (79/80/81) — each emit a spawn `targetable=1` but never a
    despawn `0` or a death event, so the pre-fix detector extended every one of
    them to fight end. From the moment the boss returned (~119s) they stacked
    into a single phantom ~400s window reporting up to *7* simultaneous targets,
    mislabeling an essentially single-target fight as a 3+ target fight from ~1
    minute in. Lock the windows to the two genuine add phases (flails ~297-345s,
    charnel ~403-436s) — both bounded, neither starting before the flails and
    neither spanning the rest of the fight.
    """
    print()
    print("Test: Vamp Fatale multi-target windows stay localized to add phases")
    if not FIXTURE_PATH.exists():
        print(f"  [SKIP] fixture missing: {FIXTURE_PATH}")
        return
    from jobs._core.downtime_sources import (
        resolve_boss_actor_ids,
        resolve_enemy_actor_ids,
        simultaneous_targetable_windows,
    )

    fix = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    start, end = fix["fight_start_ms"], fix["fight_end_ms"]
    fight = {"startTime": start, "endTime": end,
             "enemyNPCs": fix.get("enemy_npcs") or []}
    report_summary = {"masterData": {"actors": fix.get("master_npc_actors") or []}}
    wins = simultaneous_targetable_windows(
        fix.get("targetability_events") or [], start, end,
        resolve_enemy_actor_ids(fight),
        resolve_boss_actor_ids(report_summary, fight))

    _check(f"exactly the two add-phase windows (got {len(wins)})",
           len(wins) == 2, f"got {wins}")
    for s, e, n in wins:
        _check(f"window {s:.0f}-{e:.0f}s not a phantom whole-fight span",
               (e - s) < 120.0, f"window {s:.0f}-{e:.0f} spans {e - s:.0f}s")
        _check(f"window {s:.0f}-{e:.0f}s starts no earlier than the flails",
               s >= 290.0, f"window starts at {s:.0f}s (before the flails)")
    # The flails phase is the only one MCH cleaves; it must be present and ~3-up.
    flails = [w for w in wins if 290.0 <= w[0] <= 300.0]
    _check("flails phase detected (~297s, peak 3)",
           len(flails) == 1 and flails[0][2] == 3, f"got {flails}")


def main() -> int:
    test_vamp_fatale_addphase_keeps_uptime()
    test_drift_attribution_uses_window()
    test_vamp_fatale_multi_target_windows_localized()
    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
