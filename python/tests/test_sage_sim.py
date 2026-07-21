"""Sage scoring + simulator invariants (network-free).

Mirrors test_scholar_sim.py for the twenty-first job simulator (the fourth healer,
second shield healer). SGE is a low-fork healer — no GCD-recast haste window, no
offensive gauge — with two structural additions over AST: the Eukrasia DoT sequence
(Eukrasia -> Eukrasian Dosis III) and the Phlegma III charge economy. The invariants
pin its structure, not exact (⚠️ pre-calibration) potencies:

  * Pipeline doesn't crash — `analyze_pull('Sage', ...)` runs every aspect via the
    registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain; equal for a no-fork job).
  * The Eukrasian Dosis III DoT is kept up on a ~30s cadence via the 2-GCD Eukrasia
    sequence (every Eukrasia is immediately followed by an Eukrasian Dosis III).
  * The Phlegma III charge economy never over-produces (count bounded by the regen
    budget); Psyche fires on its ~60s cooldown.
  * The Eukrasian Dosis III DoT over-refresh is overcap-safe; downtime lowers the
    ceiling.
  * Mit-plan heal locks are placed inside their window (the honest-maximum ceiling).

Run from python/:  python tests/test_sage_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.sage import data as gd
from jobs.sage import scoring as sc
from jobs.sage.simulator import (
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
    """A realistic SGE cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-SGE pull. Casts come from the sim; every other
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
            "title": "SGE fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Sage", "server": "T",
                     "type": "Player", "subType": "Sage", "petOwner": None,
                     "gameID": 40},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Sage", client, "AbCd1234", 1,
                        ranking_name=None, label="sge-fixture")


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


def test_eukrasia_sequence_and_dot_kept_up():
    """Every Eukrasia is immediately followed (in GCD order) by an Eukrasian Dosis
    III, and the DoT is refreshed on a ~30s cadence — never left to lapse for long."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    # Sequence: the GCD after each Eukrasia is always Eukrasian Dosis III.
    gcds = [a for _, a in timeline if a not in gd.OGCD_IDS
            and a != gd.TOXIKON_II]  # (Toxikon never appears in the sim anyway)
    for i, a in enumerate(gcds):
        if a == gd.EUKRASIA:
            assert i + 1 < len(gcds) and gcds[i + 1] == gd.EUKRASIAN_DOSIS_III, \
                f"Eukrasia at GCD {i} not followed by Eukrasian Dosis III"
    dots = sorted(t for t, a in timeline if a == gd.EUKRASIAN_DOSIS_III)
    assert len(dots) >= _DURATION_S / gd.EUKRASIAN_DOSIS_DOT_DURATION_S - 1
    for a, b in zip(dots, dots[1:]):
        assert b - a <= gd.EUKRASIAN_DOSIS_DOT_DURATION_S + 1e-6, \
            f"Eukrasian Dosis lapsed: {a}->{b}"


def test_phlegma_charge_economy():
    """Phlegma III opens with 2 charges and recharges one per ~CD_S; over the fight
    the count is bounded by (2 + duration/CD_S) — never over-produced by the engine's
    multi-charge regen."""
    c = Counter(a for _, a in simulate_idealized(_DURATION_S, [])[0])
    max_possible = gd.PHLEGMA_CHARGES + _DURATION_S / gd.PHLEGMA_CD_S + 1e-6
    assert c[gd.PHLEGMA_III] <= max_possible, \
        f"Phlegma over-produced: {c[gd.PHLEGMA_III]} > {max_possible:.1f}"
    # And it is actually being cast on cooldown (not starved).
    assert c[gd.PHLEGMA_III] >= _DURATION_S / gd.PHLEGMA_CD_S - 1


def test_psyche_on_cooldown():
    """Psyche fires on its ~60s cooldown — roughly duration/CD casts, never more."""
    c = Counter(a for _, a in simulate_idealized(_DURATION_S, [])[0])
    assert c[gd.PSYCHE] <= _DURATION_S / gd.PSYCHE_CD_S + 1
    assert c[gd.PSYCHE] >= _DURATION_S / gd.PSYCHE_CD_S - 1


def test_dot_overrefresh_safe():
    """Over-refreshing the Eukrasian Dosis III DoT credits by time-to-next-cast
    (capped at the DoT duration), so two casts 5s apart credit far less than two a
    full duration apart — never double-counted."""
    tight = sc._eukrasian_dosis_dot_potency(
        [(0.0, gd.EUKRASIAN_DOSIS_III), (5.0, gd.EUKRASIAN_DOSIS_III)], None)
    spaced = sc._eukrasian_dosis_dot_potency(
        [(0.0, gd.EUKRASIAN_DOSIS_III),
         (gd.EUKRASIAN_DOSIS_DOT_DURATION_S, gd.EUKRASIAN_DOSIS_III)], None)
    assert tight < spaced
    one = sc._eukrasian_dosis_dot_potency([(0.0, gd.EUKRASIAN_DOSIS_III)], None)
    max_one = (gd.EUKRASIAN_DOSIS_DOT_DURATION_S / gd.EUKRASIAN_DOSIS_DOT_TICK_S) \
        * gd.EUKRASIAN_DOSIS_DOT_TICK_P
    assert one <= max_one + 1e-6


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_heal_locks_placed_in_window():
    """The mit-plan heal lock forces the required count of Eukrasian Prognosis II
    into its window — the ceiling already pays the healing tax (the honest max)."""
    hl = HealLockContext(
        locks=(LockedGcdWindow(ability_id=gd.EUKRASIAN_PROGNOSIS_II,
                               start_s=100.0, end_s=130.0, count=3, cast_s=2.0),),
        inner=None)
    timeline, _ = simulate_idealized_perfect(_DURATION_S, [], None, hl)
    in_window = [a for t, a in timeline
                 if a == gd.EUKRASIAN_PROGNOSIS_II and 100.0 <= t < 130.0]
    assert len(in_window) == 3, f"expected 3 locked heals, got {len(in_window)}"
    # A cold (unlocked) run never voluntarily casts the heal GCD.
    cold, _ = simulate_idealized_perfect(_DURATION_S, [])
    assert not any(a == gd.EUKRASIAN_PROGNOSIS_II for _t, a in cold)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all sage sim tests passed")


if __name__ == "__main__":
    main()
