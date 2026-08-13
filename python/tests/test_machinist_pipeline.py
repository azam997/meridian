"""MCH end-to-end pipeline + contract snapshot + the Lindwurm ceiling gate.

MCH is the pilot job, yet until v1.1 it was the only job with NO per-job
pipeline lock (it rides the legacy fixture suites — test_execution.py — for
sim/pulls coverage, which stays authoritative for those layers). This file
adds the two missing pieces:

  * the synthetic wire-shape lock (`mch_synthetic.snapshot.json`), the same
    shape every other job pins via its *_pipeline test, and
  * the **Lindwurm II regression gate**: on the real m12s_p2_topq_1 fixture
    the strict ceiling must fit at least as many damaging oGCDs as the real
    player did (Double Check / Checkmate / Wildfire). This is the pinned
    acceptance of the v1.1 engine wave — the "player fit more Checkmates than
    the sim" class of under-fit (weave-gate FP tie + opener CDR overcap +
    gear-vs-argmax context mismatch) stays closed. The ceiling may never fit
    FEWER damaging oGCDs than a real player: an extra damaging oGCD displaces
    no GCD, so it is free potency the ceiling must dominate. Weave capacity
    must NEVER be raised to satisfy this gate (owner directive: 1 weave per
    1.5s slot, 2 per 2.5s; changes require FFLogs clip-proof) — an under-fit
    here means a PLACEMENT/priority bug.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_machinist_pipeline.py

Run from python/:  python tests/test_machinist_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.machinist import data as md

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.machinist.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-MCH pull; refs flow short-circuited via empty
    rankings. Targetability/aura streams are empty (boss targetable throughout →
    zero downtime, deterministic 'targetability' source)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "MCH pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic MCH Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "MCH Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "Machinist", "petOwner": None, "gameID": 31},
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
    you = analyze_pull("Machinist", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Machinist", you, refs)
    return _build_response("Machinist", you, refs, comparisons)


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


def test_mch_response_shape(response: dict) -> None:
    """MCH ships the shared aspects PLUS its bespoke ones."""
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring", "Queen", "Wildfire", "Reassemble"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"


def test_mch_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


# --- The Lindwurm II ceiling gate (real fixture, offline) ---------------------


_WATCH = ("DOUBLE_CHECK", "CHECKMATE", "WILDFIRE")


def test_mch_ceiling_fits_at_least_the_players_ogcds() -> None:
    """On the real M12S-P2 top-quartile fixture the strict ceiling fits >= the
    player's damaging-oGCD counts, with a zero witness gap and bit-identical
    repeat runs. See the module docstring for why this must never be satisfied
    by raising weave capacity."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_execution import _run_pipeline as run_fixture_pipeline
    from jobs.machinist.simulator import (
        CHECKMATE, DOUBLE_CHECK, WILDFIRE, simulate_idealized_perfect,
    )
    ids = {DOUBLE_CHECK: "DoubleCheck", CHECKMATE: "Checkmate",
           WILDFIRE: "Wildfire"}

    fix = json.loads(
        (FIXTURES_DIR / "m12s_p2_topq_1.json").read_text(encoding="utf-8"))
    mr = run_fixture_pipeline(fix)
    st = mr.aspects["Scoring"].state
    assert not st.get("ceiling_witness_gap"), \
        f"witness gap {st.get('ceiling_witness_gap')} — the ceiling under-fits"

    s_ms = fix["fight_start_ms"]
    player = {aid: 0 for aid in ids}
    for ev in fix["cast_events"]:
        aid = ev.get("abilityGameID")
        if ev.get("type") == "cast" and aid in ids \
                and ev.get("timestamp", s_ms) >= s_ms:
            player[aid] += 1

    dur = st["fight_duration_s"]
    downtime = st.get("downtime_windows") or []
    ctx = st.get("sim_context")
    tl1, _ = simulate_idealized_perfect(dur, downtime, None, ctx)
    tl2, _ = simulate_idealized_perfect(dur, downtime, None, ctx)
    assert list(tl1) == list(tl2), "ceiling sim not deterministic"

    ceil = {aid: 0 for aid in ids}
    for t, aid in tl1:
        if aid in ids and t >= 0:
            ceil[aid] += 1
    for aid, name in ids.items():
        assert ceil[aid] >= player[aid], \
            (f"{name}: ceiling fits {ceil[aid]} vs the player's {player[aid]} "
             f"— a damaging-oGCD under-fit (placement bug; do NOT raise weave "
             f"capacity)")


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "mch_synthetic.snapshot.json"


def test_mch_snapshot(response: dict) -> None:
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
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_machinist_pipeline.py"
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
    test_mch_response_shape(resp)
    test_mch_headline_has_efficiency(resp)
    test_mch_ceiling_fits_at_least_the_players_ogcds()
    test_mch_snapshot(resp)
    print("all machinist pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
