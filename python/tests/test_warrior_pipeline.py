"""WAR end-to-end pipeline + contract snapshot test.

Drives `analyze_pull` and the sidecar's `_build_response` against a synthetic
WAR pull (the default-sim timeline replayed as FFLogs cast events — no network,
no fixture file needed) and compares the JSON output to a frozen snapshot, the
same way test_gunbreaker_pipeline.py does for GNB. Complements
test_warrior_sim.py (simulator invariants) and test_warrior_pulls.py (real-pull
calibration gates) by locking the actual wire shape.

Also pins the v1.1 Inner Release fix end-to-end: crediting is STACK-counted
(the next 3 Fell Cleave/Decimate casts after each IR, inside the 15s buff),
never a fixed window — the old 8s box dropped the 3rd free cast's crit-DH on
both lenses once Inner Chaos interleaved first.

When the contract intentionally changes, regenerate with:
    UPDATE_SNAPSHOT=1 python tests/test_warrior_pipeline.py

Run from python/:  python tests/test_warrior_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs.warrior import data as wd

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"
_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts() -> list[dict]:
    from jobs.warrior.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class StubFFLogsClient:
    """Serves the synthetic single-WAR pull; refs flow short-circuited via empty
    rankings. Targetability/aura/damage streams are empty (boss targetable
    throughout → zero downtime; Surging Tempest coverage degrades to none,
    which is fine — the snapshot pins the deterministic result)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "WAR pipeline fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Synthetic WAR Fight", "encounterID": 103,
                "difficulty": 101, "kill": True,
                "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "WAR Synthetic Player",
                     "server": "TestServer", "type": "Player",
                     "subType": "Warrior", "petOwner": None, "gameID": 21},
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
    you = analyze_pull("Warrior", stub, "AbCd1234", 1,
                       ranking_name=None, label="You")
    refs: list = []
    comparisons = _compare_all_aspects("Warrior", you, refs)
    return _build_response("Warrior", you, refs, comparisons)


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


def test_war_response_shape(response: dict) -> None:
    """WAR ships the shared aspects + SurgingTempest — no MCH-specific ones."""
    states = response.get("aspectStates", {})
    for name in ("Abilities", "Drift", "Clipping", "Overcap", "Opener",
                 "Alignment", "Scoring", "SurgingTempest"):
        assert name in states, f"aspectStates missing {name}: {sorted(states)}"
    for name in ("Queen", "Wildfire", "Tools"):
        assert name not in states, f"MCH-only aspect {name} leaked into WAR"


def test_war_headline_has_efficiency(response: dict) -> None:
    h = response.get("headline", {})
    assert (h.get("yourIdealizedPotency") or 0) > 0
    assert (h.get("efficiencyPct") or 0) > 0
    assert (h.get("yourPotency") or 0) > 0


def test_war_ir_crediting_is_stack_counted() -> None:
    """Every Inner Release in the idealized line yields exactly 3 credited free
    Fell Cleaves/Decimates (stack semantics), regardless of Inner Chaos
    interleaving — except a fight-tail IR that runs out of casts."""
    from jobs.warrior.scoring import _ir_credited_indices
    from jobs.warrior.simulator import simulate_idealized
    timeline, _ = simulate_idealized(_DURATION_S, [])
    tl = [(t, aid) for t, aid in timeline if t >= 0]
    n_ir = sum(1 for _t, aid in tl if aid == wd.INNER_RELEASE)
    credited = _ir_credited_indices(tl)
    assert n_ir >= 3, f"synthetic line has too few IRs ({n_ir})"
    assert len(credited) >= 3 * (n_ir - 1), \
        f"{len(credited)} credited casts for {n_ir} IRs — stack counting broken"
    assert len(credited) <= 3 * n_ir
    # Each credited cast is a Fell Cleave / Decimate.
    for i in credited:
        assert tl[i][1] in (wd.FELL_CLEAVE, wd.DECIMATE)


def test_war_ir_third_cast_credited_past_old_window() -> None:
    """The regression the fix exists for: a free FC landing >8s after its IR
    (Inner Chaos first, 3 GCDs later) is still credited under stack counting."""
    from jobs.warrior.scoring import _ir_credited_indices
    tl = [
        (0.0, wd.INNER_RELEASE),
        (1.0, wd.INNER_CHAOS),       # does not consume a stack
        (3.5, wd.FELL_CLEAVE),
        (6.0, wd.FELL_CLEAVE),
        (8.5, wd.FELL_CLEAVE),       # past the old 8.0s box — must be credited
        (11.0, wd.FELL_CLEAVE),      # 4th FC: no stack left, not credited
        (16.0, wd.FELL_CLEAVE),      # way later: not credited
    ]
    credited = _ir_credited_indices(tl)
    assert credited == {2, 3, 4}, f"got {sorted(credited)}"


def test_war_ir_buff_expiry_bounds_crediting() -> None:
    """A Fell Cleave after the 15s buff lapses is not credited even with
    stacks nominally unspent."""
    from jobs.warrior.scoring import _ir_credited_indices
    tl = [
        (0.0, wd.INNER_RELEASE),
        (2.0, wd.FELL_CLEAVE),
        (16.0, wd.FELL_CLEAVE),      # buff expired at 15.0
    ]
    credited = _ir_credited_indices(tl)
    assert credited == {1}, f"got {sorted(credited)}"


# --- Snapshot -----------------------------------------------------------------


def _snapshot_path() -> Path:
    return SNAPSHOTS_DIR / "war_synthetic.snapshot.json"


def test_war_snapshot(response: dict) -> None:
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
        f"no snapshot at {snap_path}; run UPDATE_SNAPSHOT=1 python tests/test_warrior_pipeline.py"
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
    test_war_response_shape(resp)
    test_war_headline_has_efficiency(resp)
    test_war_ir_crediting_is_stack_counted()
    test_war_ir_third_cast_credited_past_old_window()
    test_war_ir_buff_expiry_bounds_crediting()
    test_war_snapshot(resp)
    print("all warrior pipeline tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
