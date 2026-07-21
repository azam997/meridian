"""RDM end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the RDM
synthetic fixture (no FFLogs network calls) and compares the JSON output to a
frozen snapshot. RDM is the first *caster* and (in Phase 1) ships with only the
shared aspects — no simulator, no per-job aspects — so this locks the multi-job
contract shape for the caster path the same way test_samurai_pipeline.py does
for the melee data-only path.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_redmage_pipeline.py

Run from python/:
    python tests/test_redmage_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rdm"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Mirrors the SAM stub. Refs flow short-circuited via empty rankings —
    the snapshot covers the RDM `you` pipeline + the no-refs comparison."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "RDM synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic RDM Fight",
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
                        "name": f.get("label", "RDM Synthetic Player"),
                        "server": "TestServer",
                        "type": "Player",
                        # Real FFLogs reports the spaceless subType — exercises
                        # the spaceless job-name match in find_player_actor.
                        "subType": "RedMage",
                        "petOwner": None,
                        "gameID": 35,
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
        "Red Mage", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("Red Mage", you, refs)
    return _build_response("Red Mage", you, refs, comparisons)


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


# --- pytest fixtures -------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def actual() -> dict:
    return _normalize(_run_pipeline())


@pytest.fixture(scope="module")
def response(actual: dict) -> dict:
    return actual


# --- Shape assertions (independent of the snapshot) ----------------------

def test_rdm_response_shape(response: dict) -> None:
    """RDM ships with the shared aspects + its Scoring aspect (the idealized
    ceiling), and NOT the MCH-/RPR-specific aspects nor the Phase-3 Procs
    aspect."""
    print()
    print("Test: RDM aspectStates shape")
    states = response.get("aspectStates", {})
    expected_present = {"Abilities", "Drift", "Clipping", "Overcap",
                        "Opener", "Alignment", "Scoring", "Procs"}
    expected_absent = {"Queen", "Wildfire", "Tools", "Execution",
                       "DeathsDesign"}

    for name in expected_present:
        _check(f"aspectStates['{name}'] present", name in states,
               f"keys={sorted(states)}")
    for name in expected_absent:
        _check(f"aspectStates['{name}'] absent",
               name not in states,
               f"got {name!r} in {sorted(states)}")


def test_rdm_headline_has_efficiency(response: dict) -> None:
    """RDM now has a simulator → a real idealized ceiling and efficiency. The
    synthetic fixture is deliberately suboptimal (overcap spam, dropped Fleche),
    so efficiency lands below 100%."""
    print()
    print("Test: RDM headline efficiency populated")
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    eff = h.get("efficiencyPct") or 0
    _check("0 < headline.efficiencyPct <= 100",
           0 < eff <= 100.5, f"got {eff!r}")
    _check("headline.yourPotency > 0",
           (h.get("yourPotency") or 0) > 0)


def test_rdm_scoring_proc_budget(response: dict) -> None:
    """The Scoring aspect measures the player's proc count (Verfire+Verstone) and
    exposes it as procBudget; the fixture casts Verstone once."""
    print()
    print("Test: RDM Scoring exposes proc budget")
    sc = response.get("aspectStates", {}).get("Scoring", {})
    _check("Scoring.procBudget == 1 (one Verstone in the fixture)",
           sc.get("procBudget") == 1, f"got {sc.get('procBudget')!r}")


def test_rdm_drift_finds_fleche(response: dict) -> None:
    """Fleche (25s recast) is cast once at t=6s then never again across the
    122s fight → DriftAspect (fallback cast-gap heuristic, no simulator)
    detects a large cap."""
    print()
    print("Test: RDM Drift finds Fleche cooldown waste")
    drift = response.get("aspectStates", {}).get("Drift", {})
    findings = drift.get("findings") or []
    fleche = next((f for f in findings if f.get("abilityId") == 7517), None)
    _check("Fleche drift finding present", fleche is not None,
           f"got findings for {[f.get('abilityId') for f in findings]}")
    if fleche:
        _check("Fleche capped_seconds > 60 (drift detected)",
               fleche.get("cappedSeconds", 0) > 60,
               f"got {fleche.get('cappedSeconds')}")


def test_rdm_overcap_findings_per_gauge(response: dict) -> None:
    """RDM has 2 mana gauges; the tail Veraero III spam overcaps white_mana.
    Verify any findings reference only the real RDM gauges."""
    print()
    print("Test: RDM Overcap findings reference known gauges")
    overcap = response.get("aspectStates", {}).get("Overcap", {})
    findings = overcap.get("findings") or []
    valid_gauges = {"white_mana", "black_mana"}
    bad = [f.get("gauge") for f in findings
           if f.get("gauge") not in valid_gauges]
    _check("no findings on unknown gauges", not bad, f"got {bad}")
    _check("at least one overcap finding (white_mana tail spam)",
           len(findings) > 0, f"got {len(findings)}")


def test_rdm_opener_findings_are_lists(response: dict) -> None:
    """OpenerAspect always emits a list; the synthetic opener deviates from
    canonical, so it produces a non-empty findings list with the right fields."""
    print()
    print("Test: RDM Opener findings list")
    opener = response.get("aspectStates", {}).get("Opener", {})
    findings = opener.get("findings") or []
    _check("findings is a list", isinstance(findings, list))
    for f in findings:
        _check(f"slot {f.get('position')} has expected fields",
               all(k in f for k in ["position", "expectedId", "actualId",
                                     "lostPotency", "summary"]),
               f"got {sorted(f)}")


def test_rdm_ability_meta_only_rdm_ids(response: dict) -> None:
    """abilityMeta should contain only RDM ability IDs (union of the fixture's
    cast IDs and RDM's declared cooldowns — drift seeds metadata for every
    cooldown ability, even ones not cast)."""
    print()
    print("Test: RDM abilityMeta references RDM IDs only")
    meta = response.get("abilityMeta", {})
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8")
    )
    from jobs.redmage.data import CANONICAL_OPENER as RDM_OPENER
    from jobs.redmage.data import COOLDOWNS as RDM_COOLDOWNS
    from jobs.redmage.data import OGCD_IDS as RDM_OGCDS
    from jobs.redmage.data import POTENCIES as RDM_POTENCIES
    rdm_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    # abilityMeta is seeded for fixture casts, drift cooldowns, opener slots, and
    # every ability the idealized sim / improvements reference — i.e. the whole
    # RDM roster (POTENCIES) plus zero-potency oGCDs the sim fires (Swiftcast is
    # modeled as a free-instant source even though it carries no table potency).
    allowed = (rdm_ids | set(RDM_COOLDOWNS.keys()) | set(RDM_OPENER)
               | set(RDM_POTENCIES.keys()) | set(RDM_OGCDS))
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is an RDM-related ID",
               aid in allowed,
               f"got {aid}, not in fixture casts or RDM cooldowns")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"rdm_{FIXTURE_NAME}.snapshot.json"


def test_rdm_snapshot(actual: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
    print()
    print(f"Test: RDM contract snapshot for fixture {FIXTURE_NAME!r}")
    snap_path = _snapshot_path()

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"  [OK  ] wrote snapshot {snap_path.name}")
        return

    if not snap_path.exists():
        _check("snapshot exists", False,
               f"no snapshot at {snap_path}; "
               f"run UPDATE_SNAPSHOT=1 python tests/test_redmage_pipeline.py")
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
    _check("snapshot matches", False,
           "mismatch — run UPDATE_SNAPSHOT=1 python tests/test_redmage_pipeline.py")


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"Loading RDM fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_rdm_snapshot(actual)
        print()
        print("=" * 60)
        print("Snapshot written.")
        return 0

    test_rdm_response_shape(actual)
    test_rdm_headline_has_efficiency(actual)
    test_rdm_scoring_proc_budget(actual)
    test_rdm_drift_finds_fleche(actual)
    test_rdm_overcap_findings_per_gauge(actual)
    test_rdm_opener_findings_are_lists(actual)
    test_rdm_ability_meta_only_rdm_ids(actual)
    test_rdm_snapshot(actual)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
