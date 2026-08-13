"""User-authored ("exclusive") mitigation-plan tests: the planner's
`pinned_exclusive=True` mode + the shared `parse_plan_dict` validator.

Exclusive mode is the drag-and-drop editor's backend: the plan is EXACTLY the
authored casts — no invuln-first pass, no greedy tiers (party mit included), no
tank-buster suggestions, no pass-2.5 top-up — while the HP sweep, statuses and
summary still run, and only cooldowns/charges/resource pools reject a cast.
Generator policy caps (MAX_ASSIGN_PER_MECHANIC, the buster party-mit ban,
MIN_MARGINAL_PREVENTED) must never bind an authored cast.

Run from python/:  python tests/test_mitplan_user_plan.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitplan.classify import DamageModel, Mechanic  # noqa: E402
from mitplan.planner import MAX_ASSIGN_PER_MECHANIC, plan  # noqa: E402
from mitplan.premade import (PinnedEntry, PremadePlan, load_premade,  # noqa: E402
                             parse_plan_dict)

ROLE_HP = {"tank": 320_000.0, "healer": 205_000.0, "dps": 226_000.0}
COMP = dict(shield_healer="Sage", regen_healer="White Mage",
            tanks=["Paladin", "Dark Knight"],
            dps=["Samurai", "Dragoon", "Bard", "Pictomancer"])

KERACHOLE = 24298        # Sage, 30s cd, addersgall
TEMPERANCE = 16536       # White Mage, 120s cd
MEDICA_III = 37010       # White Mage AoE GCD heal (costed)
CURE_II = 135            # White Mage single-target GCD heal (tank top-up)
RAPTURE = 16534          # White Mage AoE lily heal (free, lily-gated)
PLENARY = 7433           # White Mage pair-gated Confession rider
REPRISAL = 7535          # any tank, 60s cd
FEINT = 7549             # any melee, 90s cd
ADDLE = 7560             # any caster, 90s cd
TROUBADOUR = 7405        # Bard, 90s cd
DIVINE_VEIL = 3540       # Paladin, 90s cd
HALLOWED = 30            # Paladin invuln


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


def _user(*entries):
    return PremadePlan(encounter_id=1085, encounter_name="Test Ultimate",
                       entries=tuple(entries))


def run_user(mechanics, *entries):
    return plan(model(mechanics), COMP["shield_healer"], COMP["regen_healer"],
                COMP["tanks"], COMP["dps"], pinned=_user(*entries),
                pinned_exclusive=True)


def test_package_wrapper_forwards_exclusive():
    # The sidecar calls mitplan.plan (the lazy-import wrapper in __init__.py),
    # NOT mitplan.planner.plan — the wrapper must forward pinned_exclusive.
    # (A live TypeError here is exactly the bug this pins.)
    import mitplan
    m = mech("50#0", 45.0, healer=430_000, dps=470_000, tank=280_000)
    p = mitplan.plan(model([m]), COMP["shield_healer"], COMP["regen_healer"],
                     COMP["tanks"], COMP["dps"],
                     pinned=_user(PinnedEntry(
                         label="Big", boss_ability_id=50,
                         mits=(("Sage", KERACHOLE),))),
                     pinned_exclusive=True)
    pm = p.mechanics[0]
    assert {(a.slot, a.action_id) for a in _own(pm)} == {("H1", KERACHOLE)}
    assert not any(a.is_suggestion for a in pm.assignments)


def _own(pm):
    return [a for a in pm.assignments if not a.is_carryover]


# --- exclusive placement -------------------------------------------------------

def test_exclusive_places_exactly_the_authored_casts():
    # A heavy raidwide the greedy would pile onto: exclusive mode places ONLY
    # the two authored mits — no party auto-fill, no healer tiers, nothing
    # suggested anywhere in the plan.
    m = mech("100#0", 45.0, healer=430_000, dps=470_000, tank=280_000)
    p = run_user([m], PinnedEntry(
        label="Big", boss_ability_id=100,
        mits=(("Sage", KERACHOLE), ("White Mage", TEMPERANCE))))
    pm = p.mechanics[0]
    assert {(a.slot, a.action_id) for a in _own(pm)} == {
        ("H1", KERACHOLE), ("H2", TEMPERANCE)}, pm.assignments
    assert not any(a.is_suggestion for x in p.mechanics for a in x.assignments)
    for a in _own(pm):
        assert a.cast_at_s < m.time_s          # timing derived (leads the hit)


def test_exclusive_empty_plan_places_nothing():
    # An empty draft: zero assignments, no invuln-first on the lethal buster,
    # stop_reason "user" and the gap note on uncovered mechanics.
    ms = [mech("200#0", 30.0, healer=430_000, dps=470_000),
          mech("201#0", 60.0, kind="tankbuster",
               tank=900_000, healer=0, dps=0)]
    p = run_user(ms)
    assert all(not pm.assignments for pm in p.mechanics)
    for pm in p.mechanics:
        assert pm.stop_reason == "user", pm.stop_reason
    uncovered = [pm for pm in p.mechanics if pm.status == "uncovered"]
    assert uncovered, [pm.status for pm in p.mechanics]
    assert any("your plan leaves a gap here" in n
               for pm in uncovered for n in pm.notes), uncovered[0].notes


def test_exclusive_never_auto_inserts_heals():
    # Two near-lethal raidwides on a thin plan with NO authored heals: the
    # sweep must not invent any — GCD healing is plan content for a user
    # plan. hp_after still computes (entry HP carries between mechanics).
    ms = [mech("300#0", 30.0, healer=200_000, dps=220_000),
          mech("301#0", 45.0, healer=200_000, dps=220_000)]
    p = run_user(ms, PinnedEntry(label="first", boss_ability_id=300,
                                 mits=(("Sage", KERACHOLE),)))
    assert all(not pm.gcd_heals for pm in p.mechanics)
    assert p.summary["gcd_heal_count"] == 0
    assert all(pm.hp_after for pm in p.mechanics)


def test_authored_heals_price_and_credit():
    # The user authors 3 Medica IIIs before the second hit: they appear as
    # that mechanic's gcd_heals, count into the summary, and visibly raise
    # its post-hit HP versus the bare plan. Earlier mechanics are untouched.
    ms = [mech("310#0", 30.0, healer=200_000, dps=220_000),
          mech("311#0", 45.0, healer=200_000, dps=220_000)]
    bare = run_user(ms)
    healed = run_user(ms, PinnedEntry(
        label="second", boss_ability_id=311, mits=(),
        heals=(("White Mage", MEDICA_III, 3),)))
    pm = healed.mechanics[1]
    assert [(g.job, g.action_id, g.count) for g in pm.gcd_heals] == \
        [("White Mage", MEDICA_III, 3)], pm.gcd_heals
    assert all(g.slot == "H2" for g in pm.gcd_heals)
    assert healed.summary["gcd_heal_count"] == 3
    assert pm.hp_after["healer"] > bare.mechanics[1].hp_after["healer"]
    assert healed.mechanics[0].hp_after == bare.mechanics[0].hp_after


def test_authored_single_target_heal_credits_tank_only():
    # A buster drops the tank; two authored Cure IIs before the next hit top
    # up the TANK alone — healer/dps HP identical to the bare plan.
    ms = [mech("330#0", 30.0, kind="tankbuster",
               tank=250_000, healer=0, dps=0),
          mech("331#0", 50.0, tank=60_000, healer=120_000, dps=130_000)]
    bare = run_user(ms)
    healed = run_user(ms, PinnedEntry(
        label="second", boss_ability_id=331, mits=(),
        heals=(("White Mage", CURE_II, 2),)))
    pm = healed.mechanics[1]
    assert [(g.action_id, g.count) for g in pm.gcd_heals] == [(CURE_II, 2)]
    assert pm.hp_after["tank"] > bare.mechanics[1].hp_after["tank"]
    assert pm.hp_after["healer"] == bare.mechanics[1].hp_after["healer"]
    assert pm.hp_after["dps"] == bare.mechanics[1].hp_after["dps"]


def test_single_target_ogcd_heal_credits_tank_in_sweep():
    # A healer's ST oGCD (Tetragrammaton) authored onto a buster heals the
    # TANK in the HP sweep — the exact "heal the tank after the buster" case.
    ms = [mech("340#0", 30.0, kind="tankbuster",
               tank=250_000, healer=0, dps=0),
          mech("341#0", 55.0, tank=60_000, healer=120_000, dps=130_000)]
    bare = run_user(ms)
    healed = run_user(ms, PinnedEntry(
        label="buster", boss_ability_id=340,
        mits=(("White Mage", 3570),)))          # Tetragrammaton
    assert healed.mechanics[0].hp_after["tank"] \
        > bare.mechanics[0].hp_after["tank"]
    assert healed.mechanics[0].hp_after["dps"] \
        == bare.mechanics[0].hp_after["dps"]


def test_tank_self_sustain_credits_tank():
    # A Warrior's dragged Bloodwhetting (weave self-sustain: heal + ticks +
    # Stem the Tide barrier) mitigates AND heals the tank through a buster.
    ms = [mech("350#0", 30.0, kind="tankbuster",
               tank=250_000, healer=0, dps=0)]
    def run_war(entries):
        return plan(model(ms), COMP["shield_healer"], COMP["regen_healer"],
                    ["Warrior", "Paladin"], COMP["dps"],
                    pinned=(PremadePlan(encounter_id=1085,
                                        encounter_name="Test Ultimate",
                                        entries=entries) if entries else
                            _user()),
                    pinned_exclusive=True)
    bare = run_war(())
    sustained = run_war((PinnedEntry(label="buster", boss_ability_id=350,
                                     mits=(("Warrior", 25751),)),))
    pm = sustained.mechanics[0]
    assert 25751 in {a.action_id for a in _own(pm)}
    assert pm.predicted["tank"] < bare.mechanics[0].predicted["tank"]  # 10% mit
    assert pm.hp_after["tank"] > bare.mechanics[0].hp_after["tank"]


def test_authored_heal_off_comp_or_not_a_heal_drops_loudly():
    m = mech("320#0", 40.0)
    p = run_user([m], PinnedEntry(
        label="x", boss_ability_id=320, mits=(),
        heals=(("Scholar", 188, 1),          # not a healer in this comp
               ("White Mage", TEMPERANCE, 1))))   # oGCD mit, not a GCD heal
    assert all(not pm.gcd_heals for pm in p.mechanics)
    assert any("not a healer in this comp" in w for w in p.warnings), p.warnings
    assert any("not a GCD heal" in w for w in p.warnings), p.warnings


def test_infeasible_cast_dropped_with_warning():
    # Kerachole (30s recast) authored on mechanics 15s apart: the second cast
    # is cooldown-infeasible and drops with a "Your plan" warning.
    ms = [mech("400#0", 40.0), mech("401#0", 55.0)]
    p = run_user(
        ms,
        PinnedEntry(label="a", boss_ability_id=400, mits=(("Sage", KERACHOLE),)),
        PinnedEntry(label="b", boss_ability_id=401, mits=(("Sage", KERACHOLE),)))
    placed = [a for pm in p.mechanics for a in _own(pm)
              if a.action_id == KERACHOLE]
    assert len(placed) == 1, placed
    assert any(w.startswith("Your plan:") and "unavailable (cooldown)" in w
               for w in p.warnings), p.warnings


def test_generator_caps_do_not_bind():
    # Seven party tools authored onto ONE raidwide (> MAX_ASSIGN_PER_MECHANIC):
    # every single one lands — the greedy's cap is policy, not a constraint.
    m = mech("500#0", 45.0)
    p = run_user([m], PinnedEntry(
        label="stack", boss_ability_id=500,
        mits=(("Sage", KERACHOLE), ("White Mage", TEMPERANCE),
              ("@tank", REPRISAL), ("@melee", FEINT), ("@caster", ADDLE),
              ("@ranged", TROUBADOUR), ("Paladin", DIVINE_VEIL))))
    own = _own(p.mechanics[0])
    assert len(own) == 7 > MAX_ASSIGN_PER_MECHANIC, own


def test_party_mit_lands_on_a_tankbuster():
    # The greedy bans party-wide tools on busters; an authored one still lands.
    m = mech("600#0", 40.0, kind="tankbuster", tank=360_000, healer=0, dps=0)
    p = run_user([m], PinnedEntry(label="buster", boss_ability_id=600,
                                  mits=(("Sage", KERACHOLE),)))
    assert KERACHOLE in {a.action_id for a in _own(p.mechanics[0])}


def test_tiny_mit_lands():
    # Prevented damage far below MIN_MARGINAL_PREVENTED: still placed.
    m = mech("700#0", 40.0, tank=2_000, healer=2_000, dps=2_000)
    p = run_user([m], PinnedEntry(label="tiny", boss_ability_id=700,
                                  mits=(("@melee", FEINT),)))
    assert FEINT in {a.action_id for a in _own(p.mechanics[0])}


def test_carryover_credit_flows():
    # Kerachole cast ~38s (15s duration) blankets a second mechanic at 50s:
    # the later mechanic inherits it as a carryover assignment.
    ms = [mech("800#0", 40.0), mech("801#0", 50.0)]
    p = run_user(ms, PinnedEntry(label="a", boss_ability_id=800,
                                 mits=(("Sage", KERACHOLE),)))
    second = p.mechanics[1]
    carried = [a for a in second.assignments if a.is_carryover]
    assert any(a.action_id == KERACHOLE for a in carried), second.assignments


def test_authored_invuln_on_buster():
    m = mech("900#0", 60.0, kind="tankbuster", tank=900_000, healer=0, dps=0)
    p = run_user([m], PinnedEntry(label="inv", boss_ability_id=900,
                                  mits=(("Paladin", HALLOWED),)))
    pm = p.mechanics[0]
    assert pm.invulned
    assert pm.predicted["tank"] == 0.0
    inv = [a for a in pm.assignments if a.action_id == HALLOWED]
    assert len(inv) == 1 and not inv[0].is_suggestion, pm.assignments
    assert pm.status == "covered"


def test_authored_invuln_on_shared_buster_only_protects_tank():
    # A buster that also cleaves the party: the invuln zeroes the TANK's
    # damage only — everyone else still takes it, priced by their own mits.
    m = mech("910#0", 60.0, kind="tankbuster",
             tank=900_000, healer=150_000, dps=160_000)
    p = run_user([m], PinnedEntry(label="inv", boss_ability_id=910,
                                  mits=(("Paladin", HALLOWED),
                                        ("Sage", KERACHOLE))))
    pm = p.mechanics[0]
    assert pm.invulned and pm.stop_reason == "invulned"
    assert pm.predicted["tank"] == 0.0
    assert pm.predicted["healer"] > 0.0 and pm.predicted["dps"] > 0.0
    # Kerachole's 10% still prices for the non-tank roles.
    assert pm.predicted["healer"] < 150_000, pm.predicted


def run_auto(mechanics):
    return plan(model(mechanics), COMP["shield_healer"], COMP["regen_healer"],
                COMP["tanks"], COMP["dps"])


def test_generator_invuln_prices_shared_damage():
    # The GENERATOR's invuln-first pass on a buster that also cleaves the
    # party: the invuln covers the tank only — healer/dps damage prices
    # honestly (and falls to the HP sweep) instead of showing zero.
    m = mech("920#0", 60.0, kind="tankbuster",
             tank=900_000, healer=150_000, dps=160_000)
    p = run_auto([m])
    pm = p.mechanics[0]
    assert pm.invulned and pm.stop_reason == "invulned"
    assert pm.predicted["tank"] == 0.0
    assert pm.predicted["healer"] > 0.0 and pm.predicted["dps"] > 0.0


def test_invuln_duration_clamps_to_real_coverage():
    # A two-hit buster whose second hit lands AFTER the invuln expires
    # (Hallowed Ground: 10s): only the covered hit zeroes; the tail prices.
    hit = {"tank": 150_000.0, "healer": 0.0, "dps": 0.0}
    unmit = {"tank": 300_000.0, "healer": 0.0, "dps": 0.0}
    m = Mechanic(
        id="930#0", time_s=60.0, end_s=75.5, name="Split Buster",
        boss_ability_ids=[930], kind="tankbuster", school="physical",
        hits=[{"time_s": 60.0, "unmitigated": dict(hit)},
              {"time_s": 75.0, "unmitigated": dict(hit)}],
        unmitigated=unmit,
        unmitigated_p90={k: v * 1.05 for k, v in unmit.items()},
        observed_mit_pct=0.2, presence_ratio=1.0)
    p = run_user([m], PinnedEntry(label="inv", boss_ability_id=930,
                                  mits=(("Paladin", HALLOWED),)))
    pm = p.mechanics[0]
    assert pm.invulned
    # Cast at 59s, 10s duration: covers the 60s hit, NOT the 75s tail.
    assert pm.predicted["tank"] == 150_000.0, pm.predicted


def test_invuln_spans_a_neighboring_mechanic():
    # Hallowed Ground cast ~59s (10s) blankets a raidwide 3s after the
    # buster: the carryover zeroes the raidwide's TANK damage; everyone
    # else's share still prices.
    ms = [mech("940#0", 60.0, kind="tankbuster",
               tank=900_000, healer=0, dps=0),
          mech("941#0", 63.0, tank=120_000, healer=150_000, dps=160_000)]
    p = run_user(ms, PinnedEntry(label="inv", boss_ability_id=940,
                                 mits=(("Paladin", HALLOWED),)))
    rw = p.mechanics[1]
    carried = [a for a in rw.assignments if a.is_carryover]
    assert any(a.action_id == HALLOWED and a.mit_pct == 1.0
               for a in carried), rw.assignments
    assert rw.predicted["tank"] == 0.0
    assert rw.predicted["healer"] > 0.0 and rw.predicted["dps"] > 0.0


def test_authored_invuln_on_raidwide_dropped_loudly():
    m = mech("901#0", 40.0)
    p = run_user([m], PinnedEntry(label="inv", boss_ability_id=901,
                                  mits=(("Paladin", HALLOWED),)))
    pm = p.mechanics[0]
    assert not pm.invulned
    assert HALLOWED not in {a.action_id for a in pm.assignments}
    assert any(w.startswith("Your plan:") and "tank buster" in w
               for w in p.warnings), p.warnings


def test_off_comp_job_warns_loudly():
    # PF sheets skip off-comp jobs silently; a user's own draft must not.
    m = mech("1000#0", 40.0)
    p = run_user([m], PinnedEntry(label="x", boss_ability_id=1000,
                                  mits=(("Scholar", 188), ("Sage", KERACHOLE))))
    pm = p.mechanics[0]
    assert 188 not in {a.action_id for a in _own(pm)}
    assert any(w.startswith("Your plan:") and "Scholar" in w
               and "not in this comp" in w for w in p.warnings), p.warnings
    # And the PF-only "uses the auto plan" H2 warning is absent — there is no
    # auto fallback in exclusive mode.
    assert not any("uses the auto plan" in w for w in p.warnings), p.warnings


def test_exclusive_deterministic():
    def fingerprint():
        ms = [mech("1100#0", 30.0, healer=180_000, dps=200_000),
              mech("1101#0", 50.0, healer=180_000, dps=200_000),
              mech("1102#0", 80.0, kind="tankbuster",
                   tank=360_000, healer=0, dps=0)]
        p = run_user(
            ms,
            PinnedEntry(label="a", boss_ability_id=1100,
                        mits=(("Sage", KERACHOLE), ("@melee", FEINT))),
            PinnedEntry(label="c", boss_ability_id=1102,
                        mits=(("Paladin", HALLOWED),)))
        return json.dumps({
            "assigns": [[asdict(a) for a in pm.assignments]
                        for pm in p.mechanics],
            "gcd": [[asdict(g) for g in pm.gcd_heals] for pm in p.mechanics],
            "summary": p.summary, "warnings": p.warnings,
        }, sort_keys=True)
    assert fingerprint() == fingerprint()


# --- parse_plan_dict -----------------------------------------------------------

def test_parse_plan_dict_roles_and_labels():
    raw = {"encounter_id": 1085, "assignments": [
        {"name": "X", "boss_ability_id": 5, "mits": [
            {"job": "Sage", "action_id": KERACHOLE},
            {"role": "melee", "action_id": FEINT},
            {"job": "Sage", "action_id": 88888888}]}]}   # bogus id
    pp = parse_plan_dict(raw, source_label="Your plan")
    assert pp.entries[0].mits == (("Sage", KERACHOLE), ("@melee", FEINT))
    assert any(w.startswith("Your plan:") and "not in the mit library" in w
               for w in pp.warnings), pp.warnings


def test_parse_plan_dict_heals():
    raw = {"assignments": [
        {"name": "X", "boss_ability_id": 5, "mits": [],
         "gcd_heals": [
             {"job": "White Mage", "action_id": MEDICA_III, "count": 2},
             {"job": "White Mage", "action_id": 99999999, "count": 1},
             {"job": "White Mage", "action_id": MEDICA_III, "count": 99}]}]}
    pp = parse_plan_dict(raw, source_label="Your plan")
    assert len(pp.entries) == 1              # a heals-only entry is kept
    e = pp.entries[0]
    assert e.mits == ()
    assert e.heals == (("White Mage", MEDICA_III, 2),
                       ("White Mage", MEDICA_III, 8))   # count clamped to 8
    assert any(w.startswith("Your plan:") and "heal dropped" in w
               for w in pp.warnings), pp.warnings


def test_parse_plan_dict_tolerates_junk():
    pp = parse_plan_dict(["not", "a", "plan"], encounter_id=7,
                         source_label="Your plan")
    assert pp.entries == () and pp.encounter_id == 7
    assert any(w.startswith("Your plan:") for w in pp.warnings)
    pp2 = parse_plan_dict({"assignments": [
        "junk-row",
        {"name": "ok", "boss_ability_id": "not-an-int",
         "mits": [{"job": "Sage", "action_id": KERACHOLE}]}]})
    assert pp2.entries == ()
    assert any("bad match keys" in w for w in pp2.warnings), pp2.warnings


def test_load_premade_equivalent_to_parse_plan_dict():
    # The refactor guard: the real premade file parses identically through the
    # file loader and the factored-out dict parser.
    import mitplan.premade as premade
    path = premade._path(1085)
    assert path.is_file(), "premade/1085.json should ship with the repo"
    via_loader = load_premade(1085)
    via_parser = parse_plan_dict(
        json.loads(path.read_text(encoding="utf-8")), encounter_id=1085)
    assert via_loader == via_parser


def test_pinned_plenary_warns_without_a_host_heal():
    # Plenary is pair-gated: pinned WITHOUT a qualifying heal in its window it
    # stays on the board (user content) but the plan says it pays nothing.
    m = mech("60#0", 90.0)
    p = run_user([m], PinnedEntry(label="X", boss_ability_id=60,
                                  mits=(("White Mage", PLENARY),)))
    pm = p.mechanics[0]
    assert any(a.action_id == PLENARY and not a.is_suggestion
               for a in pm.assignments)
    assert any("pairs with no" in w for w in p.warnings), p.warnings
    # With an authored Medica III in the gap, the rider pays — no warning.
    p2 = run_user([m], PinnedEntry(label="X", boss_ability_id=60,
                                   mits=(("White Mage", PLENARY),),
                                   heals=(("White Mage", MEDICA_III, 2),)))
    assert not any("pairs with no" in w for w in p2.warnings), p2.warnings


def test_authored_heals_trim_to_the_lily_bucket():
    # Lilies accrue in combat only (bucket starts EMPTY): 6 authored Raptures
    # before a 90s mechanic trim to the 3 lilies that can exist by ~71s, with
    # a loud notice. Costed Medica III has no pool and never trims.
    m = mech("61#0", 90.0)
    p = run_user([m], PinnedEntry(label="X", boss_ability_id=61, mits=(),
                                  heals=(("White Mage", RAPTURE, 6),)))
    pm = p.mechanics[0]
    assert [(g.name, g.count) for g in pm.gcd_heals] == [
        ("Afflatus Rapture", 3)]
    assert any("fit the lily gauge" in w for w in p.warnings), p.warnings


# --- get_mit_library (sidecar palette) ----------------------------------------

def test_get_mit_library_shape_and_leads():
    from sidecar.main import get_mit_library
    from mitplan.planner import _ACTION_BY_JOB_ID, cast_lead_for
    out = get_mit_library({"shieldHealer": "Sage", "regenHealer": "White Mage",
                           "tanks": ["Paladin", "Dark Knight"],
                           "dps": ["Samurai", "Dragoon", "Bard", "Pictomancer"]})
    slots = {s["slot"]: s for s in out["slots"]}
    assert [s["slot"] for s in out["slots"]] == [
        "T1", "T2", "H1", "H2", "D1", "D2", "D3", "D4"]
    assert slots["H1"]["job"] == "Sage" and slots["T1"]["job"] == "Paladin"
    # Shield riders (Zoe 24300, Recitation) never reach the palette — they
    # fold onto host shields automatically. Pure HEAL amps ARE placeable now:
    # Krasis 24317 (+20% received) and WHM Plenary 7433 (Confession rider)
    # carry their windows where the user puts them.
    h1_ids = {a["actionId"] for a in slots["H1"]["actions"]}
    assert KERACHOLE in h1_ids and 24300 not in h1_ids and 24317 in h1_ids
    assert HALLOWED in {a["actionId"] for a in slots["T1"]["actions"]}
    h2_ids = {a["actionId"] for a in slots["H2"]["actions"]}
    assert 7433 in h2_ids                      # Plenary: placeable heal rider
    plenary = next(a for a in slots["H2"]["actions"] if a["actionId"] == 7433)
    assert plenary["mitAll"] == 0.0            # Confession has NO mitigation
    assert plenary["healFlatPotency"] == 200
    # Prerequisite chain ships to the editor (Divine Caress -> Temperance).
    caress = next(a for a in slots["H2"]["actions"] if a["actionId"] == 37011)
    assert caress["requiresActionId"] == 16536
    assert caress["requiresWithinSec"] == 30.0
    # Pure AoE GCD heals split out as the healer's healOptions (the per-gap
    # incrementer), never drag content.
    h2_heals = {o["actionId"]: o for o in slots["H2"]["healOptions"]}
    assert h2_heals[MEDICA_III]["target"] == "party"
    assert h2_heals[CURE_II]["target"] == "single"     # tank top-up authorable
    h2_action_ids = {a["actionId"] for a in slots["H2"]["actions"]}
    assert MEDICA_III not in h2_action_ids and CURE_II not in h2_action_ids
    assert slots["T1"]["healOptions"] == []
    # Camelized per-action keys + the lead policy matches the planner's.
    for s in out["slots"]:
        for a in s["actions"]:
            row = _ACTION_BY_JOB_ID[(s["job"], a["actionId"])]
            assert a["castLeadSec"] == cast_lead_for(row), (s["job"], a)
            assert a["cooldownSec"] == row.cooldown_s
            assert a["minCastSec"] in (0.0, -10.0)
    assert out["resourcePools"]["addersgall"] == {
        "capacity": 3, "regenSec": 20.0, "startTokens": 3}
    # Lilies accrue in combat only — the bucket starts EMPTY.
    assert out["resourcePools"]["lily"] == {
        "capacity": 3, "regenSec": 20.0, "startTokens": 0}
    # Comp-keyed memo: identical requests return the same object.
    assert get_mit_library({"shieldHealer": "Sage",
                            "regenHealer": "White Mage",
                            "tanks": ["Paladin", "Dark Knight"],
                            "dps": ["Samurai", "Dragoon", "Bard",
                                    "Pictomancer"]}) is out


def test_export_mit_plan_writes_json_and_readable():
    import tempfile
    import sidecar.main as sm
    with tempfile.TemporaryDirectory() as d:
        saved = sm.MIT_PLANS_DIR
        sm.MIT_PLANS_DIR = Path(d)
        try:
            plan_doc = {"encounter_id": 1085, "assignments": [
                {"name": "X", "boss_ability_id": 5,
                 "mits": [{"job": "Sage", "action_id": KERACHOLE,
                           "_ability": "Kerachole"}]}]}
            out = sm.export_mit_plan({
                "encounterId": 1085, "fileName": "My Plan",
                "plan": plan_doc,
                "readable": "0:24  X\n         Kerachole (H1 SGE)\n"})
            p, rp = Path(out["path"]), Path(out["readablePath"])
            assert p.suffix == ".json" and rp.suffix == ".txt"
            assert rp.stem == p.stem            # same base name, side by side
            # The JSON is the payload verbatim (incl. the _ability doc key)...
            assert json.loads(p.read_text(encoding="utf-8")) == plan_doc
            # ...and re-parses through the shared validator (the _ability key
            # is documentation only — ignored, not rejected).
            pp = parse_plan_dict(json.loads(p.read_text(encoding="utf-8")),
                                 encounter_id=1085, source_label="Your plan")
            assert pp.entries[0].mits == (("Sage", KERACHOLE),)
            assert "Kerachole" in rp.read_text(encoding="utf-8")
            # No readable payload -> no .txt, no readablePath.
            out2 = sm.export_mit_plan({"encounterId": 1085,
                                       "fileName": "My Plan", "plan": plan_doc})
            assert out2["path"] != out["path"]   # collision de-collided
            assert "readablePath" not in out2
        finally:
            sm.MIT_PLANS_DIR = saved


def test_get_mit_library_rejects_bad_comp():
    from sidecar.main import get_mit_library
    try:
        get_mit_library({"shieldHealer": "Warrior"})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid shield healer must raise")


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("test_mitplan_user_plan: all passed")


if __name__ == "__main__":
    main()
