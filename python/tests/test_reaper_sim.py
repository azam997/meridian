"""Reaper scoring + simulator invariants (network-free).

Mirrors test_execution.py for the second job simulator:

  * Pipeline doesn't crash — `analyze_pull('Reaper', ...)` runs every aspect
    via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band.
  * idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock
    budget.
  * Death's Design scoring: full coverage beats partial (the DD penalty fires);
    with no DD events the aspect falls back to 100% (no penalty).
  * Enshroud sub-rotation is gauge-consistent (one Communio per Enshroud, four
    Reapings per Enshroud) and the Soulsow -> Harvest Moon downtime re-arm works.

Run from python/:  python tests/test_reaper_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.reaper import data as rd
from jobs.reaper import scoring as sc
from jobs.reaper.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 300.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic RPR cast stream = the default-sim timeline, as FFLogs cast
    events. Used as the 'delivered' run (near-ideal, so efficiency ~ high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    evs = []
    for t, aid in timeline:
        if t < 0:
            continue
        evs.append({
            "timestamp": _FIGHT_START_MS + int(t * 1000),
            "type": "cast",
            "sourceID": _SOURCE_ID,
            "abilityGameID": aid,
        })
    return evs


class MockClient:
    """Serves a synthetic single-Reaper pull. Casts come from the sim; every
    other stream is empty (no targetability -> boss targetable throughout, zero
    downtime; no debuff events -> Death's Design falls back to full coverage)."""

    def __init__(self, casts: list[dict]):
        self._casts = casts

    def get_events(self, code, start, end, source_id, data_type="Casts",
                   ability_id=None):
        if data_type != "Casts":
            return []
        return [e for e in self._casts if start <= e["timestamp"] <= end]

    def get_targetability_events(self, code, start, end):
        return []

    def get_aura_events(self, code, start, end, actor_id, data_type="Buffs"):
        return []

    def get_report_summary(self, code: str) -> dict:
        end_ms = _FIGHT_START_MS + int(_DURATION_S * 1000)
        return {
            "title": "RPR fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1,
                "name": "Fight",
                "encounterID": 103,
                "difficulty": 101,
                "kill": True,
                "startTime": _FIGHT_START_MS,
                "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "Test Reaper", "server": "T",
                     "type": "Player", "subType": "Reaper", "petOwner": None,
                     "gameID": 39},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T",
                     "type": "NPC", "subType": "Boss", "petOwner": None,
                     "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Reaper", client, "AbCd1234", 1,
                        ranking_name=None, label="rpr-fixture")


# --- Pipeline invariants ---------------------------------------------------

_SHARED_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                   "Alignment", "BuffDrift", "Scoring", "DeathsDesign"]


def test_pipeline_runs_and_has_aspects():
    mr = _run_pipeline()
    for name in _SHARED_ASPECTS:
        assert name in mr.aspects, f"missing {name}"


def test_delivered_in_band_and_below_ceiling():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    delivered = st["delivered_potency"]
    ideal = st["idealized_strict"]
    assert delivered > 0
    pps = delivered / _DURATION_S
    assert 200 <= pps <= 430, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_deaths_design_full_coverage_fallback():
    """With no DD events the aspect assumes 100% uptime (no penalty)."""
    mr = _run_pipeline()
    dd = mr.aspects["DeathsDesign"].state
    assert dd["coverage_pct"] == 100.0, dd
    assert dd["lost_potency"] == 0.0, dd


# --- Simulator invariants --------------------------------------------------

def test_sim_monotonicity():
    sd = sc.score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    so = sc.score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    sp = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert so >= sd - 1e-6, f"optimal {so} < default {sd}"
    assert sp >= so - 1e-6, f"perfect {sp} < optimal {so}"


def test_idealized_beats_degraded_delivered():
    """idealized@duration >= a delivered run with a few casts dropped."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]  # drop half the casts
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    dd_full = sc._full_dd_intervals(_DURATION_S)
    delivered = sc.score_delivered_potency(degraded, dd_intervals=dd_full)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_deaths_design_multiplier_penalty():
    """Partial Death's Design coverage scores strictly below full coverage."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    full = sc.score_delivered_potency(
        timeline, dd_intervals=sc._full_dd_intervals(_DURATION_S))
    partial = sc.score_delivered_potency(
        timeline, dd_intervals=[(0.0, _DURATION_S / 2.0, rd.DEATHS_DESIGN_MULT)])
    none_dd = sc.score_delivered_potency(timeline)
    assert none_dd < partial < full


def test_enshroud_subrotation_consistent():
    """Each Enshroud window ends in exactly one Communio and contains four
    Void/Cross Reapings (the 5-Lemure budget: 4 reapings + Communio)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    n_ensh = c[rd.ENSHROUD]
    assert n_ensh > 0, "no Enshroud fired"
    assert c[rd.COMMUNIO] == n_ensh, f"Communio {c[rd.COMMUNIO]} != Enshroud {n_ensh}"
    reapings = c[rd.VOID_REAPING] + c[rd.CROSS_REAPING]
    assert reapings == 4 * n_ensh, f"{reapings} reapings != 4 x {n_ensh} Enshrouds"


def test_burst_and_upkeep_present():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    # Death's Design refreshed on cadence (~ every 30s).
    assert c[rd.SHADOW_OF_DEATH] >= _DURATION_S / 45.0
    # Raid burst fires.
    assert c[rd.ARCANE_CIRCLE] >= 1 and c[rd.PLENTIFUL_HARVEST] >= 1
    # Perfectio only follows a Plentiful-Harvest-fed Communio.
    assert c[rd.PERFECTIO] <= c[rd.PLENTIFUL_HARVEST]


def test_soulsow_rearm_in_downtime():
    """A long downtime window re-arms Soulsow, yielding an extra Harvest Moon."""
    base, _ = simulate_idealized(_DURATION_S, [])
    with_dt, _ = simulate_idealized(_DURATION_S, [(120.0, 145.0)])
    cb = Counter(a for _, a in base)
    cd = Counter(a for _, a in with_dt)
    assert cd[rd.SOULSOW] >= 1
    assert cd[rd.HARVEST_MOON] > cb[rd.HARVEST_MOON]


def test_starts_soulsow_armed_never_recasts_in_uptime():
    """The fight starts with Soulsow active (Harvest Moon pre-armed), so on a
    no-downtime pull the sim NEVER spends a pre-pull (or in-fight) GCD re-casting
    Soulsow — it just fires the armed Harvest Moon. (Soulsow is only re-armed
    inside a downtime window; see test_soulsow_rearm_in_downtime.)"""
    from jobs.reaper.simulator import ReaperRotationModel
    assert ReaperRotationModel().init_state().soulsow is True
    tl, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in tl)
    assert c[rd.SOULSOW] == 0, "should not re-cast the pre-armed Soulsow in uptime"
    assert c[rd.HARVEST_MOON] >= 1, "should fire the pre-armed Harvest Moon"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all reaper sim tests passed")


if __name__ == "__main__":
    main()
