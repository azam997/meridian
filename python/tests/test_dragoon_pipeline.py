"""DRG end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against a synthetic
DRG pull (the default-sim timeline replayed as FFLogs cast events — no network,
no fixture file needed) and compares the JSON output to a frozen snapshot, the
same way test_gunbreaker_pipeline.py does for GNB. Complements
test_dragoon_sim.py and test_dragoon_pulls.py by locking the actual wire shape.

Also pins the v1.1 audit fixes: Nastrond gated on an ACTIVE Life of the Dragon
window (a refine hold on Geirskogul could previously emit an illegal
out-of-window Nastrond), and the Geirskogul drift price resized to the
Dawntrail 1-Nastrond window (the 3200 figure was Endwalker's 3-per-window math).

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_dragoon_pipeline.py

Run from python/:  python tests/test_dragoon_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.dragoon import data as dd

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.dragoon.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-DRG pull; refs flow short-circuited via empty
    rankings. Targetability/aura/damage streams are empty (boss targetable
    throughout → zero downtime; the positional bonus byte degrades to
    assume-hit)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "DRG pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic DRG Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "DRG Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "Dragoon", "petOwner": None, "gameID": 22},
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
    you = analyze_pull("Dragoon", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Dragoon", you, refs)
    return _build_response("Dragoon", you, refs, comparisons)


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


def test_drg_response_shape(response: dict) -> None:
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"
    for name in ("Queen", "Wildfire", "Tools"):
        assert name not in states, f"MCH-only aspect {name} leaked into DRG"


def test_drg_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


def test_drg_nastrond_gated_on_lotd() -> None:
    """Nastrond requires an ACTIVE Life of the Dragon: a stale nastrond_ready
    with lotd lapsed must not pick Nastrond."""
    from jobs.dragoon.simulator import DragoonRotationModel, NASTROND
    m = DragoonRotationModel()
    params = next(iter(m.sweep_params(())))
    st = m.init_state()
    st.t = 100.0
    # Park every higher-priority oGCD so the LotD chain is the live decision.
    for k in list(st.cd_ready):
        st.cd_ready[k] = 999.0
    st.charges = {k: 0.0 for k in st.charges}
    st.nastrond_ready = 1
    st.lotd_end = 90.0            # lapsed
    assert m.pick_ogcd(st, params) != NASTROND
    st.lotd_end = 110.0           # active
    assert m.pick_ogcd(st, params) == NASTROND


def test_drg_geirskogul_price_is_dt_sized() -> None:
    """The drift price reflects the Dawntrail 1-Nastrond window, not
    Endwalker's 3-per-window 3200."""
    assert dd.COOLDOWN_VALUE_P[dd.GEIRSKOGUL] == 1760
    assert dd.NASTROND_PER_LOTD == 1


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "drg_synthetic.snapshot.json"


def test_drg_snapshot(response: dict) -> None:
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
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_dragoon_pipeline.py"
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
    test_drg_response_shape(resp)
    test_drg_headline_has_efficiency(resp)
    test_drg_nastrond_gated_on_lotd()
    test_drg_geirskogul_price_is_dt_sized()
    test_drg_snapshot(resp)
    print("all dragoon pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
