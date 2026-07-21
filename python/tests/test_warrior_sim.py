"""Warrior scoring + simulator invariants (network-free).

The analyzer's second TANK simulator (and first tank with an OFFENSIVE gauge).
Mirrors test_paladin_sim.py / test_reaper_sim.py:

  * Pipeline doesn't crash — `analyze_pull('Warrior', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band.
  * idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Surging Tempest: the maintained 10% self-buff amplifies the score (the
    coverage overlay), and the guaranteed crit-DH weaponskills (Inner Chaos /
    Primal Rend always; a free Fell Cleave inside an Inner Release window) carry
    the crit-DH multiplier.
  * The Inner Release burst is consistent (Primal Rend -> Ruination, one Primal
    Wrath per Inner Release; Inner Chaos fed by Infuriate), and the Beast Gauge
    overcap pass runs (WAR's offensive gauge, unlike PLD's defensive-only Oath).

Run from python/:  python tests/test_warrior_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.warrior import data as wd
from jobs.warrior import scoring as sc
from jobs.warrior.simulator import (
    WarriorRotationModel,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 300.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic WAR cast stream = the default-sim timeline as FFLogs cast
    events (near-ideal, so efficiency ~ high). Pre-pull casts (t<0) are dropped."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [
        {"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
         "sourceID": _SOURCE_ID, "abilityGameID": aid}
        for t, aid in timeline if t >= 0
    ]


class MockClient:
    """Serves a synthetic single-Warrior pull. Casts come from the sim; every
    other stream is empty (boss targetable throughout -> zero downtime; empty
    DamageDone -> Surging Tempest coverage falls back to full)."""

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
            "title": "WAR fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Warrior", "server": "T",
                     "type": "Player", "subType": "Warrior", "petOwner": None,
                     "gameID": 21},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Warrior", client, "AbCd1234", 1,
                        ranking_name=None, label="war-fixture")


# --- Pipeline invariants ---------------------------------------------------

_SHARED_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener",
                   "Alignment", "BuffDrift", "Scoring", "SurgingTempest"]


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
    assert 180 <= pps <= 450, f"p/sec out of band: {pps:.1f}"
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


def test_surging_tempest_amplifies():
    """The maintained Surging Tempest overlay scales the whole timeline by ~1.10
    (the coverage overlay on the delivered + idealized sides)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    no_st = sc.score_delivered_potency(timeline)
    with_st = sc.score_delivered_potency(
        timeline, st_intervals=[(-10.0, _DURATION_S + 1.0, wd.SURGING_TEMPEST_MULT)])
    assert with_st > no_st, f"Surging Tempest gave no amp: {with_st} <= {no_st}"
    # Full coverage = a uniform x1.10 on every cast.
    assert abs(with_st - no_st * wd.SURGING_TEMPEST_MULT) < 1.0


def test_guaranteed_crit_dh():
    """Inner Chaos / Primal Rend always crit-DH; a Fell Cleave only inside an
    Inner Release window."""
    cd = wd.GUARANTEED_CRIT_DH_MULT
    ic = wd.POTENCIES[wd.INNER_CHAOS]
    assert abs(sc.score_delivered_potency([(10.0, wd.INNER_CHAOS)]) - ic * cd) < 1e-6
    pr = wd.POTENCIES[wd.PRIMAL_REND]
    assert abs(sc.score_delivered_potency([(10.0, wd.PRIMAL_REND)]) - pr * cd) < 1e-6
    fc = wd.POTENCIES[wd.FELL_CLEAVE]
    # Inner Release at t=0 opens an ~8s crit-DH window; the FC at t=1 is inside it.
    inside = sc.score_delivered_potency([(0.0, wd.INNER_RELEASE), (1.0, wd.FELL_CLEAVE)])
    assert abs(inside - fc * cd) < 1e-6, inside
    # The same Fell Cleave well outside any IR window is plain potency.
    outside = sc.score_delivered_potency([(100.0, wd.FELL_CLEAVE)])
    assert abs(outside - fc) < 1e-6, outside


def test_inner_release_burst_consistent():
    """One Primal Rend -> Primal Ruination and one Primal Wrath per Inner Release
    (the final chain may be cut by fight end, hence n_ir-1), and Inner Chaos is
    fed by Infuriate."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    n_ir = c[wd.INNER_RELEASE]
    assert n_ir >= 1, "no Inner Release fired"
    for aid in (wd.PRIMAL_REND, wd.PRIMAL_RUINATION, wd.PRIMAL_WRATH):
        assert n_ir - 1 <= c[aid] <= n_ir, f"{aid}: {c[aid]} vs {n_ir} Inner Releases"
    # Every Infuriate grants Nascent Chaos -> exactly one Inner Chaos.
    assert c[wd.INNER_CHAOS] == c[wd.INFURIATE], \
        f"Inner Chaos {c[wd.INNER_CHAOS]} != Infuriate {c[wd.INFURIATE]}"


def test_inner_chaos_always_spends_beast():
    """Inner Chaos costs 50 Beast even under Inner Release — IR frees Fell Cleave /
    Decimate ONLY, and the Nascent Chaos upgrades sit outside the IR free-cast
    system, so they also never consume an IR stack."""
    model = WarriorRotationModel()

    # Inner Chaos under IR: debits 50 Beast, leaves the IR stack count intact.
    st = model.init_state()
    st.beast, st.inner_release, st.nascent_chaos = 100, 3, True
    model.apply_cast(st, wd.INNER_CHAOS)
    assert st.beast == 50, f"Inner Chaos should spend 50 Beast, got {st.beast}"
    assert st.inner_release == 3, "Inner Chaos must not consume an Inner Release stack"

    # Contrast: a free Fell Cleave under IR spends NO Beast and burns one stack.
    st = model.init_state()
    st.beast, st.inner_release = 100, 3
    model.apply_cast(st, wd.FELL_CLEAVE)
    assert st.beast == 100, f"Fell Cleave is free under IR, got {st.beast}"
    assert st.inner_release == 2, "a free Fell Cleave consumes one Inner Release stack"


def test_combo_integrity():
    """Heavy Swing -> Maim -> finisher (Storm's Eye / Storm's Path) chains cleanly;
    the FINAL combo may be cut by fight end, so the chain is allowed to be one short
    at the tail (same boundary tolerance as the Inner Release burst test)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[wd.MAIM] <= c[wd.HEAVY_SWING] <= c[wd.MAIM] + 1, \
        f"Heavy Swing {c[wd.HEAVY_SWING]} vs Maim {c[wd.MAIM]}"
    finishers = c[wd.STORMS_EYE] + c[wd.STORMS_PATH]
    assert c[wd.MAIM] - 1 <= finishers <= c[wd.MAIM], \
        f"finishers {finishers} vs Maim {c[wd.MAIM]}"
    # Surging Tempest is actually maintained (Storm's Eye fires periodically).
    assert c[wd.STORMS_EYE] >= 1


def test_beast_gauge_modeled():
    """WAR is the first tank with an OFFENSIVE gauge — the Overcap pass runs over
    a real Beast gauge (opposite of PLD's defensive-only, gauge-less Oath)."""
    gauge_names = [g.name for g in wd.JOB_DATA.gauges]
    assert "beast" in gauge_names, gauge_names
    mr = _run_pipeline()
    # The Overcap aspect ran and produced its findings list (possibly empty on a
    # near-ideal rotation — the point is the pass exists and didn't crash).
    oc = mr.aspects["Overcap"].state
    assert "findings" in oc
    assert all(f.gauge == "beast" for f in oc["findings"]), oc["findings"]


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all warrior sim tests passed")


if __name__ == "__main__":
    main()
