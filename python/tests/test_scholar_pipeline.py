"""SCH end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the SCH synthetic
fixture (no FFLogs network calls) and compares the JSON output to a frozen snapshot.
SCH is the third *healer*; this locks the multi-job contract shape for the healer
path the same way test_astrologian_pipeline.py does.

The fixture is the deterministic output of scripts/gen_scholar_synthetic_fixture.py
(default-sim rotation with a deliberate one-cast Chain Stratagem drift).

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_scholar_pipeline.py

Run from python/:
    python tests/test_scholar_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sch"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Mirrors the AST stub. Refs flow short-circuited via empty rankings — the
    snapshot covers the SCH `you` pipeline + the no-refs comparison."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "SCH synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic SCH Fight",
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
                        "name": f.get("label", "SCH Synthetic Player"),
                        "server": "TestServer",
                        # Real FFLogs reports the spaceless subType.
                        "type": "Player",
                        "subType": "Scholar",
                        "petOwner": None,
                        "gameID": 28,
                    },
                ],
            },
        }

    def get_events(self, code: str, start: int, end: int, source_id: int,
                   data_type: str = "Casts", ability_id: int | None = None) -> list[dict]:
        return [e for e in self._events if start <= e.get("timestamp", 0) <= end]

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


# --- Pipeline driver ------------------------------------------------------

def _run_pipeline() -> dict:
    fixture = json.loads((FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    stub = StubFFLogsClient(fixture)

    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(
        "Scholar", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("Scholar", you, refs)
    return _build_response("Scholar", you, refs, comparisons)


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


import pytest  # noqa: E402


@pytest.fixture(scope="module")
def actual() -> dict:
    return _normalize(_run_pipeline())


@pytest.fixture(scope="module")
def response(actual: dict) -> dict:
    return actual


# --- Shape assertions (independent of the snapshot) ----------------------

def test_sch_response_shape(response: dict) -> None:
    """SCH ships with only the shared aspects — confirm the response has them and
    none of the other jobs' bespoke aspects."""
    print()
    print("Test: SCH aspectStates shape")
    states = response.get("aspectStates", {})
    expected_present = {"Abilities", "Drift", "Clipping", "Overcap",
                        "Opener", "Alignment"}
    expected_absent = {"Queen", "Wildfire", "Tools", "Execution", "Procs"}

    for name in expected_present:
        _check(f"aspectStates['{name}'] present", name in states,
               f"keys={sorted(states)}")
    for name in expected_absent:
        _check(f"aspectStates['{name}'] absent (other-job-only)",
               name not in states,
               f"got {name!r} in {sorted(states)}")


def test_sch_headline_has_efficiency(response: dict) -> None:
    """SCH ships a full simulator → a real idealized ceiling + efficiency."""
    print()
    print("Test: SCH headline carries a real efficiency")
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    _check("headline.efficiencyPct > 0",
           (h.get("efficiencyPct") or 0) > 0,
           f"got {h.get('efficiencyPct')!r}")
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_sch_drift_finds_chain_stratagem(response: dict) -> None:
    """The fixture casts Chain Stratagem once and never again across 360s —
    DriftAspect must flag the 120s cooldown sitting capped, with quantized lost
    potency > 0."""
    print()
    print("Test: SCH Drift finds the Chain Stratagem cooldown waste")
    drift = response.get("aspectStates", {}).get("Drift", {})
    findings = drift.get("findings") or []
    cs = next((f for f in findings if f.get("abilityId") == 7436), None)
    _check("Chain Stratagem drift finding present", cs is not None,
           f"got findings for {[f.get('abilityId') for f in findings]}")
    if cs:
        _check("Chain Stratagem capped_seconds > 120",
               cs.get("cappedSeconds", 0) > 120,
               f"got {cs.get('cappedSeconds')}")


def test_sch_ability_meta_only_sch_ids(response: dict) -> None:
    """abilityMeta should reference only SCH-related ids (fixture casts ∪ the SCH
    data tables — the idealized track can add abilities never cast)."""
    print()
    print("Test: SCH abilityMeta references SCH IDs only")
    meta = response.get("abilityMeta", {})
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8")
    )
    from jobs.scholar.data import (COOLDOWNS as SCH_COOLDOWNS, DEFENSIVE_IDS,
                                   POTENCIES as SCH_POTENCIES)
    sch_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (sch_ids | set(SCH_COOLDOWNS.keys()) | set(SCH_POTENCIES.keys())
               | set(DEFENSIVE_IDS))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a SCH-related ID",
               aid in allowed,
               f"got {aid}, not in fixture casts or SCH tables")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"sch_{FIXTURE_NAME}.snapshot.json"


def test_sch_snapshot(actual: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
    print()
    print(f"Test: SCH contract snapshot for fixture {FIXTURE_NAME!r}")
    snap_path = _snapshot_path()

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"  [OK  ] wrote snapshot {snap_path.relative_to(Path.cwd())}")
        return

    if not snap_path.exists():
        _check("snapshot exists", False,
               f"no snapshot at {snap_path}; "
               f"run UPDATE_SNAPSHOT=1 python tests/test_scholar_pipeline.py")
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
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_scholar_pipeline.py")
    _FAILED.append(("snapshot matches", "mismatch"))
    raise AssertionError("snapshot mismatch")


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"Loading SCH fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_sch_snapshot(actual)
        print()
        print("=" * 60)
        print("Snapshot written.")
        return 0

    test_sch_response_shape(actual)
    test_sch_headline_has_efficiency(actual)
    test_sch_drift_finds_chain_stratagem(actual)
    test_sch_ability_meta_only_sch_ids(actual)
    test_sch_snapshot(actual)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
