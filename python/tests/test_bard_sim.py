"""Bard scoring + simulator invariants (network-free).

Mirrors test_samurai_sim.py for the fifteenth job (the first song-cycle job):

  * Pipeline doesn't crash — `analyze_pull('Bard', ...)` runs every aspect via
    the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock budget.
  * Raging Strikes windows amp covered casts (x1.15) and nothing outside them.
  * Barrage arms exactly ONE tripled Refulgent Arrow (3x280, live-verified as
    three separate hits).
  * Radiant Encore is priced by the Coda the granting Finale consumed (700/1100).
  * DoT over-refresh is overcap-safe (time-to-next-application, capped 45s) and
    Iron Jaws refreshes only ACTIVE DoTs.
  * The song cycle holds (WM -> MB -> AP order; Encore == Finale casts;
    Resonant == Barrage casts) and the Army's Paeon haste + Muse windows shape
    `gcd_duration`.
  * The measured budgets (sim_context) are spent EXACTLY (the ceiling matches
    the player's RNG-resource counts) and larger budgets raise the ceiling.
  * Downtime lowers the ceiling; the AP windows are excluded from the gear-GCD
    inference.

Run from python/:  python tests/test_bard_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.bard import data as bd
from jobs.bard import scoring as sc
from jobs.bard.simulator import (
    BardCtx,
    BardRotationModel,
    SimParams,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)
from jobs.bard.scoring import BardScoringAspect, score_delivered_potency

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic BRD cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-BRD pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no aura events
    -> no observed tincture/raid windows)."""

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
            "title": "BRD fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Bard", "server": "T",
                     "type": "Player", "subType": "Bard", "petOwner": None,
                     "gameID": 23},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Bard", client, "AbCd1234", 1,
                        ranking_name=None, label="brd-fixture")


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
    assert 150 <= pps <= 320, f"p/sec out of band: {pps:.1f}"
    ratio = delivered / ideal if ideal > 0 else 0
    assert ratio <= 1.005, f"delivered {delivered:.0f} > ideal {ideal:.0f}"


def test_buff_scenarios_present():
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("idealized_observed", "idealized_master", "idealized_lenient",
                "delivered_observed", "enabler_net_values"):
        assert key in st, f"missing scoring key {key}"


def test_budget_state_present():
    """The measured budgets ride the Scoring state (the sidecar's lenient /
    timeline sims spend the same counts)."""
    mr = _run_pipeline()
    st = mr.aspects["Scoring"].state
    for key in ("refulgentBudget", "ppBudget", "apexBudget", "blastBudget",
                "heartbreakBudget", "sim_context"):
        assert key in st, f"missing {key}"
    assert st["ppBudget"] > 0 and st["heartbreakBudget"] > 0


# --- Simulator invariants --------------------------------------------------

def test_sim_monotonicity():
    s_d = score_delivered_potency(simulate_idealized(_DURATION_S, [])[0])
    s_o = score_delivered_potency(simulate_idealized_optimal(_DURATION_S, [])[0])
    s_p = score_delivered_potency(simulate_idealized_perfect(_DURATION_S, [])[0])
    assert s_o >= s_d - 1e-6, f"optimal {s_o} < default {s_d}"
    assert s_p >= s_o - 1e-6, f"perfect {s_p} < optimal {s_o}"


def test_idealized_beats_degraded_delivered():
    timeline, _ = simulate_idealized(_DURATION_S, [])
    degraded = timeline[::2]  # drop half the casts
    ideal = sc.idealized_at_duration(_DURATION_S, [])
    delivered = score_delivered_potency(degraded)
    assert ideal >= delivered


def test_perfect_under_wallclock_budget():
    start = time.monotonic()
    simulate_idealized_perfect(_DURATION_S, [])
    assert time.monotonic() - start <= 20.0


def test_raging_strikes_window():
    """A cast inside the derived Raging Strikes window is amped x1.15; one
    outside is not."""
    inside = score_delivered_potency([(0.0, bd.RAGING_STRIKES), (1.0, bd.BURST_SHOT)])
    assert abs(inside - bd.POTENCIES[bd.BURST_SHOT] * bd.RAGING_STRIKES_MULT) < 1e-6
    outside = score_delivered_potency([(0.0, bd.RAGING_STRIKES), (25.0, bd.BURST_SHOT)])
    assert abs(outside - bd.POTENCIES[bd.BURST_SHOT]) < 1e-6


def test_barrage_triples_one_refulgent():
    """The first Refulgent after Barrage lands 3 hits; the next is normal."""
    one = score_delivered_potency([(0.0, bd.BARRAGE), (1.0, bd.REFULGENT_ARROW)])
    assert abs(one - 3 * bd.POTENCIES[bd.REFULGENT_ARROW]) < 1e-6
    two = score_delivered_potency([(0.0, bd.BARRAGE), (1.0, bd.REFULGENT_ARROW),
                                   (3.5, bd.REFULGENT_ARROW)])
    assert abs(two - 4 * bd.POTENCIES[bd.REFULGENT_ARROW]) < 1e-6


def test_encore_coda_tiers():
    """Radiant Encore is priced by the Coda its granting Finale consumed: the
    opener 1-Coda Encore is 700, a 3-Coda one 1100 (live-verified)."""
    opener = score_delivered_potency([
        (0.0, bd.WANDERERS_MINUET), (5.0, bd.RADIANT_FINALE),
        (7.0, bd.RADIANT_ENCORE)])
    assert abs(opener - bd.ENCORE_POTENCY_BY_CODA[1]) < 1e-6, opener
    full = score_delivered_potency([
        (0.0, bd.WANDERERS_MINUET), (40.0, bd.MAGES_BALLAD),
        (80.0, bd.ARMYS_PAEON), (110.0, bd.RADIANT_FINALE),
        (112.0, bd.RADIANT_ENCORE)])
    assert abs(full - bd.ENCORE_POTENCY_BY_CODA[3]) < 1e-6, full


def test_dot_overrefresh_safe_and_ij_rules():
    """DoT applications credit time-to-next (capped 45s): a tight double-apply
    credits less than a spaced one; Iron Jaws refreshes only an ACTIVE dot."""
    tick = bd.STORMBITE_DOT_TICK_P
    full_dot = bd.DOT_DURATION_S / bd.DOT_TICK_S * tick
    tight = score_delivered_potency([(0.0, bd.STORMBITE), (5.0, bd.STORMBITE)])
    spaced = score_delivered_potency([(0.0, bd.STORMBITE), (45.0, bd.STORMBITE)])
    assert tight < spaced
    # IJ past the dot's 45s expiry re-applies NOTHING (only the base 100 lands).
    expired = score_delivered_potency([(0.0, bd.STORMBITE), (50.0, bd.IRON_JAWS)])
    assert abs(expired - (bd.POTENCIES[bd.STORMBITE] + full_dot
                          + bd.POTENCIES[bd.IRON_JAWS])) < 1e-6
    # An in-window IJ extends the dot: strictly more DoT than the expired case.
    refreshed = score_delivered_potency([(0.0, bd.STORMBITE), (40.0, bd.IRON_JAWS)])
    assert refreshed > expired


def test_song_cycle_structure():
    """Songs cycle WM -> MB -> AP; every Finale yields one Encore; every Barrage
    one Resonant; the DoTs are applied once and maintained via Iron Jaws."""
    timeline, _ = simulate_idealized(634.0, [])
    casts = [(t, a) for t, a in timeline if a > 0]
    c = Counter(a for _, a in casts)
    songs = [a for _, a in casts if a in bd.SONG_ORDER]
    assert songs[:3] == list(bd.SONG_ORDER), songs[:3]
    assert c[bd.RADIANT_ENCORE] == c[bd.RADIANT_FINALE]
    assert c[bd.RESONANT_ARROW] == c[bd.BARRAGE]
    assert c[bd.STORMBITE] == 1 and c[bd.CAUSTIC_BITE] == 1
    assert c[bd.IRON_JAWS] >= 10
    # Song counts ~ one per 40s, WM leading.
    assert c[bd.WANDERERS_MINUET] >= c[bd.ARMYS_PAEON] >= 4


def test_budgets_spent_exactly():
    """The sim spends exactly the measured budgets (the ceiling matches the
    player's RNG-resource counts; the Barrage-armed Refulgent is free)."""
    ctx = BardCtx(refulgent_budget=20, pp_budget=10, apex_budget=4,
                  blast_budget=4, hb_budget=30)
    timeline, _ = simulate_idealized(300.0, [], sim_context=ctx)
    c = Counter(a for _, a in timeline)
    assert c[bd.REFULGENT_ARROW] == 20 + c[bd.BARRAGE], \
        f"refulgent {c[bd.REFULGENT_ARROW]} vs budget 20 + {c[bd.BARRAGE]} armed"
    assert c[bd.PITCH_PERFECT] == 10
    assert c[bd.APEX_ARROW] == 4
    assert c[bd.BLAST_ARROW] == 4
    assert c[bd.HEARTBREAK_SHOT] == 30


def test_budget_raises_ceiling():
    low = sc.idealized_at_duration(300.0, [], sim_context=BardCtx(
        refulgent_budget=10, pp_budget=5, apex_budget=2, blast_budget=2,
        hb_budget=20))
    high = sc.idealized_at_duration(300.0, [], sim_context=BardCtx(
        refulgent_budget=30, pp_budget=14, apex_budget=5, blast_budget=5,
        hb_budget=40))
    assert high > low, f"bigger budgets did not raise the ceiling: {high} <= {low}"


def test_ap_haste_and_muse_gcd():
    """gcd_duration: 2.5 base; ramping to x0.84 at 4 Army's Paeon stacks; x0.88
    inside the 10s Army's Muse tail."""
    model = BardRotationModel()
    st = model.init_state()
    params = SimParams()
    st.song = bd.ARMYS_PAEON
    st.song_start = 0.0
    st.t = 1.0     # 0 stacks yet
    assert abs(model.gcd_duration(st, bd.BURST_SHOT, params) - 2.5) < 1e-9
    st.t = 13.0    # 4 stacks (3s each)
    assert abs(model.gcd_duration(st, bd.BURST_SHOT, params) - 2.5 * 0.84) < 1e-9
    st.song = bd.WANDERERS_MINUET
    st.muse_end = 20.0
    st.t = 15.0
    assert abs(model.gcd_duration(st, bd.BURST_SHOT, params) - 2.5 * bd.MUSE_MULT) < 1e-9
    st.t = 25.0
    assert abs(model.gcd_duration(st, bd.BURST_SHOT, params) - 2.5) < 1e-9


def test_gcd_inference_excludes_ap_windows():
    """The Army's Paeon (+10s Muse) stretch is excluded from the gear-GCD
    inference (self-haste, not gear — the BLM Ley Lines rule)."""
    aspect = BardScoringAspect()
    casts = [(0.0, bd.WANDERERS_MINUET), (43.5, bd.MAGES_BALLAD),
             (83.5, bd.ARMYS_PAEON), (120.0, bd.WANDERERS_MINUET)]
    excl = aspect.gcd_inference_exclusions(casts)
    assert len(excl) == 1
    s, e = excl[0]
    assert abs(s - 83.5) < 1e-9
    assert abs(e - 130.0) < 1e-9   # min(next song 120, 83.5+45) + 10s Muse


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_songs_roll_through_downtime():
    """The song cycle keeps rolling through a boss-untargetable gap (songs are
    targetless), so the post-downtime cadence is intact."""
    timeline, _ = simulate_idealized(300.0, [(60.0, 110.0)])
    song_ts = [t for t, a in timeline if a in bd.SONG_ORDER]
    in_window = [t for t in song_ts if 60.0 <= t < 110.0]
    assert in_window, "no song cast inside the downtime window"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all bard sim tests passed")


if __name__ == "__main__":
    main()
