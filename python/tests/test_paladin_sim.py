"""Paladin scoring + simulator invariants (network-free).

The analyzer's first TANK simulator. Mirrors test_reaper_sim.py:

  * Pipeline doesn't crash — `analyze_pull('Paladin', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band.
  * idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Fight or Flight scoring: removing the FoF casts (so no window) scores strictly
    below keeping them — the self-buff amp fires; and Goring Blade is always cast
    INSIDE a FoF window (the gating that keeps the ceiling honest).
  * The magical combo is consistent (one Confiteor->...->Blade of Honor chain per
    Imperator), and the ranged pre-pull Holy Spirit channel is emitted.

Run from python/:  python tests/test_paladin_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.paladin import data as pd
from jobs.paladin import scoring as sc
from jobs.paladin.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 300.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic PLD cast stream = the default-sim timeline as FFLogs cast
    events (near-ideal, so efficiency ~ high). Pre-pull casts (t<0) are dropped."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [
        {"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
         "sourceID": _SOURCE_ID, "abilityGameID": aid}
        for t, aid in timeline if t >= 0
    ]


class MockClient:
    """Serves a synthetic single-Paladin pull. Casts come from the sim; every
    other stream is empty (boss targetable throughout -> zero downtime)."""

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
            "title": "PLD fixture",
            "startTime": _FIGHT_START_MS,
            "endTime": end_ms,
            "fights": [{
                "id": 1, "name": "Fight", "encounterID": 101, "difficulty": 101,
                "kill": True, "startTime": _FIGHT_START_MS, "endTime": end_ms,
                "friendlyPlayers": [_SOURCE_ID],
                "enemyNPCs": [{"id": _BOSS_ID, "gameID": 1, "petOwner": None}],
            }],
            "masterData": {
                "actors": [
                    {"id": _SOURCE_ID, "name": "Test Paladin", "server": "T",
                     "type": "Player", "subType": "Paladin", "petOwner": None,
                     "gameID": 19},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Paladin", client, "AbCd1234", 1,
                        ranking_name=None, label="pld-fixture")


# --- Pipeline invariants ---------------------------------------------------

_SHARED_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                   "Alignment", "BuffDrift", "Scoring"]


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
    assert 180 <= pps <= 400, f"p/sec out of band: {pps:.1f}"
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
    sd = sc.score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    so = sc.score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    sp = sc.score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert so >= sd - 1e-6, f"optimal {so} < default {sd}"
    assert sp >= so - 1e-6, f"perfect {sp} < optimal {so}"


def test_idealized_beats_degraded_delivered():
    """idealized@duration >= a delivered run with half the casts dropped."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = sc.score_delivered_potency(degraded)
    assert ideal >= delivered


def test_downtime_reduces_ceiling():
    no_dt = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 150.0)])
    assert with_dt < no_dt


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_fight_or_flight_amplifies():
    """Dropping the FoF casts (so no window) scores strictly below keeping them —
    the self-buff is folded into the score from the timeline's own FoF casts."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    with_fof = sc.score_delivered_potency(timeline)
    no_fof = sc.score_delivered_potency(
        [(t, a) for t, a in timeline if a != pd.FIGHT_OR_FLIGHT])
    assert with_fof > no_fof, f"FoF gave no amp: {with_fof} <= {no_fof}"


def test_goring_blade_inside_fof_window():
    """Goring Blade is held for the FoF window so the idealized cast is always
    amplified (preserving the efficiency <= 100% guard)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    fof = [t for t, a in timeline if a == pd.FIGHT_OR_FLIGHT]
    gor = [t for t, a in timeline if a == pd.GORING_BLADE]
    assert gor, "no Goring Blade fired"
    dur = pd.FIGHT_OR_FLIGHT_DURATION_S
    for g in gor:
        assert any(f <= g < f + dur for f in fof), f"Goring at {g} outside any FoF"


def test_magical_combo_consistent():
    """One Confiteor -> Blade of Faith -> Truth -> Valor -> Blade of Honor chain
    per Imperator (the final chain may be cut by fight end, hence n_imp-1)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    n_imp = c[pd.IMPERATOR]
    assert n_imp >= 1, "no Imperator fired"
    for aid in (pd.CONFITEOR, pd.BLADE_OF_FAITH, pd.BLADE_OF_TRUTH,
                pd.BLADE_OF_VALOR, pd.BLADE_OF_HONOR):
        assert n_imp - 1 <= c[aid] <= n_imp, f"{aid}: {c[aid]} vs {n_imp} Imperators"


def test_burst_and_combo_present():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[pd.FIGHT_OR_FLIGHT] >= 1
    # The main combo runs (Royal Authority feeds the Atonement chain).
    assert c[pd.ROYAL_AUTHORITY] >= 1
    assert c[pd.ATONEMENT] >= 1 and c[pd.SEPULCHRE] >= 1


def test_prepull_holy_spirit_channel():
    """The default sim opens with the ranged Holy Spirit precast (negative t)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    assert any(t < 0 and a == pd.HOLY_SPIRIT for t, a in timeline), \
        "no pre-pull Holy Spirit channel"


def test_no_offensive_gauge():
    """PLD models no offensive gauge — the Overcap aspect has nothing to flag."""
    mr = _run_pipeline()
    oc = mr.aspects["Overcap"].state
    # Either no gauges key, or every gauge reports zero overcap.
    gauges = oc.get("gauges") or oc.get("overcaps") or []
    assert not gauges or all(
        (g.get("lost_potency", 0) or 0) == 0 for g in gauges if isinstance(g, dict)
    ), oc


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all paladin sim tests passed")


if __name__ == "__main__":
    main()
