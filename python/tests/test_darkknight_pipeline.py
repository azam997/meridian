"""DRK end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against a synthetic
DRK pull (the default-sim timeline replayed as FFLogs cast events — no network,
no fixture file needed) and compares the JSON output to a frozen snapshot.
Complements test_darkknight_sim.py (sim invariants) and test_darkknight_pulls.py
(real-pull calibration gates) by locking the actual wire shape, the same way
test_summoner_pipeline.py does for SMN.

Also asserts the bundled ability metadata end-to-end: every abilityMeta entry
must carry a real name + the OGCD_IDS oGCD flag under the hermetic stub — the
regression this guards is DRK ids silently resolving to None (which blanks the
Clipping aspect and the GCD-speed inference).

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_darkknight_pipeline.py

Run from python/:  python tests/test_darkknight_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.darkknight import data as dd

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.darkknight.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-DRK pull; refs flow short-circuited via empty
    rankings. Targetability/aura streams are empty (boss targetable throughout →
    zero downtime, deterministic 'targetability' source). No pet actors — the
    Esteem fold rides the Living Shadow cast id, so nothing else is fetched."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "DRK pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic DRK Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "DRK Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "DarkKnight", "petOwner": None, "gameID": 42},
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
    you = analyze_pull("Dark Knight", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Dark Knight", you, refs)
    return _build_response("Dark Knight", you, refs, comparisons)


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


def test_drk_response_shape(response: dict) -> None:
    """DRK ships only the shared aspects — no MCH-specific ones."""
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"
    for name in ("Queen", "Wildfire", "Tools"):
        assert name not in states, f"MCH-only aspect {name} leaked into DRK"


def test_drk_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


def test_drk_ability_meta_resolves_hermetically(response: dict) -> None:
    """Every abilityMeta entry carries a real bundled name + the OGCD_IDS flag —
    the end-to-end proof the DRK block in ability_metadata.BUNDLED is wired."""
    meta = response.get("abilityMeta", {})
    assert meta, "abilityMeta empty"
    for aid_str, m in meta.items():
        aid = int(aid_str)
        if aid <= 0:
            continue   # the in-sim tincture marker never reaches abilityMeta
        assert m.get("name"), f"abilityMeta[{aid}] has no name (not bundled?)"
        if aid in dd.POTENCIES or aid in dd.OGCD_IDS or aid in dd.DEFENSIVE_IDS:
            assert m.get("isOgcd") == (aid in dd.OGCD_IDS), \
                f"{m.get('name')} ({aid}): isOgcd mismatch vs OGCD_IDS"
    # Spot-check two load-bearing ids by name.
    assert meta.get(str(dd.LIVING_SHADOW), {}).get("name") == "Living Shadow"
    assert meta.get(str(dd.HARD_SLASH), {}).get("name") == "Hard Slash"


def test_drk_defensives_flagged(response: dict) -> None:
    """Tank defensives arrive isDefensive=true on the wire (the Defensives lane
    + the DPS-diff filter both hang off this one flag). The synthetic timeline
    carries no defensive casts, so assert via the ids that ARE present plus the
    shared role-action union logic: every DEFENSIVE_IDS member that made it into
    abilityMeta must be flagged."""
    meta = response.get("abilityMeta", {})
    for aid_str, m in meta.items():
        aid = int(aid_str)
        if aid in dd.DEFENSIVE_IDS:
            assert m.get("isDefensive"), f"{m.get('name')} ({aid}) not isDefensive"


def test_drk_clipping_active(response: dict) -> None:
    """The Clipping aspect must actually WALK the casts — it silently skips any
    cast whose ability metadata is None, which would blank the whole aspect if
    the DRK BUNDLED block were missing."""
    finding = response.get("aspectStates", {}).get("Clipping", {}).get("clipping")
    assert finding, "Clipping state empty"
    assert finding.get("avgGcdPotency", 350.0) != 350.0, \
        "avgGcdPotency at the metadata-blanked default — DRK ids not resolving"


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "drk_synthetic.snapshot.json"


def test_drk_snapshot(response: dict) -> None:
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
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_darkknight_pipeline.py"
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
    test_drk_response_shape(resp)
    test_drk_headline_has_efficiency(resp)
    test_drk_ability_meta_resolves_hermetically(resp)
    test_drk_defensives_flagged(resp)
    test_drk_clipping_active(resp)
    test_drk_snapshot(resp)
    print("all darkknight pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
