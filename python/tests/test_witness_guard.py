"""Witness guard on the strict ceiling (ScoringAspectBase.analyze).

The guard floors `idealized_strict` at `delivered` — the player's own executed
line valued by the very scorer the ceiling maximizes. A real, executed rotation
is a feasible candidate for the optimum, so strict efficiency stays ≤100% by
construction even when the ceiling search under-sequences at some sub-GCD sweep
cadence (the live Lindwurm II MCH case: off-2.50 band runs drop an Air Anchor /
Drill, no band point dominates the real line, and a rank-9 parse read 100.35%).
Strictly delivered — NOT delivered + tincture_loss: the pot-timing loss is
overlay-valued, a different currency from the ceiling's in-timeline pot markers,
and overshoots on budget/pet jobs (the BRD/SMN/DRK/VPR pipeline fixtures).

Two ends pinned here, on a synthetic MCH pull (the greedy sim line replayed as
FFLogs cast events — no network):

  * dominated (the normal case): the real ceiling beats the executed line, the
    guard is a no-op, and the `ceiling_witness_gap` key is ABSENT — the contract
    snapshot stays byte-identical.
  * under-search (forced): with `fns.idealized_at_duration` stubbed below
    delivered, the guard floors `idealized_strict` (and the back-compat
    `idealized_potency` + the pre-ref `idealized_lenient`) at delivered and
    emits the pre-guard shortfall as `ceiling_witness_gap`.

The heal-locked skip (a mit-plan-locked ceiling is EXPECTED to be exceedable and
keeps its raw value) is exercised by the healer lock suites — the skip is the
`HealLockContext` isinstance branch in analyze.

Run from python/:  python tests/test_witness_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DURATION_S = 240.0
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
    """Serves the synthetic single-MCH pull (the test_gunbreaker_pipeline stub
    shape). Targetability/aura streams empty → zero downtime, no observed pots."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "Witness-guard fixture",
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


def _scoring_aspect():
    from jobs import get_job
    return next(a for a in get_job("Machinist").aspects
                if getattr(a, "name", "") == "Scoring")


def _analyze_state() -> dict:
    from jobs import analyze_pull
    stub = StubFFLogsClient(_synthetic_casts())
    mr = analyze_pull("Machinist", stub, "WiTn3ss1", 1,
                      ranking_name=None, label="You")
    return mr.aspects["Scoring"].state


def test_dominated_ceiling_leaves_state_unchanged():
    state = _analyze_state()
    assert state["idealized_strict"] + 1e-6 >= state["delivered_potency"]
    assert "ceiling_witness_gap" not in state


def test_under_searched_ceiling_is_floored_at_the_witness(monkeypatch):
    aspect = _scoring_aspect()
    real = aspect.fns
    _FAKE_IDEALIZED = 1000.0  # far below any real delivered

    fake = SimpleNamespace(
        prime=lambda *a, **k: None,   # ceiling is stubbed — skip the sim warm
        enabler_net_values=real.enabler_net_values,
        idealized_at_duration=lambda *a, **k: _FAKE_IDEALIZED,
    )
    monkeypatch.setattr(aspect, "fns", fake)
    state = _analyze_state()

    delivered = state["delivered_potency"]
    assert delivered > _FAKE_IDEALIZED
    assert state["idealized_strict"] == pytest.approx(delivered)
    assert state["idealized_potency"] == pytest.approx(delivered)
    assert state["idealized_lenient"] == pytest.approx(delivered)
    assert state["ceiling_witness_gap"] == pytest.approx(
        delivered - _FAKE_IDEALIZED)


def main() -> int:
    test_dominated_ceiling_leaves_state_unchanged()

    class _MP:
        def setattr(self, obj, name, value):
            self._saved = (obj, name, getattr(obj, name))
            setattr(obj, name, value)

        def undo(self):
            obj, name, old = self._saved
            setattr(obj, name, old)

    mp = _MP()
    try:
        test_under_searched_ceiling_is_floored_at_the_witness(mp)
    finally:
        mp.undo()
    print("test_witness_guard: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
