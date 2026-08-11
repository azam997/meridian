"""NIN end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the NIN synthetic
fixture (no FFLogs network calls) and compares the JSON output to a frozen snapshot.
Complements `test_ninja_sim.py` (internal invariants) and `test_ninja_pulls.py`
(real pulls) by exercising the actual data pipeline end-to-end and locking the
contract shape.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_ninja_pipeline.py

Run from python/:
    python tests/test_ninja_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "nin"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Serves the NIN synthetic fixture. Refs flow is short-circuited via empty
    rankings — the snapshot covers the NIN `you` pipeline + the no-refs branches."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "NIN synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic NIN Fight",
                "encounterID": 101,
                "difficulty": 101,
                "kill": True,
                "startTime": f["fight_start_ms"],
                "endTime": f["fight_end_ms"],
                "friendlyPlayers": [f["source_id"]],
            }],
            "masterData": {
                "actors": [
                    {
                        "id": f["source_id"],
                        "name": f.get("label", "NIN Synthetic Player"),
                        "server": "TestServer",
                        "type": "Player",
                        "subType": "Ninja",
                        "petOwner": None,
                        "gameID": 30,
                    },
                ],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict]:
        if data_type != "Casts":
            return []
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

    def get_targetability_events(self, code, start, end):
        return []

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
    """Drive analyze_pull + _build_response against the NIN fixture without
    subprocess / NDJSON overhead. Returns the same dict the frontend receives."""
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    stub = StubFFLogsClient(fixture)

    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(
        "Ninja", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("Ninja", you, refs)
    return _build_response("Ninja", you, refs, comparisons)


def _normalize(obj: Any) -> Any:
    """Round floats to 2 decimal places for deterministic comparison."""
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


# --- Shape assertions (independent of the snapshot) ----------------------

def test_nin_response_shape(response: dict) -> None:
    """NIN ships with only the shared aspects — confirm the response has
    Abilities/Drift/Clipping/Overcap/Opener/Alignment and NOT the MCH-specific ones."""
    states = response.get("aspectStates", {})
    for name in {"Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment"}:
        _check(f"aspectStates['{name}'] present", name in states,
               f"keys={sorted(states)}")
    for name in {"Queen", "Wildfire", "Tools", "Execution"}:
        _check(f"aspectStates['{name}'] absent (MCH-only)", name not in states,
               f"got {name!r} in {sorted(states)}")


def test_nin_headline_has_efficiency(response: dict) -> None:
    """NIN ships a full simulator → a real idealized ceiling + efficiency."""
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    _check("headline.efficiencyPct > 0", (h.get("efficiencyPct") or 0) > 0,
           f"got {h.get('efficiencyPct')!r}")
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_nin_overcap_findings_known_gauges(response: dict) -> None:
    """NIN has 2 overcap gauges (ninki, kazematoi); any findings reference only
    those."""
    overcap = response.get("aspectStates", {}).get("Overcap", {})
    findings = overcap.get("findings") or []
    valid = {"ninki", "kazematoi"}
    bad = [f.get("gauge") for f in findings if f.get("gauge") not in valid]
    _check("no findings on unknown gauges", not bad, f"got {bad}")


def test_nin_ability_meta_only_nin_ids(response: dict) -> None:
    """abilityMeta should reference only NIN-related ids (fixture casts ∪ the NIN
    potency / cooldown / defensive tables) — no leftover ids from another job."""
    meta = response.get("abilityMeta", {})
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    from jobs.ninja.data import COOLDOWNS, JOB_DATA, POTENCIES
    cast_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (cast_ids | set(POTENCIES) | set(COOLDOWNS)
               | set(JOB_DATA.defensive_ids))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a NIN-related id", aid in allowed,
               f"got {aid}, not a NIN ability")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"nin_{FIXTURE_NAME}.snapshot.json"


def test_nin_snapshot(actual: dict) -> None:
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
               f"no snapshot at {snap_path}; run "
               f"UPDATE_SNAPSHOT=1 python tests/test_ninja_pipeline.py")
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
                  f"(actual {len(a_str)} chars vs expected {len(e_str)} chars)")
            if mismatches >= 3:
                print("         ...")
                break
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_ninja_pipeline.py")
    raise AssertionError("snapshot mismatch")


def main() -> int:
    print(f"Loading NIN fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_nin_snapshot(actual)
        print("Snapshot written.")
        return 0
    test_nin_response_shape(actual)
    test_nin_headline_has_efficiency(actual)
    test_nin_overcap_findings_known_gauges(actual)
    test_nin_ability_meta_only_nin_ids(actual)
    test_nin_snapshot(actual)
    print(f"\nPassed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
