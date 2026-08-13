"""PLD end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against a synthetic
PLD pull (the default-sim timeline replayed as FFLogs cast events — no network,
no fixture file needed) and compares the JSON output to a frozen snapshot, the
same way test_gunbreaker_pipeline.py does for GNB. Complements
test_paladin_sim.py (simulator invariants) and test_paladin_pulls.py (real-pull
calibration gates) by locking the actual wire shape.

Also pins the v1.1 combo-break model end-to-end: the Atonement chain and Goring
Blade are weaponskills — cast mid-combo they are legal but RESET the physical
combo (a beam hold-line pays the real cost instead of keeping Riot Blade /
Royal Authority combo'd), the greedy defers Goring Blade to a combo boundary,
and spells (Holy Spirit / Holy Circle / the Confiteor chain) leave the combo
intact.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_paladin_pipeline.py

Run from python/:  python tests/test_paladin_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.paladin import data as pd

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.paladin.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-PLD pull; refs flow short-circuited via empty
    rankings. Targetability/aura streams are empty (boss targetable throughout →
    zero downtime, deterministic 'targetability' source)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "PLD pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic PLD Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "PLD Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "Paladin", "petOwner": None, "gameID": 19},
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
    you = analyze_pull("Paladin", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Paladin", you, refs)
    return _build_response("Paladin", you, refs, comparisons)


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


def test_pld_response_shape(response: dict) -> None:
    """PLD ships only the shared aspects — no MCH-specific ones."""
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"
    for name in ("Queen", "Wildfire", "Tools"):
        assert name not in states, f"MCH-only aspect {name} leaked into PLD"


def test_pld_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


def _model():
    from jobs.paladin.simulator import PaladinRotationModel
    return PaladinRotationModel()


def test_pld_weaponskill_procs_break_combo() -> None:
    """Atonement chain steps and Goring Blade reset the physical combo when
    cast mid-combo; spells (Holy Spirit / Holy Circle) leave it intact."""
    m = _model()
    for aid in (pd.ATONEMENT, pd.SUPPLICATION, pd.SEPULCHRE, pd.GORING_BLADE):
        st = m.init_state()
        st.combo_step = 2
        st.atonement_ready = st.supplication_ready = True
        st.sepulchre_ready = st.goring_ready = True
        m.apply_cast(st, aid)
        assert st.combo_step == 0, f"{aid} did not break the combo"
    for aid in (pd.HOLY_SPIRIT, pd.HOLY_CIRCLE):
        st = m.init_state()
        st.combo_step = 2
        st.divine_might = True
        m.apply_cast(st, aid)
        assert st.combo_step == 2, f"spell {aid} broke the combo"


def test_pld_goring_defers_to_combo_boundary() -> None:
    """Greedy holds Goring Blade Ready until the combo boundary (deferring at
    most 2 GCDs), then fires it — never mid-combo."""
    m = _model()
    params = next(iter(m.sweep_params(())))
    st = m.init_state()
    st.goring_ready = True
    st.combo_step = 1
    assert m.pick_gcd(st, params) != pd.GORING_BLADE
    st.combo_step = 0
    assert m.pick_gcd(st, params) == pd.GORING_BLADE


def test_pld_holy_circle_keeps_burst_fork() -> None:
    """At high target counts the Divine Might spend swaps to Holy Circle — the
    spend-vs-hold fork must survive the swap (it silently vanished before)."""
    from jobs.paladin.simulator import PaladinRotationModel
    m = PaladinRotationModel(mt_schedule=((0.0, 999.0, 5),))
    params = next(iter(m.sweep_params(())))
    st = m.init_state()
    st.divine_might = True
    st.combo_step = 0
    greedy = m.pick_gcd(st, params)
    assert greedy == pd.HOLY_CIRCLE, f"expected Holy Circle at n=5, got {greedy}"
    cands = m.gcd_candidates(st, params)
    assert len(cands) == 2, f"burst-packing fork lost at high N: {cands}"


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "pld_synthetic.snapshot.json"


def test_pld_snapshot(response: dict) -> None:
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
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_paladin_pipeline.py"
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
    test_pld_response_shape(resp)
    test_pld_headline_has_efficiency(resp)
    test_pld_weaponskill_procs_break_combo()
    test_pld_goring_defers_to_combo_boundary()
    test_pld_holy_circle_keeps_burst_fork()
    test_pld_snapshot(resp)
    print("all paladin pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
