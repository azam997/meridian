"""RPR end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against a synthetic
RPR pull (the default-sim timeline replayed as FFLogs cast events — no network,
no fixture file needed) and compares the JSON output to a frozen snapshot, the
same way test_gunbreaker_pipeline.py does for GNB. Complements
test_reaper_sim.py and test_reaper_pulls.py by locking the actual wire shape.

Also pins the v1.1 audit fixes: defensive_ids present (Arcane Crest / Hell's
Ingress family — RPR was the only job without any), the Soul Scythe shared
charge pool (no phantom second pool in COOLDOWNS), the deleted no-op
harvest_moon_priority_high sweep axis, and the positional table without the
Void/Cross Reaping alternation-bonus mislabel.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_reaper_pipeline.py

Run from python/:  python tests/test_reaper_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.reaper import data as rd

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.reaper.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-RPR pull; refs flow short-circuited via empty
    rankings. Targetability/aura/damage streams are empty (boss targetable
    throughout → zero downtime; Death's Design coverage degrades gracefully)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "RPR pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic RPR Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "RPR Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "Reaper", "petOwner": None, "gameID": 39},
                    {"id": _BOSS_ID, "name": "Boss", "server": "TestServer",
                     "type": "NPC", "subType": "Boss", "petOwner": None,
                     "gameID": 1},
                ],
                "abilities": [],
            },
        }

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._casts if start <= e["timestamp"] <= end]

    def get_targetability_events(self, code, start, end):
        return []

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return []

    def get_rankings(self, *args: Any, **kwargs: Any) -> dict:
        return {"rankings": []}


def _run_pipeline() -> dict:
    from jobs import analyze_pull
    from sidecar.main import _build_response, _compare_all_aspects

    stub = StubFFLogsClient(_synthetic_casts())
    you = analyze_pull("Reaper", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Reaper", you, refs)
    return _build_response("Reaper", you, refs, comparisons)


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
def response() -> dict:
    return _normalize(_run_pipeline())


# --- Shape assertions (independent of the snapshot) --------------------------


def test_rpr_response_shape(response: dict) -> None:
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"
    for name in ("Queen", "Wildfire", "Tools"):
        assert name not in states, f"MCH-only aspect {name} leaked into RPR"


def test_rpr_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


def test_rpr_defensive_ids_present() -> None:
    """RPR was the only job with NO defensive_ids — Arcane Crest / the Hell's
    movement family landed on the DPS lane and entered the missed-cast diff."""
    assert rd.JOB_DATA.defensive_ids, "defensive_ids empty"
    for aid in (rd.ARCANE_CREST, rd.HELLS_INGRESS, rd.HELLS_EGRESS, rd.REGRESS):
        assert aid in rd.JOB_DATA.defensive_ids


def test_rpr_soul_scythe_shares_soul_slice_pool() -> None:
    """One 2-charge pool, two buttons: Soul Scythe maps onto Soul Slice via
    charge_sharing and must NOT have its own phantom COOLDOWNS pool."""
    assert rd.JOB_DATA.charge_sharing.get(rd.SOUL_SCYTHE) == rd.SOUL_SLICE
    assert rd.SOUL_SCYTHE not in rd.COOLDOWNS


def test_rpr_positional_table_has_no_alternation_ids() -> None:
    """Void/Cross Reaping have no positional — their 580/640 delta is the
    Enhanced ALTERNATION bonus (modeled in the simulator), so they must not be
    priced as positional misses."""
    assert rd.VOID_REAPING not in rd.POSITIONAL_IDS
    assert rd.CROSS_REAPING not in rd.POSITIONAL_IDS
    for aid in (rd.GIBBET, rd.GALLOWS, rd.EXEC_GIBBET, rd.EXEC_GALLOWS):
        assert aid in rd.POSITIONAL_IDS


def test_rpr_sweep_has_no_dead_axis() -> None:
    """The no-op harvest_moon_priority_high axis is gone: 2 weave x 2 harpe
    = 4 sweep points (was 8 for identical coverage)."""
    from jobs.reaper.simulator import ReaperRotationModel, SimParams
    points = list(ReaperRotationModel().sweep_params(()))
    assert len(points) == 4, f"expected 4 sweep points, got {len(points)}"
    assert not hasattr(SimParams(), "harvest_moon_priority_high")


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "rpr_synthetic.snapshot.json"


def test_rpr_snapshot(response: dict) -> None:
    """Lock the camelized JSON response shape against a frozen snapshot."""
    snap_path = _snapshot_path()

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(response, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"wrote snapshot {snap_path}")
        return

    assert snap_path.exists(), \
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_reaper_pipeline.py"
    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    actual_text = json.dumps(response, indent=2, sort_keys=True, default=str)
    expected_text = json.dumps(expected, indent=2, sort_keys=True)
    if actual_text != expected_text:
        drifted = [k for k in sorted(set(response) | set(expected))
                   if json.dumps(response.get(k), sort_keys=True, default=str)
                   != json.dumps(expected.get(k), sort_keys=True)]
        raise AssertionError(
            f"snapshot mismatch; drifted top-level keys: {drifted}. "
            f"If intentional, regenerate with UPDATE_SNAPSHOT=1.")


def main() -> int:
    resp = _normalize(_run_pipeline())
    test_rpr_response_shape(resp)
    test_rpr_headline_has_efficiency(resp)
    test_rpr_defensive_ids_present()
    test_rpr_soul_scythe_shares_soul_slice_pool()
    test_rpr_positional_table_has_no_alternation_ids()
    test_rpr_sweep_has_no_dead_axis()
    test_rpr_snapshot(resp)
    print("all reaper pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
