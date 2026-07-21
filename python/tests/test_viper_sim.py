"""Viper scoring + simulator invariants (network-free).

Mirrors test_reaper_sim.py / test_samurai_sim.py for the tenth job simulator:

  * Pipeline doesn't crash — `analyze_pull('Viper', ...)` runs every aspect via the
    registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Hunter's Instinct (the maintained self-buff) coverage scoring: full beats
    partial beats none; with no damage events the measurement falls back to 100%.
  * VPR rotation balance: the Reawaken combo (Reawaken -> 4 Generations + Ouroboros,
    each Generation a Legacy oGCD), the Vicewinder coil cycle (each Coil double-
    weaving Twinfang + Twinblood Bite), Uncoiled Fury's two oGCDs, each ST finisher
    a Death Rattle, and the Serpent Offering economy funding the Reawakens.
  * downtime lowers the ceiling.

Run from python/:  python tests/test_viper_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.viper import data as vd
from jobs.viper import scoring as sc
from jobs.viper.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic VPR cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class MockClient:
    """Serves a synthetic single-VPR pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no damage events
    -> Hunter's Instinct falls back to full coverage)."""

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
            "title": "VPR fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Viper", "server": "T",
                     "type": "Player", "subType": "Viper", "petOwner": None,
                     "gameID": 41},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Viper", client, "AbCd1234", 1,
                        ranking_name=None, label="vpr-fixture")


def _counts(duration_s: float = _DURATION_S) -> Counter:
    return Counter(a for _t, a in simulate_idealized(duration_s, [])[0])


# --- Pipeline invariants ---------------------------------------------------

_ASPECTS = ["Abilities", "Drift", "Clipping", "Overcap", "Opener", "Alignment",
            "BuffDrift", "Scoring"]


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
    assert 280 <= pps <= 520, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_hunters_instinct_state_present():
    """With no damage events, Hunter's Instinct falls back to 100% coverage (the
    measured-from-an-empty-stream default — no penalty)."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    assert st["hunterInstinctUptimePct"] == 100.0, st["hunterInstinctUptimePct"]


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
    hi = sc._full_hunters_instinct_intervals(_DURATION_S)
    delivered = sc.score_delivered_potency(degraded, hunters_instinct_intervals=hi)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_hunters_instinct_coverage_penalty():
    """Partial Hunter's Instinct coverage scores strictly below full; none below
    partial (the SAM-Fugetsu self-buff overlay pattern)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    full = sc.score_delivered_potency(
        timeline, hunters_instinct_intervals=sc._full_hunters_instinct_intervals(_DURATION_S))
    partial = sc.score_delivered_potency(
        timeline, hunters_instinct_intervals=[(0.0, _DURATION_S / 2.0, vd.HUNTERS_INSTINCT_MULT)])
    none_hi = sc.score_delivered_potency(timeline)
    assert none_hi < partial < full


def test_hunters_instinct_multiplier():
    """A cast under Hunter's Instinct is priced x HUNTERS_INSTINCT_MULT; without it,
    raw potency."""
    hi = sc._full_hunters_instinct_intervals(_DURATION_S)
    base = sc.score_delivered_potency([(1.0, vd.OUROBOROS)])
    buffed = sc.score_delivered_potency([(1.0, vd.OUROBOROS)],
                                        hunters_instinct_intervals=hi)
    assert abs(base - vd.POTENCIES[vd.OUROBOROS]) < 1e-6
    assert abs(buffed - vd.POTENCIES[vd.OUROBOROS] * vd.HUNTERS_INSTINCT_MULT) < 1e-6


def test_reawaken_combo_balance():
    """Each Reawaken grants 5 Anguine Tribute spent by 4 Generations + Ouroboros;
    each Generation grants its Legacy oGCD. The final combo may be cut by fight
    end (tail tolerance of 1)."""
    c = _counts()
    r = c[vd.REAWAKEN]
    assert r >= 2, f"too few Reawakens: {r}"
    assert r - 1 <= c[vd.OUROBOROS] <= r, f"Ouroboros {c[vd.OUROBOROS]} vs Reawaken {r}"
    for gen, leg in ((vd.FIRST_GENERATION, vd.FIRST_LEGACY),
                     (vd.SECOND_GENERATION, vd.SECOND_LEGACY),
                     (vd.THIRD_GENERATION, vd.THIRD_LEGACY),
                     (vd.FOURTH_GENERATION, vd.FOURTH_LEGACY)):
        assert r - 1 <= c[gen] <= r, f"{gen} count {c[gen]} vs Reawaken {r}"
        assert c[gen] - 1 <= c[leg] <= c[gen], f"Legacy {leg} vs Generation {gen}"


def test_vicewinder_coil_cycle():
    """Each Vicewinder enables Hunter's Coil + Swiftskin's Coil; each Coil double-
    weaves a Twinfang Bite + a Twinblood Bite oGCD."""
    c = _counts()
    v = c[vd.VICEWINDER]
    assert v >= 2, f"too few Vicewinders: {v}"
    assert v - 1 <= c[vd.HUNTERS_COIL] <= v
    assert v - 1 <= c[vd.SWIFTSKINS_COIL] <= v
    coils = c[vd.HUNTERS_COIL] + c[vd.SWIFTSKINS_COIL]
    assert coils - 2 <= c[vd.TWINFANG_BITE] <= coils
    assert coils - 2 <= c[vd.TWINBLOOD_BITE] <= coils


def test_uncoiled_fury_ogcds():
    """Each Uncoiled Fury grants Uncoiled Twinfang + Uncoiled Twinblood."""
    c = _counts()
    u = c[vd.UNCOILED_FURY]
    assert u >= 1
    assert u - 1 <= c[vd.UNCOILED_TWINFANG] <= u
    assert u - 1 <= c[vd.UNCOILED_TWINBLOOD] <= u


def test_finisher_death_rattle():
    """Every ST combo finisher grants a Death Rattle oGCD."""
    c = _counts()
    finishers = (c[vd.FLANKSTING_STRIKE] + c[vd.FLANKSBANE_FANG]
                 + c[vd.HINDSTING_STRIKE] + c[vd.HINDSBANE_FANG])
    assert finishers >= 2
    assert finishers - 1 <= c[vd.DEATH_RATTLE] <= finishers


def test_offering_funds_reawakens():
    """Serpent Offering generation (finishers +10, coils +5) plus the free Serpent's
    Ire Reawakens must cover every Reawaken's 50-gauge cost (never overdrawn)."""
    c = _counts()
    finishers = (c[vd.FLANKSTING_STRIKE] + c[vd.FLANKSBANE_FANG]
                 + c[vd.HINDSTING_STRIKE] + c[vd.HINDSBANE_FANG])
    coils = c[vd.HUNTERS_COIL] + c[vd.SWIFTSKINS_COIL]
    offering = 10 * finishers + 5 * coils
    free = c[vd.SERPENTS_IRE]            # each grants Ready to Reawaken (free)
    paid = max(0, c[vd.REAWAKEN] - free)
    # Offering must fund the paid Reawakens (allow one in-flight cycle of slack).
    assert offering + 50 >= paid * 50, \
        f"offering {offering} cannot fund {paid} paid Reawakens"


def test_no_overcap_in_gauges():
    """The greedy rotation never holds Rattling Coil at its cap when it could dump
    (Uncoiled Fury fires before Vicewinder/Ire would overcap) — a sanity check that
    Uncoiled Fury count tracks coil generation."""
    c = _counts()
    coil_gen = c[vd.VICEWINDER] + c[vd.SERPENTS_IRE]
    # Uncoiled Fury spends coil 1:1; it should consume nearly all generated coil
    # (within a couple banked at fight end).
    assert c[vd.UNCOILED_FURY] >= coil_gen - 3, \
        f"Uncoiled Fury {c[vd.UNCOILED_FURY]} << coil generated {coil_gen}"


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all viper sim tests passed")


if __name__ == "__main__":
    main()
