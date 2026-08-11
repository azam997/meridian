"""Premade ("PF") mitigation-plan tests: the pinned-assignment pass in the
planner + the JSON loader/validator (mitplan/premade.py).

Covers: pinned mits placed on the matched mechanic with derived timing; the
greedy HEALER tiers suppressed for a pinned mechanic (the lock stays honest to
the plan) while party mitigation still auto-fills; off-comp jobs skipped with a
warning; boss-id vs name matching + occurrence disambiguation; unknown-ability
validation; and byte-identical determinism when nothing is pinned.

Run from python/:  python tests/test_premade_plan.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitplan.classify import DamageModel, Mechanic  # noqa: E402
from mitplan.planner import Tier, plan  # noqa: E402
from mitplan.premade import PinnedEntry, PremadePlan, load_premade  # noqa: E402
from mitplan.library import ACTIONS  # noqa: E402

ROLE_HP = {"tank": 320_000.0, "healer": 205_000.0, "dps": 226_000.0}
COMP = dict(shield_healer="Sage", regen_healer="White Mage",
            tanks=["Paladin", "Dark Knight"],
            dps=["Samurai", "Dragoon", "Bard", "Pictomancer"])

_HEALER_ACTIONS = {(a.job, a.action_id) for a in ACTIONS
                   if a.tier in (Tier.HEALER_OGCD, Tier.HEALER_GCD)}


def mech(mid, t, name=None, kind="raidwide", school="magical",
         tank=100_000, healer=160_000, dps=170_000):
    unmit = {"tank": float(tank), "healer": float(healer), "dps": float(dps)}
    return Mechanic(
        id=mid, time_s=float(t), end_s=float(t) + 0.5,
        name=name if name is not None else mid,
        boss_ability_ids=[int(mid.split("#")[0])], kind=kind, school=school,
        hits=[{"time_s": float(t), "unmitigated": dict(unmit)}],
        unmitigated=unmit, unmitigated_p90={k: v * 1.05 for k, v in unmit.items()},
        observed_mit_pct=0.2, presence_ratio=1.0)


def model(mechanics):
    return DamageModel(
        mechanics=mechanics, avoidable_count=0, ref_count=10,
        model_kill_s=max((m.time_s for m in mechanics), default=60.0) + 30.0,
        ref_avg_kill_s=500.0, role_hp=dict(ROLE_HP), hp_source="logs",
        tank_drain_hps=2_000.0, magnitudes={"shield_hp_by_status": {}},
        hp_per_potency={"_default": 80.0}, downtime_windows=[],
        encounter_id=1085, encounter_name="Test Ultimate")


def run(mechanics, pinned=None):
    return plan(model(mechanics), COMP["shield_healer"], COMP["regen_healer"],
                COMP["tanks"], COMP["dps"], pinned=pinned)


def _pinned(*entries):
    return PremadePlan(encounter_id=1085, encounter_name="Test Ultimate",
                       entries=tuple(entries))


def _own(pm):
    return [a for a in pm.assignments if not a.is_carryover and not a.is_suggestion]


# --- the pinned pass ---------------------------------------------------------

def test_pinned_mits_placed_with_derived_timing():
    # Kerachole (Sage/H1) + Temperance (WHM/H2) pinned on a raidwide.
    m = mech("16542#0", 40.0)
    p = run([m], _pinned(PinnedEntry(
        label="Grand Cross", boss_ability_id=16542,
        mits=(("Sage", 24298), ("White Mage", 16536)))))
    pm = p.mechanics[0]
    placed = {(a.slot, a.action_id): a for a in _own(pm)}
    assert ("H1", 24298) in placed, placed         # Sage Kerachole
    assert ("H2", 16536) in placed, placed         # WHM Temperance
    for a in (placed[("H1", 24298)], placed[("H2", 16536)]):
        assert a.cast_at_s < m.time_s              # timing derived (leads the hit)
        assert not a.is_suggestion and not a.is_gcd
    assert pm.stop_reason in ("pinned", "goal_met")


def test_pinned_suppresses_greedy_healer_tiers():
    # A heavy raidwide the pinned pair can't fully cover: NO healer oGCD beyond
    # the two pinned ones may appear (the plan owns the healers), but party
    # mitigation is free to auto-fill.
    m = mech("100#0", 45.0, healer=430_000, dps=470_000, tank=280_000)
    p = run([m], _pinned(PinnedEntry(
        label="Big", boss_ability_id=100,
        mits=(("Sage", 24298), ("White Mage", 16536)))))
    pm = p.mechanics[0]
    healer_own = [a for a in _own(pm) if (a.job, a.action_id) in _HEALER_ACTIONS]
    assert {(a.slot, a.action_id) for a in healer_own} == {("H1", 24298), ("H2", 16536)}
    # Party mitigation (tank/DPS raid tools) DID auto-fill this heavy hit.
    assert any(a.slot in ("T1", "T2", "D1", "D2", "D3", "D4") for a in _own(pm))


def test_off_comp_job_skipped_silently_uncovered_slot_warns():
    # Comp is Sage(H1)+WHM(H2). Pin a Scholar mit (off-comp) + a Sage mit; no WHM.
    m = mech("200#0", 40.0)
    p = run([m], _pinned(PinnedEntry(
        label="X", boss_ability_id=200,
        mits=(("Scholar", 188), ("Sage", 24298)))))
    pm = p.mechanics[0]
    ids = {a.action_id for a in _own(pm)}
    assert 188 not in ids                          # Scholar off-comp — not placed
    assert 24298 in ids                            # Sage (H1) placed
    # Off-comp mits skip SILENTLY (a sheet lists every healer — no per-mit spam).
    assert not any("comp" in w and "Scholar" in w for w in p.warnings), p.warnings
    # The uncovered analyzed-healer slot (H2 / White Mage) warns exactly once.
    h2 = [w for w in p.warnings if "White Mage" in w and "H2" in w]
    assert len(h2) == 1, p.warnings


def test_name_match_and_occurrence():
    # Two instances of the same boss ability; pin only the 2nd by occurrence.
    ms = [mech("300#0", 40.0, name="Grand Cross"),
          mech("300#1", 90.0, name="Grand Cross")]
    p = run(ms, _pinned(PinnedEntry(
        label="Grand Cross", name="Grand Cross", occurrence=1,
        mits=(("Sage", 24298),))))
    first, second = p.mechanics
    assert 24298 not in {a.action_id for a in _own(first)}
    assert 24298 in {a.action_id for a in _own(second)}


def test_role_generic_distributes_shared_cooldown():
    # Two mechanics 20s apart both pin a role:tank Reprisal (60s recast) — the
    # pre-pass must distribute the shared cooldown across BOTH tanks.
    ms = [mech("60#0", 40.0), mech("60#1", 60.0)]
    p = run(ms, _pinned(
        PinnedEntry(label="m0", boss_ability_id=60, occurrence=0, mits=(("@tank", 7535),)),
        PinnedEntry(label="m1", boss_ability_id=60, occurrence=1, mits=(("@tank", 7535),))))
    reps = [(pm.mech.id, a.slot) for pm in p.mechanics for a in pm.assignments
            if a.action_id == 7535 and not a.is_carryover]
    assert {s for _, s in reps} == {"T1", "T2"}, reps   # spread across both tanks


def test_role_generic_resolves_to_comp_melee():
    # A role:melee Feint resolves to a DPS slot whose job is a melee (Samurai/
    # Dragoon in COMP), never a ranged/caster/tank.
    p = run([mech("61#0", 40.0)], _pinned(
        PinnedEntry(label="m", boss_ability_id=61, mits=(("@melee", 7549),))))
    feint = [a for pm in p.mechanics for a in pm.assignments
             if a.action_id == 7549 and not a.is_carryover]
    assert len(feint) == 1 and feint[0].job in ("Samurai", "Dragoon"), feint


def test_load_premade_parses_and_validates_roles():
    doc = {"encounter_id": 1085, "assignments": [
        {"name": "X", "boss_ability_id": 5, "mits": [
            {"role": "melee", "action_id": 7549},     # Feint — kept
            {"role": "bogus", "action_id": 7549},     # unknown role — dropped
            {"role": "caster", "action_id": 188}]}]}  # no caster has Sacred Soil
    with tempfile.TemporaryDirectory() as d:
        import mitplan.premade as premade
        saved = premade._DIR
        premade._DIR = Path(d)
        try:
            (Path(d) / "1085.json").write_text(json.dumps(doc), encoding="utf-8")
            pp = premade.load_premade(1085)
        finally:
            premade._DIR = saved
    assert pp.entries[0].mits == (("@melee", 7549),)
    assert any("bogus" in w for w in pp.warnings)
    assert any("caster" in w and "brings" in w for w in pp.warnings)


def test_unmatched_entry_warns():
    p = run([mech("400#0", 40.0)], _pinned(PinnedEntry(
        label="Nonexistent", boss_ability_id=999999,
        mits=(("Sage", 24298),))))
    assert any("no mechanic matched" in w for w in p.warnings), p.warnings


def test_pinned_none_is_unchanged():
    ms = [mech("500#0", 30.0), mech("501#0", 60.0, kind="tankbuster",
                                    tank=360_000, healer=0, dps=0)]
    a = [asdict(x) for pm in run(ms).mechanics for x in pm.assignments]
    b = [asdict(x) for pm in run(ms, pinned=None).mechanics for x in pm.assignments]
    assert a == b
    # And an empty premade plan is likewise a no-op.
    c = [asdict(x) for pm in run(ms, _pinned()).mechanics for x in pm.assignments]
    assert a == c


# --- the JSON loader ---------------------------------------------------------

def test_load_premade_validates_and_drops_unknown():
    doc = {
        "encounter_id": 1085, "encounter_name": "DM", "source": "sheet",
        "assignments": [
            {"mechanic": "Grand Cross", "name": "Grand Cross", "occurrence": 0,
             "mits": [{"job": "Sage", "action_id": 24298},
                      {"job": "Sage", "action_id": 88888888}]},   # bogus id
            {"mechanic": "Empty", "boss_ability_id": 5, "mits": []},  # dropped
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        import mitplan.premade as premade
        saved = premade._DIR
        premade._DIR = Path(d)
        try:
            (Path(d) / "1085.json").write_text(json.dumps(doc), encoding="utf-8")
            pp = premade.load_premade(1085)
        finally:
            premade._DIR = saved
    assert pp is not None
    assert len(pp.entries) == 1                    # the empty-mits row dropped
    e = pp.entries[0]
    assert e.mits == (("Sage", 24298),)            # bogus id dropped
    assert e.name == "Grand Cross" and e.occurrence == 0
    assert any("not in the mit library" in w for w in pp.warnings), pp.warnings


def test_load_premade_absent_returns_none():
    with tempfile.TemporaryDirectory() as d:
        import mitplan.premade as premade
        saved = premade._DIR
        premade._DIR = Path(d)
        try:
            assert premade.load_premade(999) is None
            assert premade.has_premade(999) is False
        finally:
            premade._DIR = saved


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("test_premade_plan: all passed")


if __name__ == "__main__":
    main()
