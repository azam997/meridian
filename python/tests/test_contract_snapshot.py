"""Contract snapshot test.

Drives the sidecar's `_build_response` end-to-end against a recorded
fixture (no FFLogs network calls) and compares the JSON output to a
frozen snapshot. Catches accidental contract drift between the Python
sidecar and the React frontend's contract.ts types.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_contract_snapshot.py

Run from project root:
    python tests/test_contract_snapshot.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"

# The fixture we snapshot against. topq_1 is the top-quartile MCH pull —
# representative of a "real" run that exercises every aspect.
FIXTURE_NAME = "topq_1"


# --- Stub client ------------------------------------------------------------

class StubFFLogsClient:
    """Synthetic FFLogs client backed by one cast-event fixture. Returns
    canned data for the methods `analyze_pull` calls on Machinist; no
    network, no creds required.

    Refs flow is short-circuited via empty `get_rankings` — the snapshot
    covers the `you` pipeline + the no-refs comparison code path."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        # NPC actors and the fight's enemyNPCs come from the fixture when
        # present (post-Tier-A regen); fall back to a single boss stub for
        # legacy fixtures so the Tier-A path still parses cleanly.
        npc_actors = f.get("master_npc_actors") or [
            {"id": 9001, "name": "TestBoss", "type": "NPC",
             "subType": "Boss", "petOwner": None, "gameID": 9001},
        ]
        enemy_npcs = f.get("enemy_npcs") or [{"id": 9001, "gameID": 9001,
                                                "petOwner": None}]
        fa = f.get("friendly_actors") or []
        other_players = [{
            "id": a["id"], "name": a.get("name"), "server": "TestServer",
            "type": "Player", "subType": a.get("subType"),
            "petOwner": None, "gameID": 0,
        } for a in fa if a["id"] != f["source_id"]]
        friendly_ids = [f["source_id"]] + [a["id"] for a in other_players]
        return {
            "title": f.get("label", "Snapshot fixture"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Snapshot Fight",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": friendly_ids,
                "enemyNPCs": enemy_npcs,
            }],
            "masterData": {
                "actors": [
                    {
                        "id": f["source_id"],
                        "name": f.get("label", "Snapshot Player"),
                        "server": "TestServer",
                        "type": "Player",
                        "subType": "Machinist",
                        "petOwner": None,
                        "gameID": 31,
                    },
                    *other_players,
                    *npc_actors,
                ],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict]:
        # Fixture pre-includes pre-pull casts; filter by time range.
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code: str, start: int,
                                   end: int) -> list[dict]:
        evs = self._fixture.get("targetability_events") or []
        return [e for e in evs if start <= e.get("timestamp", 0) <= end]

    def get_rankings(self, *args: Any, **kwargs: Any) -> dict:
        return {"rankings": []}


# --- Snapshot test ---------------------------------------------------------

def _build_response_for_fixture(name: str) -> dict:
    """Drive the sidecar pipeline against a fixture without subprocess /
    NDJSON overhead. Compares the same `_build_response` dict the
    frontend would receive over the wire."""
    fixture = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    stub = StubFFLogsClient(fixture)

    # Import the sidecar internals we drive. Imported here (not at module
    # top) so the test file itself doesn't pull in the analyzer when
    # collected.
    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(
        "Machinist", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []   # snapshot is no-refs; covers the empty-refs branch
    comparisons = _compare_all_aspects("Machinist", you, refs)
    return _build_response("Machinist", you, refs, comparisons)


def _normalize(obj: Any) -> Any:
    """Round floats to 2 decimal places so simulator nondeterminism in
    the last bits doesn't churn the snapshot. Stringify int dict keys so
    they survive the json.dumps/loads round-trip (JSON keys are always
    strings; without this, `actual` has int keys while `expected` has
    str keys after deserialization)."""
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, dict):
        return {str(k) if isinstance(k, int) else k: _normalize(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


def main() -> int:
    print()
    print(f"Test: contract snapshot for fixture {FIXTURE_NAME!r}")

    actual = _normalize(_build_response_for_fixture(FIXTURE_NAME))

    snapshot_path = SNAPSHOTS_DIR / f"run_analysis_{FIXTURE_NAME}.snapshot.json"

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"  [OK  ] wrote snapshot {snapshot_path.relative_to(Path.cwd())}")
        print(f"         {sum(1 for _ in snapshot_path.read_text().splitlines())} lines")
        return 0

    if not snapshot_path.exists():
        print(f"  [FAIL] no snapshot at {snapshot_path}")
        print(f"         run: UPDATE_SNAPSHOT=1 python tests/test_contract_snapshot.py")
        return 1

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    # Round-trip actual through json for deterministic comparison.
    actual_text = json.dumps(actual, indent=2, sort_keys=True, default=str)
    expected_text = json.dumps(expected, indent=2, sort_keys=True)

    if actual_text == expected_text:
        print(f"  [OK  ] snapshot matches")
        # Quick shape stats so failures are easier to diagnose.
        top = sorted(actual.keys())
        print(f"         top-level keys: {top}")
        print(f"         {len(actual.get('comparisons', {}))} comparisons, "
              f"{len(actual.get('aspectStates', {}))} aspect states, "
              f"{len(actual.get('abilityMeta', {}))} abilities in meta")
        print()
        print("============================================================")
        print("Passed: 1    Failed: 0")
        return 0

    # Diff: find the first 3 mismatching keys at depth 1 for actionable output.
    print(f"  [FAIL] snapshot mismatch ({len(actual_text)} vs {len(expected_text)} bytes)")
    mismatches = 0
    for k in sorted(set(actual.keys()) | set(expected.keys())):
        a_str = json.dumps(actual.get(k), sort_keys=True, default=str)
        e_str = json.dumps(expected.get(k), sort_keys=True)
        if a_str != e_str:
            mismatches += 1
            print(f"         '{k}': drift "
                  f"(actual {len(a_str)} chars vs expected {len(e_str)} chars)")
            if mismatches >= 3:
                print(f"         ...")
                break
    print(f"         run: UPDATE_SNAPSHOT=1 python tests/test_contract_snapshot.py")
    print()
    print("============================================================")
    print("Passed: 0    Failed: 1")
    return 1


def test_contract_snapshot_matches() -> None:
    """pytest entry: end-to-end _build_response vs frozen snapshot."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
