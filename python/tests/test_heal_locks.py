"""heal_locks derivation + pull-comp resolution (network-free).

Covers jobs/_core/heal_locks.py (locks_from_plan / plan_gcd_cost over a
synthetic mitplan Plan) and mitplan/comp.py (resolve_comp_from_fight over a
synthetic report). The engine scheduler is pinned by test_engine_locks.py and
the WHM integration by test_whitemage_locks.py.

Run from python/:  python tests/test_heal_locks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.heal_locks import (LOCK_LEAD_S, LockedGcdWindow, locks_from_plan,
                                   plan_gcd_cost, reconcile_heal_budget)
from mitplan.classify import Mechanic
from mitplan.comp import (canonical_job_name, resolve_comp_from_fight,
                          slot_for_job)
from mitplan.planner import Assignment, GcdHeal, Plan, PlanMechanic

WHM_MEDICA_III = 37010
WHM_RAPTURE = 16534
SGE_EPROG = 37034


def _mech(mid: str, t: float, kind: str = "raidwide") -> Mechanic:
    return Mechanic(id=mid, time_s=t, end_s=t, name=mid,
                    boss_ability_ids=[1], kind=kind, school="magical",
                    hits=[{"time_s": t, "unmitigated": {"dps": 50000.0}}],
                    unmitigated={"tank": 60000.0, "healer": 52000.0,
                                 "dps": 50000.0},
                    unmitigated_p90={}, observed_mit_pct=0.4,
                    presence_ratio=1.0)


def _gh(t: float, count: int = 1, aid: int = WHM_MEDICA_III,
        slot: str = "H2") -> GcdHeal:
    return GcdHeal(slot=slot, job="White Mage", action_id=aid,
                   name="Medica III", cast_at_s=t, count=count,
                   cast_time_s=2.5, heal_amount=20000.0)


def _assign(t: float, *, is_gcd: bool = True, suggestion: bool = False,
            carryover: bool = False, slot: str = "H1",
            aid: int = SGE_EPROG) -> Assignment:
    return Assignment(slot=slot, job="Sage", action_id=aid,
                      name="Eukrasian Prognosis II", cast_at_s=t,
                      duration_s=30.0, target="party", mit_pct=0.0,
                      shield_amount=15000.0, heal_amount=0.0, hot_hps=0.0,
                      is_gcd=is_gcd, cast_time_s=2.0,
                      is_suggestion=suggestion, is_carryover=carryover)


def _plan(mechs: list[PlanMechanic]) -> Plan:
    return Plan(slots=[("T1", "Paladin"), ("T2", "Dark Knight"),
                       ("H1", "Sage"), ("H2", "White Mage"),
                       ("D1", "Samurai"), ("D2", "Dragoon"),
                       ("D3", "Bard"), ("D4", "Pictomancer")],
                mechanics=mechs, summary={}, warnings=[])


# --- locks_from_plan -----------------------------------------------------------

def test_locks_from_gcd_heals() -> None:
    pm = PlanMechanic(mech=_mech("a#1", 120.0), gcd_heals=[_gh(110.0, count=2)])
    locks = locks_from_plan(_plan([pm]), "H2", 600.0)
    assert len(locks) == 1
    lk = locks[0]
    assert lk.ability_id == WHM_MEDICA_III and lk.count == 2
    assert lk.end_s == 120.0
    # Window opens LEAD before the earliest needed placement.
    assert abs(lk.start_s - (min(110.0, 120.0 - 2 * 2.5) - LOCK_LEAD_S)) < 0.05


def test_locks_filter_slot_and_flags() -> None:
    pm = PlanMechanic(
        mech=_mech("a#1", 90.0),
        gcd_heals=[_gh(85.0, slot="H2")],
        assignments=[
            _assign(80.0, slot="H1"),                    # real GCD shield -> H1 lock
            _assign(80.0, slot="H1", is_gcd=False),      # oGCD -> never locked
            _assign(80.0, slot="H1", suggestion=True),   # suggestion -> skipped
            _assign(80.0, slot="H1", carryover=True),    # carryover -> skipped
        ])
    plan = _plan([pm])
    h2 = locks_from_plan(plan, "H2", 600.0)
    assert len(h2) == 1 and h2[0].ability_id == WHM_MEDICA_III
    h1 = locks_from_plan(plan, "H1", 600.0)
    assert len(h1) == 1 and h1[0].ability_id == SGE_EPROG and h1[0].count == 1


def test_locks_clip_to_fight_span() -> None:
    mechs = [
        PlanMechanic(mech=_mech("a#1", 100.0), gcd_heals=[_gh(95.0)]),
        PlanMechanic(mech=_mech("b#1", 400.0), gcd_heals=[_gh(395.0)]),
    ]
    plan = _plan(mechs)
    # Player killed at 300 s -> the 400 s mechanic never happened for them.
    locks = locks_from_plan(plan, "H2", 300.0)
    assert len(locks) == 1 and locks[0].end_s == 100.0
    # Full-length run keeps both.
    assert len(locks_from_plan(plan, "H2", 600.0)) == 2


def test_locks_merge_same_deadline_and_sort() -> None:
    pm = PlanMechanic(mech=_mech("a#1", 60.0),
                      gcd_heals=[_gh(55.0), _gh(50.0)])
    late = PlanMechanic(mech=_mech("b#1", 30.0), gcd_heals=[_gh(25.0)])
    locks = locks_from_plan(_plan([pm, late]), "H2", 600.0)
    assert [lk.end_s for lk in locks] == [30.0, 60.0]
    assert locks[1].count == 2                      # merged (same aid+deadline)
    assert locks[1].start_s <= 50.0 - LOCK_LEAD_S + 0.05


def test_locks_deterministic() -> None:
    pm = PlanMechanic(mech=_mech("a#1", 120.0),
                      gcd_heals=[_gh(110.0, count=2), _gh(112.0, aid=WHM_RAPTURE)])
    plan = _plan([pm])
    assert locks_from_plan(plan, "H2", 600.0) == locks_from_plan(plan, "H2", 600.0)


def test_plan_gcd_cost_prices_costed_only() -> None:
    pm = PlanMechanic(
        mech=_mech("a#1", 120.0),
        gcd_heals=[_gh(110.0, count=2),                       # Medica III: costed
                   _gh(112.0, aid=WHM_RAPTURE, count=1)])     # Rapture: free
    count, potency, costed = plan_gcd_cost(_plan([pm]), "H2")
    assert count == 3
    assert costed == 2
    assert abs(potency - 2 * 350.0) < 1e-6, potency


# --- reconcile_heal_budget (the honest-budget lift to actual healing) ------------

_COSTED = frozenset({WHM_MEDICA_III})


def _win(start: float, end: float, count: int, aid: int = WHM_MEDICA_III
         ) -> LockedGcdWindow:
    return LockedGcdWindow(ability_id=aid, start_s=start, end_s=end,
                           count=count, cast_s=2.5)


def _reconcile(plan_locks, casts, *, is_prog: bool, dur: float = 600.0):
    return reconcile_heal_budget(
        plan_locks=tuple(plan_locks), plan_meta={"source": "pull"},
        actual_costed_casts=[(float(t), WHM_MEDICA_III) for t in casts],
        costed_ids=_COSTED, locked_heal_id=WHM_MEDICA_III,
        filler_potency=350.0, fight_duration_s=dur, is_prog=is_prog)


def test_reconcile_case_b_floor() -> None:
    # Healed fewer than the plan's 3 -> keep the plan windows (the floor); a
    # carried healer can still exceed this ceiling. No over-heal card.
    plan = [_win(50, 60, 1), _win(150, 160, 1), _win(250, 260, 1)]
    b = _reconcile(plan, [55.0], is_prog=False)
    assert b.applied and b.locks == tuple(plan)
    assert b.state["heal_lock_costed_count"] == 3
    assert b.state["heal_lock_excess"] == []
    assert b.state["mit_plan_comp_source"] == "pull"


def test_reconcile_kill_slack_tolerates_then_cards() -> None:
    # Plan 2 costed; slack = max(2, ceil(0.2*2)) = 2 -> up to 4 free on a kill.
    plan = [_win(50, 60, 1), _win(150, 160, 1)]
    within = _reconcile(plan, [55.0, 100.0, 155.0, 200.0], is_prog=False)
    assert within.state["heal_lock_excess"] == []
    assert within.state["heal_lock_costed_count"] == 4
    over = _reconcile(plan, [55.0, 100.0, 155.0, 200.0, 300.0, 400.0], is_prog=False)
    assert len(over.state["heal_lock_excess"]) == 2
    assert over.state["heal_lock_costed_count"] == 4          # plan(2) + slack(2)
    assert abs(over.state["heal_lock_potency"] - 4 * 350.0) < 1e-6


def test_reconcile_prog_credits_all() -> None:
    # A wipe is non-competitive -> the cap is waived; every heal is credited and
    # nothing cards (the whole point of prog support).
    plan = [_win(50, 60, 1), _win(150, 160, 1)]
    casts = [40.0, 55.0, 90.0, 155.0, 190.0, 250.0, 300.0]   # 7 >> plan 2
    b = _reconcile(plan, casts, is_prog=True)
    assert b.state["heal_lock_excess"] == []
    assert b.state["heal_lock_costed_count"] == 7
    assert len(b.locks) == 7 and all(w.count == 1 for w in b.locks)


def test_reconcile_out_of_order_no_card() -> None:
    # The same count as the plan, cast in a different order/time, reconciles clean.
    plan = [_win(50, 60, 1), _win(150, 160, 1)]
    b = _reconcile(plan, [160.0, 45.0], is_prog=False)
    assert b.state["heal_lock_excess"] == []


def test_reconcile_excess_is_least_necessary() -> None:
    # Over the kill cap, the carded casts are the ones FARTHEST from a plan
    # mechanic (discretionary over-heals), not merely the latest.
    plan = [_win(100, 110, 1)]                                 # cap = 1 + slack 2 = 3
    b = _reconcile(plan, [5.0, 105.0, 108.0, 300.0, 500.0], is_prog=False)
    assert b.state["heal_lock_costed_count"] == 3
    excess = sorted(t for t, _a in b.state["heal_lock_excess"])
    assert excess == [300.0, 500.0]                            # the two most isolated


def test_reconcile_reclips_to_scored_span() -> None:
    # A plan window past the scored (truncated-wipe) span is dropped before locking.
    plan = [_win(50, 60, 1), _win(250, 260, 1)]
    b = _reconcile(plan, [], is_prog=True, dur=100.0)
    assert all(w.end_s <= 100.0 for w in b.locks)
    assert b.state["heal_lock_costed_count"] == 1


def test_reconcile_thin_plan_prog_fallback() -> None:
    # Prog with NO usable plan (unmodeled fight) still credits the real healing,
    # pinned at the actual cast times.
    b = _reconcile([], [40.0, 120.0, 200.0], is_prog=True)
    assert b.applied and b.state["heal_lock_costed_count"] == 3
    assert b.state["heal_lock_excess"] == []
    assert len(b.locks) == 3


def test_reconcile_credit_windows_straddle_actual_times() -> None:
    b = _reconcile([_win(50, 60, 1)], [55.0, 200.0, 400.0], is_prog=True)
    for w, t in zip(sorted(b.locks, key=lambda w: w.end_s), [55.0, 200.0, 400.0]):
        assert w.start_s <= t <= w.end_s, (w, t)


# --- resolve_comp_from_fight -----------------------------------------------------

def _report(sub_types: list[str]) -> tuple[dict, dict]:
    actors = [{"id": i + 1, "type": "Player", "subType": s, "name": f"P{i}"}
              for i, s in enumerate(sub_types)]
    report = {"masterData": {"actors": actors}}
    fight = {"id": 7, "friendlyPlayers": [a["id"] for a in actors]}
    return report, fight


_STANDARD = ["Paladin", "DarkKnight", "Sage", "WhiteMage",
             "Samurai", "Dragoon", "Bard", "Pictomancer"]


def test_comp_standard() -> None:
    res = resolve_comp_from_fight(*_report(_STANDARD), anchor_job="White Mage")
    assert res.shield_healer == "Sage" and res.regen_healer == "White Mage"
    assert res.tanks == ["Paladin", "Dark Knight"]
    assert res.dps == ["Samurai", "Dragoon", "Bard", "Pictomancer"]
    assert res.source == "pull" and res.warnings == []


def test_comp_spaceless_normalization() -> None:
    assert canonical_job_name("WhiteMage") == "White Mage"
    assert canonical_job_name("DarkKnight") == "Dark Knight"
    assert canonical_job_name("Sage") == "Sage"
    assert canonical_job_name("LimitBreak") is None


def test_comp_double_regen_keeps_anchor() -> None:
    subs = ["Paladin", "DarkKnight", "WhiteMage", "Astrologian",
            "Samurai", "Dragoon", "Bard", "Pictomancer"]
    res = resolve_comp_from_fight(*_report(subs), anchor_job="White Mage")
    assert res.regen_healer == "White Mage"
    assert res.shield_healer == "Sage"          # substituted default
    assert res.warnings, res


def test_comp_double_shield_keeps_anchor() -> None:
    subs = ["Paladin", "DarkKnight", "Sage", "Scholar",
            "Samurai", "Dragoon", "Bard", "Pictomancer"]
    res = resolve_comp_from_fight(*_report(subs), anchor_job="Scholar")
    assert res.shield_healer == "Scholar"
    assert res.regen_healer == "White Mage"
    assert res.warnings, res


def test_comp_pads_missing_players() -> None:
    subs = ["Warrior", "Sage", "WhiteMage", "Ninja"]
    res = resolve_comp_from_fight(*_report(subs), anchor_job="White Mage")
    assert len(res.tanks) == 2 and res.tanks[0] == "Warrior"
    assert len(res.dps) == 4 and res.dps[0] == "Ninja"
    assert res.warnings, res


def test_slot_for_job() -> None:
    assert slot_for_job("White Mage") == "H2"
    assert slot_for_job("Astrologian") == "H2"
    assert slot_for_job("Sage") == "H1"
    assert slot_for_job("Scholar") == "H1"
    assert slot_for_job("Paladin") is None


# --- sidecar _heal_lock_payload (monkeypatched model + plan) ----------------------

class _PayloadClient:
    """get_report_summary only — the rest of _heal_lock_payload is patched."""

    def __init__(self, sub_types: list[str], duration_s: float = 300.0):
        report, fight = _report(sub_types)
        fight["startTime"] = 0
        fight["endTime"] = int(duration_s * 1000)
        report["fights"] = [fight]
        self._report = report

    def get_report_summary(self, code: str) -> dict:
        return self._report


def _patched_payload(client, job: str, comp_override=None,
                     plan_mechs=None, model_raises: bool = False):
    """Run sidecar_main._heal_lock_payload with the mitplan model + planner
    patched out (save/restore so the file stays standalone-runnable)."""
    import mitplan
    from sidecar import main as sidecar_main

    mechs = plan_mechs if plan_mechs is not None else [
        PlanMechanic(mech=_mech("a#1", 120.0), gcd_heals=[_gh(110.0, count=2)]),
    ]
    fake_plan = _plan(mechs)

    saved_model = sidecar_main._get_mitplan_model
    saved_plan = mitplan.plan

    def _fake_model(client_, encounter_id, progress):
        if model_raises:
            raise RuntimeError("model build failed")
        return object()

    sidecar_main._get_mitplan_model = _fake_model
    mitplan.plan = lambda model, s, r, t, d, pinned=None: fake_plan
    try:
        return sidecar_main._heal_lock_payload(
            client, job, "AbCd1234", 7, 99, comp_override,
            progress=lambda *a, **k: None)
    finally:
        sidecar_main._get_mitplan_model = saved_model
        mitplan.plan = saved_plan


def test_heal_lock_payload_pull_comp() -> None:
    client = _PayloadClient(_STANDARD, duration_s=300.0)
    payload = _patched_payload(client, "White Mage")
    assert payload is not None
    assert payload["source"] == "pull"
    assert payload["comp"][:2] == ["Sage", "White Mage"]
    assert len(payload["locks"]) == 1
    assert payload["locks"][0].count == 2
    assert payload["count"] == 2 and payload["plan_costed_count"] == 2


def test_heal_lock_payload_clips_to_pull_duration() -> None:
    client = _PayloadClient(_STANDARD, duration_s=100.0)   # mech at 120s > kill
    payload = _patched_payload(client, "White Mage")
    assert payload is not None
    assert payload["locks"] == ()
    # The plan-wide meta still reports what the plan schedules (the ceiling
    # just has nothing to lock inside this pull's span).
    assert payload["count"] == 2


def test_heal_lock_payload_override_comp() -> None:
    client = _PayloadClient(_STANDARD)
    payload = _patched_payload(
        client, "White Mage",
        comp_override=("Scholar", "White Mage", ("Warrior", "Gunbreaker"),
                       ("Ninja", "Viper", "Dancer", "Summoner")))
    assert payload is not None
    assert payload["source"] == "override"
    assert payload["comp"][0] == "Scholar" and payload["comp"][2] == "Warrior"


def test_heal_lock_payload_failure_returns_none() -> None:
    from sidecar import event_log
    client = _PayloadClient(_STANDARD)
    saved_log = event_log.log
    logged: list[tuple] = []
    event_log.log = lambda *a, **k: logged.append(a)
    try:
        payload = _patched_payload(client, "White Mage", model_raises=True)
    finally:
        event_log.log = saved_log
    assert payload is None
    assert logged and logged[0][1] == "heal_locks", logged


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all heal-lock derivation checks passed")


if __name__ == "__main__":
    main()
