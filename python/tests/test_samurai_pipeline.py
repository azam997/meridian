"""SAM end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against the
SAM synthetic fixture (no FFLogs network calls) and compares the JSON
output to a frozen snapshot. Complements `test_samurai_smoke.py` (which
only checks registration wiring) by exercising the actual data pipeline
end-to-end for the second job, locking the contract shape for any job
that ships without per-job aspects.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_samurai_pipeline.py

Run from python/:
    python tests/test_samurai_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sam"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURE_NAME = "synthetic"


# --- Stub client -----------------------------------------------------------

class StubFFLogsClient:
    """Mirrors the MCH stub in test_contract_snapshot.py. Refs flow is
    short-circuited via empty rankings — the snapshot covers the SAM
    `you` pipeline + the no-refs comparison branches."""

    def __init__(self, fixture: dict):
        self._fixture = fixture
        self._events = fixture["cast_events"]

    def get_report_summary(self, code: str) -> dict:
        f = self._fixture
        return {
            "title": f.get("label", "SAM synthetic"),
            "startTime": f["fight_start_ms"],
            "endTime": f["fight_end_ms"],
            "fights": [{
                "id": f["fight_id"],
                "name": "Synthetic SAM Fight",
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
                        "name": f.get("label", "SAM Synthetic Player"),
                        "server": "TestServer",
                        "type": "Player",
                        "subType": "Samurai",
                        "petOwner": None,
                        "gameID": 34,
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
    """Drive analyze_pull + _build_response against the SAM fixture
    without subprocess / NDJSON overhead. Returns the same dict the
    frontend would receive over the wire."""
    fixture = json.loads((FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8"))
    stub = StubFFLogsClient(fixture)

    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    you = analyze_pull(
        "Samurai", stub, fixture["report_code"], fixture["fight_id"],
        ranking_name=None, label="You",
    )
    refs: list = []
    comparisons = _compare_all_aspects("Samurai", you, refs)
    return _build_response("Samurai", you, refs, comparisons)


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
# The test functions below were written to receive the normalized response
# (main() passes `_normalize(_run_pipeline())` to every one, whether the
# parameter is named `response` or `actual`). Expose it as a module-scoped
# fixture so the pipeline runs once for the whole file under pytest.

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def actual() -> dict:
    return _normalize(_run_pipeline())


@pytest.fixture(scope="module")
def response(actual: dict) -> dict:
    return actual


# --- Shape assertions (independent of the snapshot) ----------------------

def test_sam_response_shape(response: dict) -> None:
    """SAM ships with only the shared aspects — confirm the response has
    Drift/Clipping/Overcap/Opener/Alignment and NOT the MCH-specific
    aspects (Queen/Wildfire/Tools/Execution)."""
    print()
    print("Test: SAM aspectStates shape")
    states = response.get("aspectStates", {})
    expected_present = {"Abilities", "Drift", "Clipping", "Overcap",
                        "Opener", "Alignment"}
    expected_absent = {"Queen", "Wildfire", "Tools", "Execution"}

    for name in expected_present:
        _check(f"aspectStates['{name}'] present", name in states,
               f"keys={sorted(states)}")
    for name in expected_absent:
        _check(f"aspectStates['{name}'] absent (MCH-only)",
               name not in states,
               f"got {name!r} in {sorted(states)}")


def test_sam_headline_has_efficiency(response: dict) -> None:
    """SAM now ships a full simulator → a real idealized ceiling + efficiency."""
    print()
    print("Test: SAM headline carries a real efficiency")
    h = response.get("headline", {})
    _check("headline.yourIdealizedPotency > 0",
           (h.get("yourIdealizedPotency") or 0) > 0,
           f"got {h.get('yourIdealizedPotency')!r}")
    _check("headline.efficiencyPct > 0",
           (h.get("efficiencyPct") or 0) > 0,
           f"got {h.get('efficiencyPct')!r}")
    _check("headline.yourPotency > 0", (h.get("yourPotency") or 0) > 0)


def test_sam_drift_finds_ikishoten(response: dict) -> None:
    """SAM has no simulator, so Drift uses the fallback cast-gap heuristic.
    Ikishoten (120s recast) sits ready ~117.5s in this fixture — DriftAspect
    detects the drift (cappedSeconds > 100), but because that's *under* a
    full recast, no WHOLE cast was lost, so the quantized cost is 0p (you
    can't lose a fractional cast). We assert on Ikishoten rather than Senei
    because the starter SAM data table reuses ID 16481 for both Senei and
    Yukikaze (flagged 'needs verification' in samurai/data.py), so Yukikaze
    casts would suppress the Senei drift count."""
    print()
    print("Test: SAM Drift finds Ikishoten cooldown waste (quantized cost)")
    drift = response.get("aspectStates", {}).get("Drift", {})
    findings = drift.get("findings") or []
    # (The starter-data id collision noted here originally is fixed — Senei is
    # 16481, Yukikaze 7480 — but we still assert on Ikishoten: the fixture never
    # casts it, so its full-fight drift is unambiguous.)
    iki_finding = next((f for f in findings if f.get("abilityId") == 16482), None)
    _check("Ikishoten drift finding present", iki_finding is not None,
           f"got findings for {[f.get('abilityId') for f in findings]}")
    if iki_finding:
        _check("Ikishoten capped_seconds > 100 (drift detected)",
               iki_finding.get("cappedSeconds", 0) > 100,
               f"got {iki_finding.get('cappedSeconds')}")
        _check("Ikishoten lost_potency == 0 (sub-recast drift, no whole cast lost)",
               iki_finding.get("lostPotency", -1) == 0,
               f"got {iki_finding.get('lostPotency')}")


def test_sam_overcap_findings_per_gauge(response: dict) -> None:
    """SAM has 2 gauges; both should be reachable, even if only one
    overcaps in this fixture. Verify findings (if any) reference real
    SAM gauges only."""
    print()
    print("Test: SAM Overcap findings reference known gauges")
    overcap = response.get("aspectStates", {}).get("Overcap", {})
    findings = overcap.get("findings") or []
    valid_gauges = {"kenki"}
    bad = [f.get("gauge") for f in findings
           if f.get("gauge") not in valid_gauges]
    _check("no findings on unknown gauges", not bad,
           f"got {bad}")


def test_sam_opener_findings_are_lists(response: dict) -> None:
    """OpenerAspect always emits a list; verify the synthetic fixture's
    opener (which deviates from canonical at slot 6 onward) produces a
    non-empty findings list."""
    print()
    print("Test: SAM Opener findings list")
    opener = response.get("aspectStates", {}).get("Opener", {})
    findings = opener.get("findings") or []
    _check("findings is a list", isinstance(findings, list))
    _check("findings list non-empty (synthetic differs from canonical)",
           len(findings) > 0,
           f"got {len(findings)}")
    for f in findings:
        _check(f"slot {f.get('position')} has expected fields",
               all(k in f for k in ["position", "expectedId", "actualId",
                                     "lostPotency", "summary"]),
               f"got {sorted(f)}")


def test_sam_ability_meta_only_sam_ids(response: dict) -> None:
    """abilityMeta should contain only SAM ability IDs — no leftover MCH
    IDs from a previous job's session. Allowed IDs are the union of the
    fixture's cast IDs and SAM's declared cooldowns (drift findings seed
    metadata for every cooldown ability, even ones not cast)."""
    print()
    print("Test: SAM abilityMeta references SAM IDs only")
    meta = response.get("abilityMeta", {})
    fixture = json.loads(
        (FIXTURE_DIR / f"{FIXTURE_NAME}.json").read_text(encoding="utf-8")
    )
    # With a simulator, abilityMeta also covers the idealized track's abilities
    # (Ogi, Zanshin, …) — so the allowed set is the full SAM ability table, not
    # just the fixture casts. The intent (no leftover non-SAM ids) is preserved.
    from jobs.samurai.data import (COOLDOWNS as SAM_COOLDOWNS, DEFENSIVE_IDS,
                                   MEDITATE, POTENCIES as SAM_POTENCIES)
    sam_ids = {ev["abilityGameID"] for ev in fixture["cast_events"]}
    allowed = (sam_ids | set(SAM_COOLDOWNS.keys()) | set(SAM_POTENCIES.keys())
               | set(DEFENSIVE_IDS) | {MEDITATE})
    for aid_str in meta.keys():
        aid = int(aid_str)
        _check(f"abilityMeta[{aid}] is a SAM-related ID",
               aid in allowed,
               f"got {aid}, not in fixture casts or SAM cooldowns")


# --- Snapshot ---------------------------------------------------------------

def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / f"sam_{FIXTURE_NAME}.snapshot.json"


def test_sam_snapshot(actual: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
    print()
    print(f"Test: SAM contract snapshot for fixture {FIXTURE_NAME!r}")
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
               f"run UPDATE_SNAPSHOT=1 python tests/test_samurai_pipeline.py")
        return

    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    actual_text = json.dumps(actual, indent=2, sort_keys=True, default=str)
    expected_text = json.dumps(expected, indent=2, sort_keys=True)
    if actual_text == expected_text:
        _check("snapshot matches", True)
        return

    # Diff: find the first 3 mismatching keys at depth 1.
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
    print("         run: UPDATE_SNAPSHOT=1 python tests/test_samurai_pipeline.py")
    _FAILED.append(("snapshot matches", "mismatch"))
    raise AssertionError("snapshot mismatch")


# --- Main -------------------------------------------------------------------

def main() -> int:
    print(f"Loading SAM fixture: {FIXTURE_NAME}")
    actual = _normalize(_run_pipeline())

    # If we're updating, just write and exit.
    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        test_sam_snapshot(actual)
        print()
        print("=" * 60)
        print("Snapshot written.")
        return 0

    test_sam_response_shape(actual)
    test_sam_headline_has_efficiency(actual)
    test_sam_drift_finds_ikishoten(actual)
    test_sam_overcap_findings_per_gauge(actual)
    test_sam_opener_findings_are_lists(actual)
    test_sam_ability_meta_only_sam_ids(actual)
    test_sam_snapshot(actual)

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}  {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
