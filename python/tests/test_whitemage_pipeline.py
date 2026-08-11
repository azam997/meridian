"""WHM end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the WHM
synthetic fixture (no FFLogs network calls) and compares the JSON output to a
frozen snapshot. WHM is the first *healer*; this locks the multi-job contract
shape for the healer path the same way test_samurai_pipeline.py /
test_redmage_pipeline.py do for melee / caster.

The fixture is the deterministic output of
scripts/gen_whm_synthetic_fixture.py (pre-pull Glare channel, PoM haste
cadence + Glare IVs, ~30 s Dia refreshes, the lily->Misery economy, and a
deliberate one-cast Assize drift).

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_whitemage_pipeline.py

Run from python/:
    python tests/test_whitemage_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "whm"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Mirrors the SAM/RDM stubs. Refs flow short-circuited via empty rankings
    — the snapshot covers the WHM `you` pipeline + the no-refs comparison."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "WHM synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic WHM Fight",
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
                        "name": f.get("label", "WHM Synthetic Player"),
                        "server": "TestServer",
                        "type": "Player",
                        # Real FFLogs reports the spaceless subType — exercises
                        # the spaceless job-name match in find_player_actor.
                        "subType": "WhiteMage",
                        "petOwner": None,
                        "gameID": 24,
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
        "White Mage", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("White Mage", you, refs)
    return _build_response("White Mage", you, refs, comparisons)


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

def test_whm_response_shape(response: dict) -> None:
    """WHM ships with only the shared aspects — confirm the response has them
    and none of the other jobs' bespoke aspects."""
    print()
    print("Test: WHM aspectStates shape")
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


def test_whm_headline_has_efficiency(response: dict) -> None:
    """WHM ships a full simulator → a real idealized ceiling + efficiency."""
    print()
    print("Test: WHM headline carries a real efficiency")
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    _check("headline.efficiencyPct > 0",
           (h.get("efficiencyPct") or 0) > 0,
           f"got {h.get('efficiencyPct')!r}")
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_whm_drift_finds_assize(response: dict) -> None:
    """The fixture casts Assize once (~5.4 s) and never again across 360 s —
    DriftAspect must flag the 40 s cooldown sitting capped, and at ~310 s of
    cap (~7 whole recasts) the quantized lost potency is > 0."""
    print()
    print("Test: WHM Drift finds the Assize cooldown waste")
    drift = response.get("aspectStates", {}).get("Drift", {})
    findings = drift.get("findings") or []
    assize = next((f for f in findings if f.get("abilityId") == 3571), None)
    _check("Assize drift finding present", assize is not None,
           f"got findings for {[f.get('abilityId') for f in findings]}")
    if assize:
        _check("Assize capped_seconds > 200",
               assize.get("cappedSeconds", 0) > 200,
               f"got {assize.get('cappedSeconds')}")
        _check("Assize lost_potency > 0 (whole casts lost)",
               assize.get("lostPotency", 0) > 0,
               f"got {assize.get('lostPotency')}")


def test_whm_opener_findings_are_lists(response: dict) -> None:
    print()
    print("Test: WHM Opener findings list")
    opener = response.get("aspectStates", {}).get("Opener", {})
    findings = opener.get("findings") or []
    _check("findings is a list", isinstance(findings, list))
    for f in findings:
        _check(f"slot {f.get('position')} has expected fields",
               all(k in f for k in ["position", "expectedId", "actualId",
                                    "lostPotency", "summary"]),
               f"got {sorted(f)}")


def test_whm_ability_meta_only_whm_ids(response: dict) -> None:
    """abilityMeta should reference only WHM-related ids (fixture casts ∪ the
    WHM data tables — the idealized track can add abilities never cast)."""
    print()
    print("Test: WHM abilityMeta references WHM IDs only")
    meta = response.get("abilityMeta", {})
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8")
    )
    from jobs.whitemage.data import (COOLDOWNS as WHM_COOLDOWNS, DEFENSIVE_IDS,
                                     POTENCIES as WHM_POTENCIES)
    whm_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (whm_ids | set(WHM_COOLDOWNS.keys()) | set(WHM_POTENCIES.keys())
               | set(DEFENSIVE_IDS))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a WHM-related ID",
               aid in allowed,
               f"got {aid}, not in fixture casts or WHM tables")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"whm_{FIXTURE_NAME}.snapshot.json"


def test_whm_snapshot(actual: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
    print()
    print(f"Test: WHM contract snapshot for fixture {FIXTURE_NAME!r}")
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
               f"run UPDATE_SNAPSHOT=1 python tests/test_whitemage_pipeline.py")
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
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_whitemage_pipeline.py")
    _FAILED.append(("snapshot matches", "mismatch"))
    raise AssertionError("snapshot mismatch")


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"Loading WHM fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_whm_snapshot(actual)
        print()
        print("=" * 60)
        print("Snapshot written.")
        return 0

    test_whm_response_shape(actual)
    test_whm_headline_has_efficiency(actual)
    test_whm_drift_finds_assize(actual)
    test_whm_opener_findings_are_lists(actual)
    test_whm_ability_meta_only_whm_ids(actual)
    test_whm_snapshot(actual)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
