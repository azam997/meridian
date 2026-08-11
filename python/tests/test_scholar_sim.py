"""Scholar scoring + simulator invariants (network-free).

Mirrors test_astrologian_sim.py for the twentieth job simulator (the third healer).
SCH is a low-fork healer — no GCD-recast haste window — with one offensive gauge
(Aetherflow, spent by Energy Drain), so the invariants pin its structure, not exact
(⚠️ pre-calibration) potencies:

  * Pipeline doesn't crash — `analyze_pull('Scholar', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain; equal for a no-fork job).
  * Biolysis is kept up on a ~30s cadence; Baneful Impaction is funded by Chain
    Stratagem (one payoff per enabler); the Aetherflow economy closes (Energy Drain
    count == 3 x Aetherflow casts).
  * Biolysis DoT over-refresh is overcap-safe; downtime lowers the ceiling.
  * Mit-plan heal locks are placed inside their window (the honest-maximum ceiling).

Run from python/:  python tests/test_scholar_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.scholar import data as sd
from jobs.scholar import scoring as sc
from jobs.scholar.simulator import (
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)
from jobs._core.heal_locks import HealLockContext, LockedGcdWindow

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic SCH cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-SCH pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no buff events
    -> no observed raid buffs / tincture windows)."""

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
            "title": "SCH fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Scholar", "server": "T",
                     "type": "Player", "subType": "Scholar", "petOwner": None,
                     "gameID": 28},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Scholar", client, "AbCd1234", 1,
                        ranking_name=None, label="sch-fixture")


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
    assert 110 <= pps <= 320, f"p/sec out of band: {pps:.1f}"
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


def test_biolysis_kept_up():
    """Biolysis is refreshed on a ~30s cadence — roughly duration/30 casts over the
    fight, never left to lapse for long."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    dots = sorted(t for t, a in timeline if a == sd.BIOLYSIS)
    assert len(dots) >= _DURATION_S / sd.BIOLYSIS_DOT_DURATION_S - 1
    # No two consecutive refreshes leave a gap longer than the DoT duration.
    for a, b in zip(dots, dots[1:]):
        assert b - a <= sd.BIOLYSIS_DOT_DURATION_S + 1e-6, f"biolysis lapsed: {a}->{b}"


def test_baneful_funded_by_chain_stratagem():
    """Baneful Impaction is unlocked only by Chain Stratagem's stack — one Baneful
    per Chain Stratagem (the last window may not fire it before fight end)."""
    c = Counter(a for _, a in simulate_idealized(_DURATION_S, [])[0])
    assert c[sd.CHAIN_STRATAGEM] >= 2
    assert c[sd.BANEFUL_IMPACTION] <= c[sd.CHAIN_STRATAGEM]
    assert c[sd.BANEFUL_IMPACTION] >= c[sd.CHAIN_STRATAGEM] - 1


def test_aetherflow_economy_closes():
    """Energy Drain spends exactly one Aetherflow stack, and Aetherflow refills 3
    per cast — so Energy Drain count == 3 x Aetherflow casts (never over-produced,
    the gauge is a closed cast-visible economy)."""
    c = Counter(a for _, a in simulate_idealized(_DURATION_S, [])[0])
    assert c[sd.AETHERFLOW] >= _DURATION_S / sd.AETHERFLOW_CD_S - 1
    assert c[sd.ENERGY_DRAIN] == sd.AETHERFLOW_STACKS * c[sd.AETHERFLOW], \
        f"Energy Drain {c[sd.ENERGY_DRAIN]} != 3 x Aetherflow {c[sd.AETHERFLOW]}"


def test_biolysis_dot_overrefresh_safe():
    """Over-refreshing Biolysis credits by time-to-next-cast (capped at the DoT
    duration), so two casts 5s apart credit far less than two a full duration apart
    — never double-counted."""
    tight = sc._biolysis_dot_potency([(0.0, sd.BIOLYSIS), (5.0, sd.BIOLYSIS)], None)
    spaced = sc._biolysis_dot_potency(
        [(0.0, sd.BIOLYSIS), (sd.BIOLYSIS_DOT_DURATION_S, sd.BIOLYSIS)], None)
    assert tight < spaced
    one = sc._biolysis_dot_potency([(0.0, sd.BIOLYSIS)], None)
    max_one = (sd.BIOLYSIS_DOT_DURATION_S / sd.BIOLYSIS_DOT_TICK_S) * sd.BIOLYSIS_DOT_TICK_P
    assert one <= max_one + 1e-6


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_heal_locks_placed_in_window():
    """The mit-plan heal lock forces the required count of Concitation into its
    window — the ceiling already pays the healing tax (the honest maximum)."""
    hl = HealLockContext(
        locks=(LockedGcdWindow(ability_id=sd.CONCITATION,
                               start_s=100.0, end_s=130.0, count=3, cast_s=2.0),),
        inner=None)
    timeline, _ = simulate_idealized_perfect(_DURATION_S, [], None, hl)
    in_window = [a for t, a in timeline
                 if a == sd.CONCITATION and 100.0 <= t < 130.0]
    assert len(in_window) == 3, f"expected 3 locked heals, got {len(in_window)}"
    # A cold (unlocked) run never voluntarily casts the heal GCD.
    cold, _ = simulate_idealized_perfect(_DURATION_S, [])
    assert not any(a == sd.CONCITATION for _t, a in cold)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all scholar sim tests passed")


if __name__ == "__main__":
    main()
