"""White Mage mit-plan locked-GCD integration (network-free).

Covers the WHM half of the heal-lock feature (the engine scheduler itself is
pinned by test_engine_locks.py):

  * Locked Rapture spends a lily and nourishes the Blood Lily (Misery economy
    stays coherent under locks).
  * A Rapture lock with no lily at the deadline substitutes Medica III.
  * A lock window fully inside downtime is free — the locked ceiling equals
    the unlocked one.
  * An uptime Medica III lock lowers the ceiling by about one Glare per cast.
  * Locked ceilings are monotone: locked <= unlocked, more locks <= fewer.
  * HealLockContext changes the scoring cache key; a plain WhmContext key is
    unchanged (refs never collide with a locked run).
  * The improvements contributor tolerances: plan-conformant / reordered
    heals never card; spend beyond plan+slack prices the excess.

Run from python/:  python tests/test_whitemage_locks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.heal_locks import HealLockContext, LockedGcdWindow
from jobs.whitemage import data as wd
from jobs.whitemage import scoring as sc
from jobs.whitemage.simulator import (
    WhmContext,
    simulate_idealized,
    simulate_idealized_perfect,
)

MEDICA_III = wd.MEDICA_III
RAPTURE = wd.AFFLATUS_RAPTURE
SOLACE = wd.AFFLATUS_SOLACE
MISERY = wd.AFFLATUS_MISERY

_D = 300.0


def _lk(aid, start, end, count, cast=2.5):
    return LockedGcdWindow(ability_id=aid, start_s=start, end_s=end,
                           count=count, cast_s=cast)


def _ctx(*locks):
    return HealLockContext(locks=tuple(locks))


def _casts(timeline, aid):
    return [t for t, a in timeline if a == aid]


def _score(timeline):
    return sc.score_delivered_potency(list(timeline))


# --- Lock mechanics under the WHM model ---------------------------------------

def test_locked_medica_lowers_ceiling_by_about_a_glare() -> None:
    base_tl, _ = simulate_idealized(_D, [])
    lock_tl, _ = simulate_idealized(_D, [], sim_context=_ctx(
        _lk(MEDICA_III, 100.0, 130.0, 1)))
    meds = _casts(lock_tl, MEDICA_III)
    assert len(meds) == 1 and 100.0 <= meds[0] < 130.0, lock_tl
    drop = _score(base_tl) - _score(lock_tl)
    # One uptime heal GCD displaces about one filler Glare (350p); the exact
    # figure shifts with Dia/Misery re-timing, so allow a generous band.
    assert 100.0 <= drop <= 700.0, drop


def test_locked_rapture_satisfied_by_lily_spend() -> None:
    """A Rapture lock is a lily-spend obligation: the sim's own in-window
    Solace (the forced at-cap spend) retires it via `lock_satisfiers` — no
    duplicate cast, no Medica III substitute — and the lily economy stays
    coherent: every Misery still needs 3 spends."""
    tl, _ = simulate_idealized(_D, [], sim_context=_ctx(
        _lk(RAPTURE, 60.0, 90.0, 1)))
    spends_in_window = [t for t in (_casts(tl, SOLACE) + _casts(tl, RAPTURE))
                        if 60.0 <= t < 90.0]
    assert spends_in_window, tl
    assert not _casts(tl, MEDICA_III), tl
    base_tl, _ = simulate_idealized(_D, [])
    assert abs(_score(base_tl) - _score(tl)) < 1e-6   # covered for free
    spends = len(_casts(tl, SOLACE)) + len(_casts(tl, RAPTURE))
    miseries = len(_casts(tl, MISERY))
    assert spends >= 3 * miseries, (spends, miseries)


def test_locked_rapture_forced_when_no_voluntary_spend() -> None:
    """A Rapture owed in a window where the sim would NOT voluntarily spend
    (lilies banked but below cap) fires as a real RAPTURE at the deadline."""
    tl, _ = simulate_idealized(_D, [], sim_context=_ctx(
        _lk(RAPTURE, 25.0, 42.0, 1)))
    raptures = [t for t in _casts(tl, RAPTURE) if 25.0 <= t < 42.0]
    solaces = [t for t in _casts(tl, SOLACE) if 25.0 <= t < 42.0]
    assert raptures or solaces, tl
    assert not [t for t in _casts(tl, MEDICA_III) if t < 42.0], tl


def test_rapture_lock_without_lily_substitutes_medica() -> None:
    """A Rapture owed before the first lily can exist (accrual starts at 20s)
    is paid with the hardcast Medica III overflow instead."""
    tl, _ = simulate_idealized(_D, [], sim_context=_ctx(
        _lk(RAPTURE, 0.0, 15.0, 1)))
    assert len(_casts(tl, MEDICA_III)) == 1, tl
    assert not [t for t in _casts(tl, RAPTURE) if t < 15.0], tl


def test_lock_inside_downtime_is_free() -> None:
    downtime = [(100.0, 120.0)]
    base_tl, _ = simulate_idealized(_D, downtime)
    lock_tl, _ = simulate_idealized(_D, downtime, sim_context=_ctx(
        _lk(MEDICA_III, 100.0, 121.0, 1)))
    meds = _casts(lock_tl, MEDICA_III)
    assert len(meds) == 1 and 100.0 <= meds[0] < 120.0, lock_tl
    assert abs(_score(base_tl) - _score(lock_tl)) < 1e-6, (
        _score(base_tl), _score(lock_tl))


def test_locked_ceiling_monotone() -> None:
    one = _ctx(_lk(MEDICA_III, 80.0, 110.0, 1))
    three = _ctx(_lk(MEDICA_III, 80.0, 110.0, 1),
                 _lk(MEDICA_III, 150.0, 180.0, 1),
                 _lk(MEDICA_III, 220.0, 250.0, 1))
    base, _ = simulate_idealized_perfect(_D, [])
    s_base = _score(base)
    tl1, _ = simulate_idealized_perfect(_D, [], sim_context=one)
    tl3, _ = simulate_idealized_perfect(_D, [], sim_context=three)
    assert _score(tl1) <= s_base + 1e-6
    assert _score(tl3) <= _score(tl1) + 1e-6
    assert len(_casts(tl3, MEDICA_III)) == 3


def test_lock_context_composes_with_entry_state() -> None:
    """HealLockContext nests around the phase-continuation WhmContext — both
    survive the unwrap (entry lilies fund an early Misery AND the lock fires)."""
    ctx = HealLockContext(locks=(_lk(MEDICA_III, 40.0, 70.0, 1),),
                          inner=WhmContext(entry_lilies=3, entry_blood=2))
    tl, _ = simulate_idealized(_D, [], sim_context=ctx)
    assert len(_casts(tl, MEDICA_III)) == 1
    first_misery = min(_casts(tl, MISERY))
    cold, _ = simulate_idealized(_D, [])
    assert first_misery < min(_casts(cold, MISERY)), first_misery


# --- Cache-key separation -------------------------------------------------------

def test_cache_keys_differ_with_locks() -> None:
    keys = sc._sim_cache_keys
    plain = keys(_D, [], None, WhmContext(1, 2))
    locked = keys(_D, [], None, HealLockContext(
        locks=(_lk(MEDICA_III, 10.0, 30.0, 1),), inner=WhmContext(1, 2)))
    assert plain != locked
    # The historic key shapes are untouched.
    assert plain == keys(_D, [], None, WhmContext(1, 2))
    assert keys(_D, [], None, None) == keys(_D, [], None, None)


# --- Pipeline: analyze_pull with staged locks --------------------------------------

_FIGHT_START_MS = 1_000_000
_SOURCE_ID = 1
_BOSS_ID = 900


class _MockClient:
    """Minimal synthetic single-WHM pull (mirrors test_whitemage_sim's mock)."""

    def __init__(self, casts: list[dict], duration_s: float):
        self._casts = casts
        self._duration_s = duration_s

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
        end_ms = _FIGHT_START_MS + int(self._duration_s * 1000)
        return {
            "title": "WHM lock fixture",
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


def _analyze(extra_report=None):
    from jobs import analyze_pull
    timeline, _ = simulate_idealized(_D, [])
    casts = [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
              "sourceID": _SOURCE_ID, "abilityGameID": aid}
             for t, aid in timeline if t >= 0 and aid > 0]
    return analyze_pull("White Mage", _MockClient(casts, _D), "AbCd1234", 1,
                        ranking_name=None, label="whm-lock-fixture",
                        extra_report=extra_report)


def test_pipeline_locked_run() -> None:
    """analyze_pull with staged `__heal_locks__`: the scoring state carries the
    meta keys, the strict ceiling drops vs the unlocked run, delivered is
    untouched, and an unlocked run has none of the keys (refs shape)."""
    payload = {
        "locks": (_lk(MEDICA_III, 80.0, 110.0, 1),
                  _lk(MEDICA_III, 200.0, 230.0, 1)),
        "count": 2, "potency": 700.0, "plan_costed_count": 2,
        "comp": ["Sage", "White Mage", "Paladin", "Dark Knight",
                 "Samurai", "Dragoon", "Bard", "Pictomancer"],
        "source": "pull", "warnings": [],
    }
    unlocked = _analyze()
    locked = _analyze(extra_report={"__heal_locks__": payload})
    su, sl = unlocked.aspects["Scoring"].state, locked.aspects["Scoring"].state
    assert "heal_locks_applied" not in su
    assert sl["heal_locks_applied"] is True
    assert sl["heal_lock_count"] == 2
    assert sl["heal_lock_costed_count"] == 2
    assert sl["mit_plan_comp_source"] == "pull"
    assert abs(sl["delivered_potency"] - su["delivered_potency"]) < 1e-6
    assert sl["idealized_strict"] < su["idealized_strict"], (
        sl["idealized_strict"], su["idealized_strict"])
    # Both Medica III casts appear on the locked ceiling's own timeline.
    tl = sc.perfect_sim_timeline(_D, [], None, sl["sim_context"])
    assert len([t for t, a in tl if a == MEDICA_III]) == 2, tl


def test_pipeline_kill_over_heal_credits_and_cards() -> None:
    """A KILL where the player over-heals beyond plan+slack: the reconcile lifts
    the ceiling's healing tax to plan+slack — credited at the player's OWN cast
    times — and the shared card prices the remainder, located at the beyond-plan
    casts (the ones farthest from a mechanic)."""
    from jobs import analyze_pull
    from jobs.whitemage.improvements import improvements_from_heal_gcds
    timeline, _ = simulate_idealized(_D, [])
    casts = [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
              "sourceID": _SOURCE_ID, "abilityGameID": aid}
             for t, aid in timeline if t >= 0 and aid > 0]
    heal_ts = [40.0, 80.0, 120.0, 160.0, 200.0, 230.0, 260.0]      # 7 Medica III
    casts += [{"timestamp": _FIGHT_START_MS + int(t * 1000), "type": "cast",
               "sourceID": _SOURCE_ID, "abilityGameID": MEDICA_III} for t in heal_ts]
    casts.sort(key=lambda c: c["timestamp"])
    payload = {
        "locks": (_lk(MEDICA_III, 80.0, 110.0, 1), _lk(MEDICA_III, 200.0, 230.0, 1)),
        "count": 2, "potency": 700.0, "plan_costed_count": 2,
        "comp": ["Sage", "White Mage", "Paladin", "Dark Knight",
                 "Samurai", "Dragoon", "Bard", "Pictomancer"],
        "source": "pull", "warnings": [],
    }
    res = analyze_pull("White Mage", _MockClient(casts, _D), "AbCd1234", 1,
                       ranking_name=None, label="whm-overheal",
                       extra_report={"__heal_locks__": payload})
    st = res.aspects["Scoring"].state
    # plan 2 + slack max(2, ceil(0.2*2)) = 2 -> credit 4, card the other 3.
    assert st["heal_lock_costed_count"] == 4
    assert len(st["heal_lock_excess"]) == 3
    # The ceiling now casts the 4 CREDITED heals (not the plan's 2).
    tl = sc.perfect_sim_timeline(_D, [], None, st["sim_context"])
    assert len([t for t, a in tl if a == MEDICA_III]) == 4, tl
    cards = improvements_from_heal_gcds(res)
    assert len(cards) == 1 and len(cards[0].children) == 3


# --- Improvements card (renders the reconciled excess) --------------------------
# The tolerance itself (which casts become excess) now lives in
# heal_locks.reconcile_heal_budget — see test_heal_locks.py::test_reconcile_*.
# The card is pure rendering of the `heal_lock_excess` the reconcile produced.

class _FakeRun:
    def __init__(self, state: dict):
        self.aspects = {"Scoring": type("A", (), {"state": state})()}


def _carded_state(excess, credited: int = 4, filler: float = 350.0) -> dict:
    return {"heal_locks_applied": True, "heal_lock_costed_count": credited,
            "heal_lock_filler_potency": filler,
            "heal_lock_excess": [[float(t), int(a)] for t, a in excess]}


def test_improvements_unlocked_run_no_card() -> None:
    from jobs.whitemage.improvements import improvements_from_heal_gcds
    assert improvements_from_heal_gcds(_FakeRun({})) == []


def test_improvements_no_excess_no_card() -> None:
    from jobs.whitemage.improvements import improvements_from_heal_gcds
    # A conformant / out-of-order / prog pull reconciles to zero excess -> no card.
    assert improvements_from_heal_gcds(_FakeRun(_carded_state([]))) == []


def test_improvements_excess_priced() -> None:
    from jobs.whitemage.improvements import improvements_from_heal_gcds
    state = _carded_state([(300.0, MEDICA_III), (330.0, MEDICA_III)])
    cards = improvements_from_heal_gcds(_FakeRun(state))
    assert len(cards) == 1
    card = cards[0]
    assert card.kind == "extra_heal_gcds"
    assert abs(card.lost_potency - 2 * 350.0) < 1e-6, card.lost_potency
    assert len(card.children) == 2
    # Located at the exact excess casts the reconcile flagged.
    assert {c.time_s for c in card.children} == {300.0, 330.0}


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all WHM lock checks passed")


if __name__ == "__main__":
    main()
