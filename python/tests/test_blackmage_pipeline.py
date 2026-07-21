"""BLM end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the BLM
synthetic fixture (no FFLogs network calls) and compares the JSON output to a
frozen snapshot. Mirrors test_samurai_pipeline.py — locks the contract shape for
the second caster (a job that ships with only the shared aspects + Scoring).

Regenerate the fixture with:  python scripts/gen_blm_synthetic_fixture.py
When the contract intentionally changes, regenerate the snapshot with:
    UPDATE_SNAPSHOT=1 python tests/test_blackmage_pipeline.py

Run from python/:  python tests/test_blackmage_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "blm"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


class StubFFLogsClient:
    """Mirrors the SAM stub. Refs flow is short-circuited via empty rankings —
    the snapshot covers the BLM `you` pipeline + the no-refs comparison branches."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "BLM synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"], "name": "Synthetic BLM Fight",
                "encounterID": 101, "difficulty": 101, "kill": True,
                "startTime": f["fight_start_ms"], "endTime": f["fight_end_ms"],
                "friendlyPlayers": [f["source_id"]],
            }],
            "masterData": {
                "actors": [{
                    "id": f["source_id"], "name": f.get("label", "BLM Synthetic"),
                    "server": "TestServer", "type": "Player",
                    "subType": "BlackMage", "petOwner": None, "gameID": 25,
                }],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict]:
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_rankings(self, *args: Any, **kwargs: Any) -> dict:
        return {"rankings": []}


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
    you = analyze_pull("Black Mage", stub, fixture["report_code"], fixture["fight_id"],
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Black Mage", you, refs)
    return _build_response("Black Mage", you, refs, comparisons)


def _normalize(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, dict):
        return {str(k) if isinstance(k, int) else k: _normalize(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


@pytest.fixture(scope="module")
def actual() -> dict:
    return _normalize(_run_pipeline())


@pytest.fixture(scope="module")
def response(actual: dict) -> dict:
    return actual


def test_blm_response_shape(response: dict) -> None:
    """BLM ships with only the shared aspects — confirm the response has
    Drift/Clipping/Overcap/Opener/Alignment and NOT the MCH-specific aspects."""
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment"):
        _check(f"aspectStates['{name}'] present", name in states, f"keys={sorted(states)}")
    for name in ("Queen", "Wildfire", "Tools", "Execution", "Procs"):
        _check(f"aspectStates['{name}'] absent", name not in states,
               f"got {name!r} in {sorted(states)}")


def test_blm_headline_has_efficiency(response: dict) -> None:
    """BLM ships a full simulator → a real idealized ceiling + efficiency."""
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0", (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    _check("headline.efficiencyPct > 0", (h.get("efficiencyPct") or 0) > 0,
           f"got {h.get('efficiencyPct')!r}")
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_blm_overcap_empty(response: dict) -> None:
    """BLM declares no gauges (MP/Enochian live in the sim), so the Overcap aspect
    produces no findings."""
    overcap = response.get("aspectStates", {}).get("Overcap", {})
    findings = overcap.get("findings") or []
    _check("Overcap has no findings (no gauges)", findings == [], f"got {findings}")


def test_blm_opener_findings_are_lists(response: dict) -> None:
    opener = response.get("aspectStates", {}).get("Opener", {})
    findings = opener.get("findings") or []
    _check("findings is a list", isinstance(findings, list))
    for f in findings:
        _check(f"slot {f.get('position')} has expected fields",
               all(k in f for k in ["position", "expectedId", "actualId",
                                    "lostPotency", "summary"]),
               f"got {sorted(f)}")


def test_blm_ability_meta_only_blm_ids(response: dict) -> None:
    """abilityMeta should contain only BLM-related ability IDs (fixture casts ∪
    the BLM data tables ∪ the idealized track's abilities)."""
    meta = response.get("abilityMeta", {})
    fixture = json.loads((FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    from jobs.blackmage.data import (COOLDOWNS, DEFENSIVE_IDS, POTENCIES)
    blm_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (blm_ids | set(COOLDOWNS) | set(POTENCIES) | set(DEFENSIVE_IDS))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a BLM-related ID", aid in allowed,
               f"got {aid}, not in fixture casts or BLM tables")


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"blm_{FIXTURE_NAME}.snapshot.json"


def test_blm_snapshot(actual: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
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
               f"no snapshot at {snap_path}; "
               f"run UPDATE_SNAPSHOT=1 python tests/test_blackmage_pipeline.py")
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
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_blackmage_pipeline.py")
    _FAILED.append(("snapshot matches", "mismatch"))
    raise AssertionError("snapshot mismatch")


def main() -> int:
    print(f"Loading BLM fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_blm_snapshot(actual)
        print("Snapshot written.")
        return 0
    test_blm_response_shape(actual)
    test_blm_headline_has_efficiency(actual)
    test_blm_overcap_empty(actual)
    test_blm_opener_findings_are_lists(actual)
    test_blm_ability_meta_only_blm_ids(actual)
    test_blm_snapshot(actual)
    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
