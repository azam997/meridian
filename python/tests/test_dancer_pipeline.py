"""DNC end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the DNC
synthetic fixture (no FFLogs network calls) and compares the JSON output to a
frozen snapshot. Locks the contract shape for the Dancer job — incl. the Procs
aspect and the budgeted sim_context.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_dancer_pipeline.py

Run from python/:
    python tests/test_dancer_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dnc"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Serves the synthetic DNC fixture. Refs flow is short-circuited via empty
    rankings; aura events are empty (budgets are measured from the cast stream)."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "DNC synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic DNC Fight",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": [f["source_id"]],
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"],
                    "name": f.get("label", "DNC Synthetic Player"),
                    "server": "TestServer",
                    "type": "Player",
                    "subType": "Dancer",
                    "petOwner": None,
                    "gameID": 38,
                }],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict]:
        if data_type != "Casts":
            return []
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return []

    def get_rankings(self, *args: Any, **kwargs: Any) -> dict:
        return {"rankings": []}


# --- Test harness ----------------------------------------------------------

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


def _run_pipeline() -> dict:
    fixture = json.loads((FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    stub = StubFFLogsClient(fixture)

    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(
        "Dancer", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("Dancer", you, refs)
    return _build_response("Dancer", you, refs, comparisons)


def _normalize(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, dict):
        return {str(k) if isinstance(k, int) else k: _normalize(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


import pytest  # noqa: E402


@pytest.fixture(scope="module")
def actual() -> dict:
    return _normalize(_run_pipeline())


@pytest.fixture(scope="module")
def response(actual: dict) -> dict:
    return actual


# --- Shape assertions ------------------------------------------------------

def test_dnc_response_shape(response: dict) -> None:
    states = response.get("aspectStates", {})
    expected_present = {"Abilities", "Drift", "Clipping", "Opener", "Alignment",
                        "Procs"}
    expected_absent = {"Queen", "Wildfire", "Tools", "Execution"}
    for name in expected_present:
        _check(f"aspectStates['{name}'] present", name in states,
               f"keys={sorted(states)}")
    for name in expected_absent:
        _check(f"aspectStates['{name}'] absent", name not in states,
               f"got {name!r}")


def test_dnc_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0)
    _check("headline.efficiencyPct > 0", (h.get("efficiencyPct") or 0) > 0)
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_dnc_efficiency_within_guard(response: dict) -> None:
    """The synthetic delivered stream (default sim) must not exceed its own
    idealized ceiling (perfect sim) — the <=100% guard the whole design rests on."""
    h = response.get("headline", {})
    eff = h.get("efficiencyPct") or 0
    _check("efficiencyPct <= 100.5", eff <= 100.5, f"got {eff}")


def test_dnc_ability_meta_only_dnc_ids(response: dict) -> None:
    meta = response.get("abilityMeta", {})
    from jobs.dancer.data import (COOLDOWNS, DEFENSIVE_IDS, POTENCIES, STEP_IDS)
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    dnc_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (dnc_ids | set(COOLDOWNS) | set(POTENCIES) | set(DEFENSIVE_IDS)
               | set(STEP_IDS))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a DNC id", aid in allowed,
               f"got {aid}, not a known DNC id")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"dnc_{FIXTURE_NAME}.snapshot.json"


def test_dnc_snapshot(actual: dict) -> None:
    snap_path = _snapshot_path()
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
        print(f"  [OK  ] wrote snapshot {snap_path.name}")
        return
    if not snap_path.exists():
        _check("snapshot exists", False,
               f"no snapshot; run UPDATE_SNAPSHOT=1 python tests/test_dancer_pipeline.py")
        return
    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    actual_text = json.dumps(actual, indent=2, sort_keys=True, default=str)
    expected_text = json.dumps(expected, indent=2, sort_keys=True)
    if actual_text == expected_text:
        _check("snapshot matches", True)
        return
    print(f"  [FAIL] snapshot mismatch ({len(actual_text)} vs {len(expected_text)} bytes)")
    mismatches = 0
    for k in sorted(set(actual.keys()) | set(expected.keys())):
        a_str = json.dumps(actual.get(k), sort_keys=True, default=str)
        e_str = json.dumps(expected.get(k), sort_keys=True)
        if a_str != e_str:
            mismatches += 1
            print(f"         '{k}': drift "
                  f"(actual {len(a_str)} vs expected {len(e_str)} chars)")
            if mismatches >= 3:
                print("         ...")
                break
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_dancer_pipeline.py")
    _FAILED.append(("snapshot matches", "mismatch"))
    raise AssertionError("snapshot mismatch")


def main() -> int:
    print(f"Loading DNC fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_dnc_snapshot(actual)
        print("Snapshot written.")
        return 0
    test_dnc_response_shape(actual)
    test_dnc_headline_has_efficiency(actual)
    test_dnc_efficiency_within_guard(actual)
    test_dnc_ability_meta_only_dnc_ids(actual)
    test_dnc_snapshot(actual)
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
