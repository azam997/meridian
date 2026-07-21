"""Samurai scoring + simulator invariants (network-free).

Mirrors test_reaper_sim.py for the sixth job simulator:

  * Pipeline doesn't crash — `analyze_pull('Samurai', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Fugetsu coverage scoring: full beats partial beats none; with no damage events
    the measurement falls back to 100% (no penalty).
  * Guaranteed crit: the Setsugekka / Namikiri families are priced x crit.
  * Sen / Iaijutsu balance (every 3-Sen Setsugekka consumes three combo enders;
    each Iaijutsu/Ogi has its Kaeshi follow-up), and the Ikishoten -> Ogi chain.
  * Tengentsu Kenki (sim_context) raises the ceiling; Higanbana DoT over-refresh
    is overcap-safe; downtime lowers the ceiling.

Run from python/:  python tests/test_samurai_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.samurai import data as sd
from jobs.samurai import scoring as sc
from jobs.samurai.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic SAM cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0]


class MockClient:
    """Serves a synthetic single-SAM pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no damage events
    -> Fugetsu falls back to full coverage; no buff events -> Tengentsu Kenki 0)."""

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
            "title": "SAM fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Samurai", "server": "T",
                     "type": "Player", "subType": "Samurai", "petOwner": None,
                     "gameID": 34},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Samurai", client, "AbCd1234", 1,
                        ranking_name=None, label="sam-fixture")


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
    assert 250 <= pps <= 500, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_fugetsu_and_tengentsu_state_present():
    """With no damage/buff events: Fugetsu falls back to 100% coverage, Tengentsu
    Kenki is 0 (the measured-from-an-empty-stream defaults — no penalty)."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    assert st["fugetsuUptimePct"] == 100.0, st["fugetsuUptimePct"]
    assert st["tengentsuKenki"] == 0, st["tengentsuKenki"]


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
    fg = sc._full_fugetsu_intervals(_DURATION_S)
    delivered = sc.score_delivered_potency(degraded, fugetsu_intervals=fg)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_fugetsu_coverage_penalty():
    """Partial Fugetsu coverage scores strictly below full; none below partial."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    full = sc.score_delivered_potency(
        timeline, fugetsu_intervals=sc._full_fugetsu_intervals(_DURATION_S))
    partial = sc.score_delivered_potency(
        timeline, fugetsu_intervals=[(0.0, _DURATION_S / 2.0, sd.FUGETSU_MULT)])
    none_fg = sc.score_delivered_potency(timeline)
    assert none_fg < partial < full


def test_guaranteed_crit_applied():
    """A guaranteed-crit Setsugekka is priced x GUARANTEED_CRIT_MULT; a normal
    GCD (Gyofu) is not."""
    midare = sc.score_delivered_potency([(0.0, sd.MIDARE_SETSUGEKKA)])
    assert abs(midare - sd.POTENCIES[sd.MIDARE_SETSUGEKKA]
               * sd.GUARANTEED_CRIT_MULT) < 1e-6
    gyofu = sc.score_delivered_potency([(0.0, sd.GYOFU)])
    assert abs(gyofu - sd.POTENCIES[sd.GYOFU]) < 1e-6


def test_sen_iaijutsu_balance():
    """Every 3-Sen Setsugekka consumes three combo enders; Higanbana one. So
    enders == 3*(Midare+Tendo Setsugekka) + Higanbana + leftover Sen (0-2 built
    but not yet dumped at fight end)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    enders = c[sd.GEKKO] + c[sd.KASHA] + c[sd.YUKIKAZE]
    consumed = 3 * (c[sd.MIDARE_SETSUGEKKA] + c[sd.TENDO_SETSUGEKKA]) + c[sd.HIGANBANA]
    leftover = enders - consumed
    # 0-3 Sen can be built-but-not-yet-dumped at the fight-end boundary (3 when the
    # loop ends on the GCD that completes the 3rd Sen, before the Iaijutsu).
    assert 0 <= leftover <= 3, \
        f"enders={enders} consumed={consumed} leftover={leftover}"


def test_kaeshi_and_ikishoten_chains():
    """Each Ogi Namikiri has a Kaeshi: Namikiri; each Tendo Setsugekka a Tendo
    Kaeshi; Ogi count == Ikishoten count (Ogi is Ikishoten-granted)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[sd.OGI_NAMIKIRI] >= 1
    assert c[sd.KAESHI_NAMIKIRI] == c[sd.OGI_NAMIKIRI]
    assert c[sd.TENDO_KAESHI_SETSUGEKKA] == c[sd.TENDO_SETSUGEKKA]
    assert c[sd.OGI_NAMIKIRI] == c[sd.IKISHOTEN]


def test_tengentsu_sim_context_raises_ceiling():
    """More measured Tengentsu Kenki -> the ceiling spends more Shinten -> higher
    idealized potency (the symmetric RDM-proc-budget pattern)."""
    low = sc.idealized_at_duration(_DURATION_S, [], sim_context=0)
    high = sc.idealized_at_duration(_DURATION_S, [], sim_context=300)
    assert high > low, f"bonus Kenki did not raise the ceiling: {high} <= {low}"


def test_higanbana_dot_overrefresh_safe():
    """Over-refreshing Higanbana credits by time-to-next-cast (capped at 60s), so
    two casts 5s apart credit far less than two 60s apart — never double-counted."""
    tight = sc._higanbana_dot_potency([(0.0, sd.HIGANBANA), (5.0, sd.HIGANBANA)],
                                      None, None)
    spaced = sc._higanbana_dot_potency([(0.0, sd.HIGANBANA), (60.0, sd.HIGANBANA)],
                                       None, None)
    assert tight < spaced
    # A single cast credits at most one full DoT cycle.
    one = sc._higanbana_dot_potency([(0.0, sd.HIGANBANA)], None, None)
    max_one = (sd.HIGANBANA_DOT_DURATION_S / sd.HIGANBANA_DOT_TICK_S
               * sd.HIGANBANA_DOT_TICK_P)
    assert one <= max_one + 1e-6


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all samurai sim tests passed")


if __name__ == "__main__":
    main()
