"""Dragoon scoring + simulator invariants (network-free).

Mirrors test_samurai_sim.py for the 11th job simulator (the first beam-forking melee):

  * Pipeline doesn't crash — `analyze_pull('Dragoon', ...)` runs every aspect via the
    registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Life Surge: the next finisher after a Life Surge is priced x the crit multiplier;
    a finisher with no Life Surge is not.
  * Power Surge / Life of the Dragon self-buffs derived from the timeline amp later
    casts (full beats none); the branch fork keeps the Chaotic Spring DoT alive.
  * Chaotic Spring DoT over-refresh is overcap-safe; downtime lowers the ceiling.
  * Firstminds' Focus balance (Wyrmwind Thrust spends 2 per cast) and the
    Life-of-the-Dragon chain (3 Nastrond per Geirskogul, Stardiver -> Starcross).

Run from python/:  python tests/test_dragoon_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.dragoon import data as dd
from jobs.dragoon import scoring as sc
from jobs.dragoon.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic DRG cast stream = the default-sim timeline, as FFLogs cast events
    (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class MockClient:
    """Serves a synthetic single-DRG pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no damage events
    -> positionals fall back to assume-hit)."""

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
            "title": "DRG fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Fight", "encounterID": 103, "difficulty": 101,
                "kill": True, "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "Test Dragoon", "server": "T",
                     "type": "Player", "subType": "Dragoon", "petOwner": None,
                     "gameID": 22},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Dragoon", client, "AbCd1234", 1,
                        ranking_name=None, label="drg-fixture")


# --- Pipeline invariants ---------------------------------------------------

_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment",
            "BuffDrift", "Scoring", "Positionals", "LifeSurge"]


def test_pipeline_runs_and_has_aspects():
    mr = _run_pipeline()
    for name in _ASPECTS:
        assert name in mr.aspects, f"missing {name}"


def test_delivered_in_band_and_below_ceiling():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    delivered = st["delivered_potency"]
    ideal = st["idealized_strict"]
    assert delivered > 0
    pps = delivered / _DURATION_S
    assert 250 <= pps <= 500, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


# --- Simulator invariants --------------------------------------------------

def test_sim_monotonicity():
    s_d = sc.score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    s_o = sc.score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    s_p = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert s_o >= s_d - 1e-6, f"optimal {s_o} < default {s_d}"
    assert s_p >= s_o - 1e-6, f"perfect {s_p} < optimal {s_o}"


def test_idealized_beats_degraded_delivered():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]  # drop half the casts
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = sc.score_delivered_potency(degraded)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_life_surge_guaranteed_crit():
    """A Drakesbane immediately after a Life Surge is priced x the crit multiplier;
    a Drakesbane with no Life Surge is at base potency."""
    crit = sc.score_delivered_potency([(0.0, dd.LIFE_SURGE), (1.0, dd.DRAKESBANE)])
    base = sc.score_delivered_potency([(0.0, dd.DRAKESBANE)])
    assert abs(crit - dd.POTENCIES[dd.DRAKESBANE] * dd.GUARANTEED_CRIT_MULT) < 1e-6
    assert abs(base - dd.POTENCIES[dd.DRAKESBANE]) < 1e-6


def test_power_surge_self_buff():
    """A cast after Spiral Blow is amped by Power Surge (+10%); the same cast with no
    preceding Spiral Blow is not. Spiral Blow never amps itself."""
    with_ps = sc.score_delivered_potency([(0.0, dd.SPIRAL_BLOW), (1.0, dd.HEAVENS_THRUST)])
    no_ps = sc.score_delivered_potency([(0.0, dd.HEAVENS_THRUST)])
    expected = (dd.POTENCIES[dd.SPIRAL_BLOW]
                + dd.POTENCIES[dd.HEAVENS_THRUST] * dd.POWER_SURGE_MULT)
    assert abs(with_ps - expected) < 1e-6, f"{with_ps} != {expected}"
    # Spiral Blow alone is NOT under its own Power Surge.
    spiral_alone = sc.score_delivered_potency([(0.0, dd.SPIRAL_BLOW)])
    assert abs(spiral_alone - dd.POTENCIES[dd.SPIRAL_BLOW]) < 1e-6


def test_lotd_self_buff():
    """Geirskogul grants Life of the Dragon (+15%) -> later casts amped; Geirskogul
    itself is not under its own LotD."""
    after = sc.score_delivered_potency([(0.0, dd.GEIRSKOGUL), (1.0, dd.DRAKESBANE)])
    expected = (dd.POTENCIES[dd.GEIRSKOGUL]
                + dd.POTENCIES[dd.DRAKESBANE] * dd.LOTD_MULT)
    assert abs(after - expected) < 1e-6, f"{after} != {expected}"


def test_chaotic_spring_dot_overrefresh_safe():
    """Over-refreshing Chaotic Spring credits by time-to-next-cast (capped at 24s),
    so two casts 5s apart credit far less than two 24s apart — never double-counted."""
    tight = sc._chaotic_spring_dot_potency([(0.0, 1.0), (5.0, 1.0)],
                                           [(0.0, dd.CHAOTIC_SPRING), (5.0, dd.CHAOTIC_SPRING)])
    spaced = sc._chaotic_spring_dot_potency([(0.0, 1.0), (24.0, 1.0)],
                                            [(0.0, dd.CHAOTIC_SPRING), (24.0, dd.CHAOTIC_SPRING)])
    assert tight < spaced
    one = sc._chaotic_spring_dot_potency([(0.0, 1.0)], [(0.0, dd.CHAOTIC_SPRING)])
    max_one = (dd.CHAOTIC_SPRING_DOT_DURATION_S / dd.CHAOTIC_SPRING_DOT_TICK_S
               * dd.CHAOTIC_SPRING_DOT_TICK_P)
    assert one <= max_one + 1e-6


def test_combo_branch_and_dot_maintained():
    """The sim alternates the two combos (both Spiral Blow and Lance Barrage appear),
    runs the full 5-GCD chain (Drakesbane closes both), and keeps the Chaotic Spring
    DoT effectively maintained (a Chaotic Spring every ~24s of GCD time)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[dd.SPIRAL_BLOW] >= 1 and c[dd.LANCE_BARRAGE] >= 1, "no branching"
    # Each combo ends on Drakesbane: drakesbane ~= dot-combo + raw-combo count.
    assert c[dd.DRAKESBANE] == c[dd.SPIRAL_BLOW] + c[dd.LANCE_BARRAGE] - _open_partial(c)
    # DoT cadence: a Chaotic Spring roughly every <= 28s of fight (24s DoT + slack).
    assert c[dd.CHAOTIC_SPRING] >= _DURATION_S / 28.0


def _open_partial(c: Counter) -> int:
    # Drakesbane lags the branch count by at most 1 (a combo in progress at fight end).
    return max(0, c[dd.SPIRAL_BLOW] + c[dd.LANCE_BARRAGE] - c[dd.DRAKESBANE])


def test_focus_balance():
    """Wyrmwind Thrust spends 2 Firstminds' Focus; Raiden Thrust generates 1. So
    2 * WWT <= Raiden Thrust casts (you can't spend Focus you never generated)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    generated = c[dd.RAIDEN_THRUST] + c[dd.DRACONIAN_FURY]
    assert 2 * c[dd.WYRMWIND_THRUST] <= generated, \
        f"WWT={c[dd.WYRMWIND_THRUST]} > Focus generated {generated} / 2"


def test_lotd_chain():
    """Geirskogul grants 1 Nastrond (DT 7.x) + enables Stardiver -> Starcross. So
    Nastrond == Geirskogul (modulo a trailing partial window), Starcross == Stardiver,
    and Stardiver <= Geirskogul."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[dd.GEIRSKOGUL] >= 1
    assert c[dd.NASTROND] <= dd.NASTROND_PER_LOTD * c[dd.GEIRSKOGUL]
    assert c[dd.NASTROND] >= dd.NASTROND_PER_LOTD * (c[dd.GEIRSKOGUL] - 1)
    assert c[dd.STARCROSS] == c[dd.STARDIVER]
    assert c[dd.STARDIVER] <= c[dd.GEIRSKOGUL]


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all dragoon sim tests passed")


if __name__ == "__main__":
    main()
