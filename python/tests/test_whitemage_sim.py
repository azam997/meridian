"""White Mage scoring + simulator invariants (network-free).

Mirrors test_samurai_sim.py for the seventh job simulator (the first healer):

  * Pipeline doesn't crash — `analyze_pull('White Mage', ...)` runs every aspect
    via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * The lily economy: every Misery is funded by 3 lily spends, the sim never
    wastes a spend at a bloomed Blood Lily, and downtime windows are bridged
    with free lily heals.
  * Presence of Mind: the haste window genuinely fits more GCDs (2.0 s cadence),
    and each PoM funds at most 3 Glare IVs.
  * Dia DoT over-refresh is overcap-safe; downtime lowers the ceiling; the
    entry lily state (sim_context) raises the ceiling symmetrically.

Run from python/:  python tests/test_whitemage_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.whitemage import data as wd
from jobs.whitemage import scoring as sc
from jobs.whitemage.simulator import (
    WhmContext,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic WHM cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high). The
    in-sim tincture marker (a negative pseudo-id) is filtered: real logs carry
    the pot as a buff, never as a cast."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-WHM pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no buff
    events -> no observed raid buffs / tincture windows)."""

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
            "title": "WHM fixture",
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
                    {"id": _SOURCE_ID, "name": "Test White Mage", "server": "T",
                     "type": "Player", "subType": "WhiteMage", "petOwner": None,
                     "gameID": 24},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("White Mage", client, "AbCd1234", 1,
                        ranking_name=None, label="whm-fixture")


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
    assert 130 <= pps <= 320, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_entry_lily_state_cold_start():
    """A fresh synthetic pull measures no carried lily gauge."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    assert st["entryLilies"] == 0, st["entryLilies"]
    assert st["entryBlood"] == 0, st["entryBlood"]


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


def test_lily_misery_balance():
    """Every Misery consumes a Blood Lily bloomed by exactly 3 lily spends, and
    the sim never spends a lily while the Blood Lily is already bloomed — so
    spends == 3*Misery + leftover nourishment (0-2)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    spends = c[wd.AFFLATUS_SOLACE] + c[wd.AFFLATUS_RAPTURE]
    leftover = spends - wd.BLOOD_LILY_CAP * c[wd.AFFLATUS_MISERY]
    assert 0 <= leftover <= wd.BLOOD_LILY_CAP - 1, \
        f"spends={spends} misery={c[wd.AFFLATUS_MISERY]} leftover={leftover}"
    assert c[wd.AFFLATUS_MISERY] >= 1


def test_glare_iv_funded_by_pom():
    """Each PoM grants 3 Sacred Sight stacks; Glare IV count never exceeds
    3 x PoM count (and reaches it except possibly the last window)."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    c = Counter(a for _, a in timeline)
    assert c[wd.PRESENCE_OF_MIND] >= 2
    assert c[wd.GLARE_IV] <= wd.SACRED_SIGHT_STACKS * c[wd.PRESENCE_OF_MIND]
    assert c[wd.GLARE_IV] >= wd.SACRED_SIGHT_STACKS * (c[wd.PRESENCE_OF_MIND] - 1)


def test_pom_haste_window_fits_more_gcds():
    """GCD cadence inside the 15 s PoM window is 2.0 s (vs 2.5 s outside), so
    the window fits >= 7 GCDs where an unhasted 15 s stretch fits 6."""
    timeline, _ = simulate_idealized(_DURATION_S, [])
    pom_t = next(t for t, a in timeline if a == wd.PRESENCE_OF_MIND)
    gcd_ids = {wd.GLARE_III, wd.GLARE_IV, wd.DIA, wd.AFFLATUS_MISERY,
               wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE}
    in_window = [t for t, a in timeline
                 if a in gcd_ids and pom_t <= t < pom_t + wd.POM_DURATION_S]
    assert len(in_window) >= 7, f"only {len(in_window)} GCDs in the PoM window"


def test_downtime_bridged_with_lily_heals():
    """A mid-fight downtime window gets bridged with free lily spends (the
    instant party-targeted heals a real WHM banks on every disconnect)."""
    timeline, _ = simulate_idealized_perfect(_DURATION_S, [(100.0, 130.0)])
    inside = [a for t, a in timeline if 100.0 <= t < 130.0]
    assert inside, "no casts inside the downtime window"
    assert all(a in (wd.AFFLATUS_SOLACE, wd.AFFLATUS_RAPTURE) for a in inside), \
        f"non-lily casts during downtime: {inside}"


def test_dia_dot_overrefresh_safe():
    """Over-refreshing Dia credits by time-to-next-cast (capped at 30 s), so two
    casts 5 s apart credit far less than two 30 s apart — never double-counted."""
    tight = sc._dia_dot_potency([(0.0, wd.DIA), (5.0, wd.DIA)], None)
    spaced = sc._dia_dot_potency([(0.0, wd.DIA), (30.0, wd.DIA)], None)
    assert tight < spaced
    one = sc._dia_dot_potency([(0.0, wd.DIA)], None)
    max_one = (wd.DIA_DOT_DURATION_S / wd.DIA_DOT_TICK_S) * wd.DIA_DOT_TICK_P
    assert one <= max_one + 1e-6


def test_entry_lily_context_raises_ceiling():
    """A carried (bloomed) Blood Lily lets the ceiling open with a free Misery
    — strictly more potency than the cold start (the symmetric M12S-P2
    continuation pattern)."""
    cold = sc.idealized_at_duration(_DURATION_S, [])
    carried = sc.idealized_at_duration(
        _DURATION_S, [], sim_context=WhmContext(entry_lilies=0, entry_blood=3))
    assert carried > cold, f"entry blood did not raise the ceiling: {carried} <= {cold}"


def test_measure_entry_lily_state():
    """Deepest-deficit inference: an early Misery with no prior spends implies a
    carried bloomed Blood Lily; early spends beyond the timer's accrual imply
    carried lilies; a cold-start stream measures 0/0."""
    early_misery = [(2.0, wd.GLARE_III), (4.5, wd.AFFLATUS_MISERY)]
    assert sc.measure_entry_lily_state(early_misery) == (0, 3)
    early_spends = [(1.0, wd.AFFLATUS_SOLACE), (3.5, wd.AFFLATUS_SOLACE)]
    lil, _b = sc.measure_entry_lily_state(early_spends)
    assert lil == 2, lil
    cold = [(1.0, wd.DIA), (25.0, wd.AFFLATUS_SOLACE),
            (45.0, wd.AFFLATUS_SOLACE), (65.0, wd.AFFLATUS_SOLACE),
            (67.5, wd.AFFLATUS_MISERY)]
    assert sc.measure_entry_lily_state(cold) == (0, 0)


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all white mage sim tests passed")


if __name__ == "__main__":
    main()
