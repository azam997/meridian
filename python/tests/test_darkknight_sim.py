"""Dark Knight scoring + simulator invariants (network-free).

Mirrors test_summoner_sim.py (+ the GNB tank extras) for the eighteenth job (the
fourth tank, the second pet-fold job):

  * Pipeline doesn't crash — `analyze_pull('Dark Knight', ...)` runs every aspect
    via the registry against a synthetic pull (no FFLogs network).
  * delivered_potency / fight duration stays in a sane p/sec band, and
    idealized@own_duration >= delivered (the upper-bound invariant).
  * perfect >= optimal >= default (strict-upgrade chain) within a wall-clock
    budget.
  * The folds are pure table potency (Living Shadow carries Esteem's 5-hit
    sequence, Salted Earth its 6 ticks); Esteem's own ids score zero.
  * Every DRK id resolves hermetically from ability_metadata.BUNDLED with the
    OGCD_IDS truth (Esteem ids flagged oGCD).
  * The rotation structure holds on a long greedy run: Delirium ~60s cadence,
    Living Shadow ~120s, the chain 3-per-Delirium, Disesteem 1:1 with Living
    Shadow inside Scorn, Salt and Darkness 1:1 inside the Salted Earth patch,
    the Blood and MP ledgers close, Darkside uptime stays high.
  * Darkside extend-and-cap semantics; a grant never amps its own cast.
  * The Living Shadow downtime hold fires (never at the fight tail — the fold
    symmetry rule); downtime lowers the ceiling; entry Blood raises it.

Run from python/:  python tests/test_darkknight_sim.py
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import analyze_pull
from jobs.darkknight import data as dd
from jobs.darkknight import scoring as sc
from jobs.darkknight.scoring import darkside_stats, score_delivered_potency
from jobs.darkknight.simulator import (
    DarkKnightRotationModel,
    SimParams,
    simulate_idealized,
    simulate_idealized_optimal,
    simulate_idealized_perfect,
)

_DURATION_S = 360.0
_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900

_CHAIN_IDS = (dd.SCARLET_DELIRIUM, dd.COMEUPPANCE, dd.TORCLEAVER)


def _synthetic_casts(duration_s: float) -> list[dict]:
    """A realistic DRK cast stream = the default-sim timeline, as FFLogs cast
    events (the 'delivered' run — near-ideal, so efficiency is high)."""
    timeline, _ = simulate_idealized(duration_s, [])
    return [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
             "sourceID": _SOURCE_ID, "abilityGameID": aid}
            for t, aid in timeline if t >= 0 and aid > 0]


class MockClient:
    """Serves a synthetic single-DRK pull. Casts come from the sim; every other
    stream is empty (boss targetable throughout -> zero downtime; no aura events
    -> no observed tincture/raid windows; no pet actors -> nothing to fetch —
    the Esteem fold rides the Living Shadow cast id)."""

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
            "title": "DRK fixture",
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
                    {"id": _SOURCE_ID, "name": "Test Dark Knight", "server": "T",
                     "type": "Player", "subType": "DarkKnight", "petOwner": None,
                     "gameID": 42},
                    {"id": _BOSS_ID, "name": "Boss", "server": "T", "type": "NPC",
                     "subType": "Boss", "petOwner": None, "gameID": 1},
                ],
                "abilities": [],
            },
        }


def _run_pipeline():
    client = MockClient(_synthetic_casts(_DURATION_S))
    return analyze_pull("Dark Knight", client, "AbCd1234", 1,
                        ranking_name=None, label="drk-fixture")


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
    assert 240 <= pps <= 380, f"p/sec out of band: {pps:.1f}"
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
    assert time.monotonic() - start <= 30.0


def test_folds_are_pure_table_potency():
    """Living Shadow carries Esteem's folded 5-hit sequence, Salted Earth its
    6 ticks — plain table lookups, symmetric with the sim's incremental score.
    Esteem's own damage ids score zero (they never appear in a cast stream)."""
    assert dd.POTENCIES[dd.LIVING_SHADOW] == 2450   # 420*3 + 570 + 620
    assert dd.POTENCIES[dd.SALTED_EARTH] == 300     # 6 ticks x 50
    for aid in (dd.LIVING_SHADOW, dd.SALTED_EARTH, dd.CARVE_AND_SPIT):
        got = score_delivered_potency([(0.0, aid)])
        assert abs(got - dd.POTENCIES[aid]) < 1e-6, (aid, got)
    for pid in dd.ESTEEM_IDS:
        assert score_delivered_potency([(0.0, pid)]) == 0.0, pid


def test_ability_metadata_bundled():
    """Every DRK id resolves from ability_metadata.BUNDLED with the OGCD_IDS
    oGCD flag — under the hermetic stub (no XIVAPI). The Clipping aspect and the
    GCD-speed inference / demonstrated-cadence anchor skip any cast whose
    metadata is None, so a missing entry silently blanks those paths."""
    from jobs._core.ability_metadata import BUNDLED, get_metadata
    all_ids = set(dd.POTENCIES) | set(dd.OGCD_IDS) | set(dd.DEFENSIVE_IDS)
    for aid in sorted(all_ids):
        assert aid in BUNDLED, f"DRK id {aid} missing from ability_metadata.BUNDLED"
        meta = get_metadata(aid)
        assert meta is not None and meta.name, f"id {aid} did not resolve"
        expected_ogcd = aid in dd.OGCD_IDS
        assert meta.is_ogcd == expected_ogcd, \
            f"{meta.name} ({aid}): is_ogcd={meta.is_ogcd}, OGCD_IDS says {expected_ogcd}"


def test_darkside_semantics():
    """Extend-and-cap from the cast times; a grant never amps its own cast."""
    e, hs = dd.EDGE_OF_SHADOW, dd.HARD_SLASH
    # The granting Edge itself is unbuffed; the following GCD is amped.
    got = score_delivered_potency([(0.0, e), (1.0, hs)])
    assert abs(got - (460 + 300 * 1.10)) < 1e-6, got
    # A cast before any grant is unbuffed.
    got = score_delivered_potency([(0.0, hs), (1.0, e)])
    assert abs(got - (300 + 460)) < 1e-6, got
    # Cap: three quick Edges reach t+30 / +30 capped at t+60 -> the window ends
    # at 70, so a cast at 69 is amped and one at 71 is not.
    tl = [(0.0, e), (5.0, e), (10.0, e)]
    amped = score_delivered_potency(tl + [(69.0, hs)])
    unamped = score_delivered_potency(tl + [(71.0, hs)])
    base = score_delivered_potency(tl)
    assert abs((amped - base) - 300 * 1.10) < 1e-6
    assert abs((unamped - base) - 300) < 1e-6
    # darkside_stats: mid-fight drop is priced; the pre-first-Edge opener isn't.
    stats = darkside_stats([(1.0, hs), (2.0, e), (80.0, hs), (81.0, e)], 100.0)
    assert stats["darkside_lost_potency"] > 0
    assert stats["darkside_uptime_pct"] < 100.0


def test_rotation_structure():
    """Cooldown cadences + the Blood/MP economies on a long greedy run."""
    dur = 634.0
    timeline, _ = simulate_idealized(dur, [])
    casts = [(t, a) for t, a in timeline if a > 0]
    c = Counter(a for _, a in casts)

    # Cadences: Delirium ~60s, Living Shadow ~120s (cooldown-locked greedy).
    for aid, recast in ((dd.DELIRIUM, 60.0), (dd.LIVING_SHADOW, 120.0),
                        (dd.SALTED_EARTH, 90.0), (dd.CARVE_AND_SPIT, 60.0)):
        ts = [t for t, a in casts if a == aid]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        assert all(recast - 0.01 <= g <= recast + 8.0 for g in gaps), (aid, gaps)
        assert len(ts) >= int(dur // (recast + 6.0)), (aid, len(ts))

    # The chain: 3 per Delirium (tail may truncate one window).
    n_chain = sum(c[a] for a in _CHAIN_IDS)
    assert 3 * c[dd.DELIRIUM] - 3 <= n_chain <= 3 * c[dd.DELIRIUM]
    # Chain GCDs only while the 15s buff is live.
    del_ts = [t for t, a in casts if a == dd.DELIRIUM]
    for t, a in casts:
        if a in _CHAIN_IDS:
            assert any(d <= t <= d + dd.DELIRIUM_DURATION_S for d in del_ts), (t, a)

    # Disesteem 1:1 with Living Shadow, inside Scorn.
    ls_ts = [t for t, a in casts if a == dd.LIVING_SHADOW]
    dis_ts = [t for t, a in casts if a == dd.DISESTEEM]
    assert abs(len(dis_ts) - len(ls_ts)) <= 1
    for t in dis_ts:
        assert any(l <= t <= l + dd.SCORN_DURATION_S for l in ls_ts), t

    # Salt and Darkness 1:1 with Salted Earth, inside the 15s patch.
    se_ts = [t for t, a in casts if a == dd.SALTED_EARTH]
    snd_ts = [t for t, a in casts if a == dd.SALT_AND_DARKNESS]
    assert abs(len(snd_ts) - len(se_ts)) <= 1
    for t in snd_ts:
        assert any(s <= t <= s + 15.0 for s in se_ts), t

    # Blood ledger: spends never exceed generation (cold start).
    blood_gen = 20 * c[dd.SOULEATER] + 10 * n_chain
    blood_spend = 50 * (c[dd.BLOODSPILLER] + c[dd.QUIETUS])
    assert blood_spend <= blood_gen, (blood_spend, blood_gen)

    # MP ledger: Edges never exceed the income budget — and the sim doesn't
    # waste meaningful MP either (the ceiling should spend what it earns).
    edges = c[dd.EDGE_OF_SHADOW] + c[dd.FLOOD_OF_SHADOW]
    budget = (dd.MP_MAX + dd.MP_PER_TICK * dur / dd.MP_TICK_S
              + dd.COMBO_MP_GRANT * (c[dd.SYPHON_STRIKE] + c[dd.STALWART_SOUL])
              + dd.CARVE_MP_GRANT * c[dd.CARVE_AND_SPIT]
              + (dd.BLOOD_WEAPON_MP + dd.CHAIN_RESTORE_MP) * n_chain) / dd.EDGE_MP_COST
    assert edges <= budget + 1e-9, (edges, budget)
    assert edges >= int(budget) - 1, (edges, budget)

    # Combo integrity: the basic combo cycles cleanly.
    assert c[dd.HARD_SLASH] >= c[dd.SYPHON_STRIKE] >= c[dd.SOULEATER] \
        >= c[dd.HARD_SLASH] - 1

    # Darkside: the ceiling holds near-full uptime.
    stats = darkside_stats(casts, dur)
    assert stats["darkside_uptime_pct"] >= 95.0, stats


def test_ls_downtime_hold_and_tail_escape():
    """A ready Living Shadow is held when a gap would eat Esteem's window — but
    never at the fight tail (the fold symmetry rule: a player's full-credit
    tail summon must be matchable)."""
    model = DarkKnightRotationModel()
    st = model.init_state()
    st.fight_duration_s = 300.0
    st.t = 100.0
    st.downtime_windows = [(108.0, 130.0)]
    assert model._ls_burns_into_downtime(st)
    assert model.pick_ogcd(st, SimParams()) != dd.LIVING_SHADOW
    # Same geometry at the fight tail -> fire anyway.
    st2 = model.init_state()
    st2.fight_duration_s = 300.0
    st2.t = 285.0
    st2.downtime_windows = [(293.0, 299.0)]
    assert not model._ls_burns_into_downtime(st2)
    assert model.pick_ogcd(st2, SimParams()) == dd.LIVING_SHADOW


def test_combo_lost_across_long_downtime():
    model = DarkKnightRotationModel()
    st = model.init_state()
    st.t = 10.0
    model.apply_cast(st, dd.HARD_SLASH)
    st.last_gcd_t = 10.0
    assert st.basic_combo_step == 1
    model.on_downtime_window(st, 12.0, 50.0)   # 40s > the 30s combo timer
    assert st.basic_combo_step == 0


def test_downtime_lowers_ceiling():
    full = sc.idealized_at_duration(_DURATION_S, [])
    with_dt = sc.idealized_at_duration(_DURATION_S, [(120.0, 160.0)])
    assert with_dt < full, f"downtime did not lower the ceiling: {with_dt} >= {full}"


def test_entry_blood_raises_ceiling():
    from jobs._core.entry_gauge import EntryState
    cold = score_delivered_potency(simulate_idealized_perfect(120.0, [])[0])
    seeded = score_delivered_potency(simulate_idealized_perfect(
        120.0, [], sim_context=EntryState(gauges=(("blood", 100),)))[0])
    assert seeded >= cold - 1e-6, (seeded, cold)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("all darkknight sim tests passed")


if __name__ == "__main__":
    main()
