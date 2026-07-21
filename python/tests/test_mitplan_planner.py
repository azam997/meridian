"""Mitigation-planner scheduling tests (pure, synthetic DamageModel + the real
action library).

Covers: cooldown/charge feasibility, stack-group non-stacking, the party-mit →
healer-oGCD → healer-GCD tier order, tank-buster suggestions + invuln
escalation, cross-mechanic carryover, GCD-heal budgeting + pricing, severity
reservation of long cooldowns, and byte-identical determinism.

Run from python/:  python tests/test_mitplan_planner.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitplan.classify import DamageModel, Mechanic  # noqa: E402
from mitplan.planner import (  # noqa: E402
    Assignment, PlanMechanic, _apply_windowed_shield_amps, _predicted_for,
    _Ctx, plan,
)

ROLE_HP = {"tank": 320_000.0, "healer": 205_000.0, "dps": 226_000.0}
COMP = dict(shield_healer="Sage", regen_healer="White Mage",
            tanks=["Paladin", "Dark Knight"],
            dps=["Samurai", "Dragoon", "Bard", "Pictomancer"])


def mech(mid, t, kind="raidwide", school="magical", tank=100_000,
         healer=160_000, dps=170_000, hits=None, end=None):
    unmit = {"tank": float(tank), "healer": float(healer), "dps": float(dps)}
    return Mechanic(
        id=mid, time_s=float(t), end_s=float(end if end is not None else t + 0.5),
        name=mid, boss_ability_ids=[int(mid.split("#")[0])],
        kind=kind, school=school,
        hits=hits or [{"time_s": float(t), "unmitigated": dict(unmit)}],
        unmitigated=unmit,
        unmitigated_p90={k: v * 1.05 for k, v in unmit.items()},
        observed_mit_pct=0.2, presence_ratio=1.0,
    )


def model(mechanics, **over):
    kw = dict(
        mechanics=mechanics, avoidable_count=0, ref_count=10,
        model_kill_s=max((m.time_s for m in mechanics), default=60.0) + 30.0,
        ref_avg_kill_s=500.0, role_hp=dict(ROLE_HP), hp_source="logs",
        tank_drain_hps=2_000.0,
        magnitudes={"shield_hp_by_status": {}},
        hp_per_potency={"_default": 80.0},
        downtime_windows=[], encounter_id=101, encounter_name="Test",
    )
    kw.update(over)
    return DamageModel(**kw)


def run(mechanics, tanks=None, dps=None, **over):
    return plan(model(mechanics, **over), COMP["shield_healer"],
                COMP["regen_healer"], tanks or COMP["tanks"],
                dps or COMP["dps"])


def run_comp(mechanics, shield="Scholar", regen="Astrologian",
             tanks=("Paladin", "Warrior"),
             dps=("Samurai", "Dragoon", "Bard", "Pictomancer"), **over):
    # A comp whose healers bring party oGCDs (AST Sun Sign, SCH Sacred Soil).
    return plan(model(mechanics, **over), shield, regen, list(tanks), list(dps))


def hpset(mid, t):
    zeros = {"tank": 0.0, "healer": 0.0, "dps": 0.0}
    return Mechanic(
        id=mid, time_s=float(t), end_s=float(t) + 1.0, name="HP set to 1",
        boss_ability_ids=[], kind="hpSet", school="unknown",
        hits=[{"time_s": float(t), "unmitigated": dict(zeros)}],
        unmitigated=dict(zeros), unmitigated_p90=dict(zeros),
        observed_mit_pct=0.0, presence_ratio=1.0,
        notes=["Sets the party to 1 HP — unmitigable; heal up after."],
    )


def plan_fingerprint(p) -> str:
    body = {
        "summary": p.summary,
        "mechs": [{
            "id": pm.mech.id, "status": pm.status,
            "predicted": pm.predicted, "hp_after": pm.hp_after,
            "assignments": [asdict(a) for a in pm.assignments],
            "gcd_heals": [asdict(g) for g in pm.gcd_heals],
        } for pm in p.mechanics],
    }
    return json.dumps(body, sort_keys=True)


def test_feasibility_and_validation():
    # A dense raidwide chain: every (slot, action) timeline must satisfy its
    # cooldown (plan() runs _validate internally — reaching here IS the test),
    # and no same-action double-cast may violate its recast.
    mechanics = [mech(f"9{i}#0", 20.0 + 25.0 * i) for i in range(10)]
    p = run(mechanics)
    seen: dict[tuple[str, int], list[float]] = {}
    for pm in p.mechanics:
        for a in pm.assignments:
            if not a.is_carryover:
                seen.setdefault((a.slot, a.action_id), []).append(a.cast_at_s)
    from mitplan.library import ACTIONS
    by_id = {(x.job, x.action_id): x for x in ACTIONS}
    for (slot, aid), casts in seen.items():
        job = next(j for s, j in p.slots if s == slot)
        row = by_id[(job, aid)]
        casts = sorted(set(casts))
        if row.cooldown_s > 0 and row.charges == 1:
            for x, y in zip(casts, casts[1:]):
                assert y - x >= row.cooldown_s - 1e-6, (slot, aid, casts)


def test_stack_group_never_stacks():
    ctx = _Ctx(model([mech("1#0", 30.0)]), [("T1", "Paladin"), ("T2", "Warrior")])
    m = mech("1#0", 30.0, school="magical")

    def reprisal(slot):
        return Assignment(slot=slot, job="Paladin" if slot == "T1" else "Warrior",
                          action_id=7535, name="Reprisal", cast_at_s=28.0,
                          duration_s=15.0, target="enemy", mit_pct=0.10,
                          shield_amount=0.0, heal_amount=0.0, hot_hps=0.0,
                          is_gcd=False, cast_time_s=0.0, is_suggestion=False,
                          covers=[30.0])

    one = _predicted_for(m, [reprisal("T1")], ctx)
    two = _predicted_for(m, [reprisal("T1"), reprisal("T2")], ctx)
    assert abs(one["dps"] - two["dps"]) < 1.0          # second Reprisal free-rides
    assert abs(one["dps"] - 170_000 * 0.9) < 1.0


def test_tier_order_gcd_shields_last():
    # Moderate raidwide → party mit + healer oGCDs suffice; no GCD rows.
    p = run([mech("1#0", 30.0)])
    a0 = p.mechanics[0].assignments
    assert a0, "expected assignments"
    assert not any(a.is_gcd for a in a0)
    # A dense monster chain (6 huge hits, 10s apart) drains tiers 1–2 — every
    # party tool and healer oGCD ends up on cooldown — and only then does the
    # shield healer's GCD shield appear (tier 3, priced).
    monsters = [mech(f"2#{i}", 30.0 + 10.0 * i,
                     tank=280_000, healer=430_000, dps=470_000)
                for i in range(6)]
    p2 = run(monsters)
    gcd_rows = [(pm.mech.id, a.name) for pm in p2.mechanics
                for a in pm.assignments
                if a.is_gcd and not a.is_carryover]
    assert gcd_rows, "expected a GCD shield under cooldown contention"
    # And they never appear on the FIRST hits, where oGCDs still flowed.
    assert all(mid not in ("2#0", "2#1") for mid, _ in gcd_rows), gcd_rows


def test_invuln_first_buster():
    # A feasible buster is planned AS the invuln: one primary suggestion,
    # zero healer tools or party mitigation spent, covered, tank alternation.
    p = run([mech("3#0", 45.0, kind="tankbuster",
                  tank=380_000, healer=0, dps=0),
             mech("3#1", 105.0, kind="tankbuster",
                  tank=380_000, healer=0, dps=0)])
    from mitplan.library import ACTIONS, Tier
    invuln_ids = {a.action_id for a in ACTIONS if a.tier is Tier.INVULN}
    slots_seen = []
    for pm in p.mechanics:
        assert pm.invulned and pm.stop_reason == "invulned"
        assert pm.status == "covered"
        assert len(pm.assignments) == 1
        a = pm.assignments[0]
        assert a.action_id in invuln_ids and a.is_suggestion
        slots_seen.append(a.slot)
        assert pm.predicted["tank"] == 0.0
    assert slots_seen == ["T1", "T2"]   # buster alternation


def test_invuln_recast_fallback():
    # Third buster lands back on T1 while Hallowed Ground (420s) is still
    # down — it must fall back to healer tools + personals, no invuln.
    busters = [mech(f"4#{i}", 45.0 + 60.0 * i, kind="tankbuster",
                    tank=380_000, healer=0, dps=0) for i in range(3)]
    p = run(busters)
    by_id = {pm.mech.id: pm for pm in p.mechanics}
    assert by_id["4#0"].invulned and by_id["4#1"].invulned
    pm3 = by_id["4#2"]
    assert not pm3.invulned
    assert any(not a.is_suggestion for a in pm3.assignments), \
        "fallback buster should consume real tools"
    from mitplan.library import ACTIONS, Tier
    invuln_ids = {a.action_id for a in ACTIONS if a.tier is Tier.INVULN}
    assert not any(a.action_id in invuln_ids for a in pm3.assignments)


def test_carryover_credits_but_never_double_counts():
    # Two heavy raidwides 8s apart: the 15s mit tools cast for the first (needs a
    # real stack, not one big shield) blanket the second.
    p = run([mech("5#0", 40.0, dps=280_000, healer=266_000),
             mech("5#1", 48.0, dps=280_000, healer=266_000)])
    first, second = p.mechanics
    carried = [a for a in second.assignments if a.is_carryover]
    assert carried, "expected carryover from the first raidwide"
    assert all(a.shield_amount == 0.0 for a in carried)
    # Carryovers must not re-count as GCD heals or duplicate lane casts.
    ids_first = {(a.slot, a.action_id, a.cast_at_s)
                 for a in first.assignments if not a.is_carryover}
    for a in carried:
        assert (a.slot, a.action_id, a.cast_at_s) in ids_first


def test_gcd_heal_budgeting_and_pricing():
    # Back-to-back heavy raidwides with a tiny gap: recovery can't repay the
    # debt, so AoE GCD heals appear and are priced into the summary.
    mechanics = [mech(f"6#{i}", 20.0 + 6.0 * i,
                      dps=210_000, healer=195_000, tank=120_000)
                 for i in range(4)]
    p = run(mechanics)
    assert p.summary["gcd_heal_count"] > 0
    total = sum(g.count for pm in p.mechanics for g in pm.gcd_heals)
    assert total == p.summary["gcd_heal_count"] - sum(
        1 for pm in p.mechanics for a in pm.assignments
        if a.is_gcd and not a.is_suggestion and not a.is_carryover)
    assert p.summary["gcd_heal_potency_lost"] >= 0.0


def test_severity_reserves_long_cooldowns():
    # Two mechanics 20s apart, the LATER one far more dangerous: severity
    # order lets the big one pick first, so it must reach its goal (the small
    # one may not steal the tools it needed). Cheap-tool preference is fine —
    # the invariant is the big hit's outcome, not which named tool it used.
    small = mech("7#0", 50.0, dps=150_000, healer=140_000, tank=90_000)
    big = mech("7#1", 70.0, dps=225_000, healer=204_000, tank=140_000)
    p = run([small, big])
    by_id = {pm.mech.id: pm for pm in p.mechanics}
    assert by_id["7#1"].stop_reason == "goal_met"
    assert by_id["7#1"].status in ("covered", "tight")
    own_big = [a for a in by_id["7#1"].assignments if not a.is_carryover]
    assert own_big, "the big hit picked first — it must own assignments"


def test_post_hp1_collision_guard():
    # T1 = Gunbreaker -> its invuln (Superbolide) leaves the tank at 1 HP.
    # With a heavy tank hit 8s after the buster, the guard must refuse it;
    # when the follow-up is an HP-set instead, the debt is irrelevant.
    tanks = ["Gunbreaker", "Paladin"]
    buster = mech("5#0", 45.0, kind="tankbuster", tank=380_000, healer=0, dps=0)
    heavy = mech("5#1", 53.0, tank=140_000, healer=180_000, dps=190_000)
    p = run([buster, heavy], tanks=tanks)
    pm = {x.mech.id: x for x in p.mechanics}["5#0"]
    assert not pm.invulned, "post-hp1 invuln must respect the follow-up hit"

    p2 = run([mech("5#0", 45.0, kind="tankbuster", tank=380_000, healer=0, dps=0),
              hpset("hpset#0", 53.0)], tanks=tanks)
    pm2 = {x.mech.id: x for x in p2.mechanics}["5#0"]
    assert pm2.invulned, "an HP-set right after makes the 1-HP debt moot"
    assert pm2.invuln_post_hp1
    # Sweep: the tank is at 1 HP after the invulned buster, everyone after the set.
    assert pm2.hp_after["tank"] <= 1.0
    hp_pm = {x.mech.id: x for x in p2.mechanics}["hpset#0"]
    assert all(hp_pm.hp_after[r] <= 1.0 for r in ("tank", "healer", "dps"))


def test_hpset_semantics():
    before = mech("6#0", 150.0, tank=110_000, healer=170_000, dps=180_000)
    after = mech("6#1", 200.0, tank=110_000, healer=170_000, dps=180_000)
    p = run([before, hpset("hpset#0", 170.0), after])
    by_id = {pm.mech.id: pm for pm in p.mechanics}
    hp_pm = by_id["hpset#0"]
    assert hp_pm.assignments == [] and hp_pm.gcd_heals == []
    assert hp_pm.status == "covered" and hp_pm.stop_reason == "hp_set"
    assert all(hp_pm.hp_after[r] <= 1.0 for r in ("tank", "healer", "dps"))
    # The mechanic before the set is planned survival-only: relax note + fewer
    # tools than the identical mechanic in a control run without the set.
    assert any("set to 1 HP right after" in n for n in by_id["6#0"].notes)
    control = run([mech("6#0", 150.0, tank=110_000, healer=170_000, dps=180_000)])
    n_relaxed = len([a for a in by_id["6#0"].assignments if not a.is_carryover])
    n_control = len([a for a in control.mechanics[0].assignments
                     if not a.is_carryover])
    assert n_relaxed < n_control, (n_relaxed, n_control)
    # Recovery after the reset: the next mechanic is survivable (not uncovered).
    assert by_id["6#1"].status in ("covered", "tight")


def test_buster_no_dps_party_mit():
    # Even when the invuln is unavailable (three same-tank-cycle busters), a
    # buster never spends DPS-cast party mitigation — that stays banked for
    # raid damage. Tank-cast Reprisal remains allowed.
    busters = [mech(f"70#{i}", 45.0 + 60.0 * i, kind="tankbuster",
                    tank=380_000, healer=0, dps=0) for i in range(3)]
    p = run(busters)
    dps_slots = {"D1", "D2", "D3", "D4"}
    from mitplan.library import ACTIONS, Tier
    party_other_ids = {a.action_id for a in ACTIONS if a.tier is Tier.PARTY_OTHER}
    for pm in p.mechanics:
        for a in pm.assignments:
            if a.is_carryover:
                continue
            assert not (a.slot in dps_slots and a.action_id in party_other_ids), \
                (pm.mech.id, a.slot, a.name)


def test_buster_banks_healer_party_ogcds():
    # The Sun Sign class of bug: a healer's PARTY-scoped oGCD (AST Sun Sign,
    # SCH Sacred Soil) is raid mitigation — it must never be spent on a tank
    # buster, even one the invuln can't reach. Three same-tank-cycle busters
    # exhaust the invulns, so the 3rd runs the greedy; it may lean on
    # single-target tank tools / personals / Reprisal, but no party oGCD.
    from mitplan.library import ACTIONS, Target
    party_ids = {a.action_id for a in ACTIONS if a.target == Target.PARTY}
    busters = [mech(f"80#{i}", 45.0 + 55.0 * i, kind="tankbuster",
                    tank=300_000, healer=0, dps=0) for i in range(3)]
    p = run_comp(busters)
    assert any(not pm.invulned for pm in p.mechanics), \
        "expected a non-invulned buster to actually exercise the greedy guard"
    for pm in p.mechanics:
        for a in pm.assignments:
            if a.is_carryover:
                continue
            assert a.action_id not in party_ids, (pm.mech.id, a.slot, a.name)


def test_party_mit_reaches_raidwide_not_buster():
    # The bank is not a global ban: with a buster then a heavy raidwide, the
    # party oGCDs stay available and the raidwide draws on them.
    from mitplan.library import ACTIONS, Target
    party_ids = {a.action_id for a in ACTIONS if a.target == Target.PARTY}
    mechs = [mech("81#0", 45.0, kind="tankbuster", tank=300_000, healer=0, dps=0),
             mech("81#1", 220.0, tank=150_000, healer=280_000, dps=300_000)]
    p = run_comp(mechs)
    by_id = {pm.mech.id: pm for pm in p.mechanics}
    buster_party = [a for a in by_id["81#0"].assignments
                    if a.action_id in party_ids and not a.is_carryover]
    assert not buster_party, buster_party
    rw_party = [a for a in by_id["81#1"].assignments
                if a.action_id in party_ids and not a.is_carryover]
    assert rw_party, "the raidwide should draw on the banked party mitigation"


def test_neutral_sect_used_on_raidwide():
    # Neutral Sect (Sun Sign, id 37031) is a major AST raid cooldown — 10% party
    # mit PLUS the amplified-Helios barrier — not a bare 10% mit. On a heavy
    # raidwide the plan must actually USE it (maximal use), never hoard it. A
    # buster in the same run must NOT get it (party mit stays banked there).
    p = run_comp([mech("95#0", 60.0, dps=290_000, healer=270_000, tank=165_000),
                  mech("95#1", 120.0, kind="tankbuster",
                       tank=300_000, healer=0, dps=0)])
    by_id = {pm.mech.id: pm for pm in p.mechanics}
    rw = [a for a in by_id["95#0"].assignments
          if a.action_id == 37031 and not a.is_carryover]
    assert rw, "Neutral Sect should be used on a heavy raidwide, not hoarded"
    bus = [a for a in by_id["95#1"].assignments
           if a.action_id == 37031 and not a.is_carryover]
    assert not bus, "Neutral Sect (party mit) must stay off the tank buster"


def test_topup_shrinks_costed_gcd_bill():
    # "Exhaust oGCD before dipping into GCD resources." Three moderate raidwides
    # with weak recovery leave the greedy at bare survival paying costed GCD
    # heals — but abundant spare oGCD mit is available. The pass-2.5 top-up must
    # spend it and strictly cut the costed GCD-heal potency (never raise it).
    import mitplan.planner as P

    def scenario():
        return [mech(f"92#{i}", 30.0 + 10.0 * i, dps=230_000, healer=213_900,
                     tank=131_100) for i in range(3)]

    orig = P.TOPUP_MAX_ROUNDS
    try:
        P.TOPUP_MAX_ROUNDS = 0
        base = run_comp(scenario(), hp_per_potency={"_default": 38.0}).summary
        P.TOPUP_MAX_ROUNDS = orig
        topped = run_comp(scenario(), hp_per_potency={"_default": 38.0}).summary
    finally:
        P.TOPUP_MAX_ROUNDS = orig
    b, t = base["gcd_heal_potency_lost"], topped["gcd_heal_potency_lost"]
    assert b > 0, b            # the scenario genuinely forces costed GCD heals
    assert t <= b, (t, b)      # top-up is monotone — it never costs more
    assert t < b, (t, b)       # and with spare oGCD it strictly helps


def _shield_asn(name, cast_at, amount=30_000.0, slot="H1", job="Scholar"):
    return Assignment(
        slot=slot, job=job, action_id=hash(name) % 9999 + 1, name=name,
        cast_at_s=float(cast_at), duration_s=30.0, target="party",
        mit_pct=0.0, shield_amount=float(amount), heal_amount=0.0, hot_hps=0.0,
        is_gcd=True, cast_time_s=2.0, is_suggestion=False)


def test_seraphism_amps_every_host_in_its_window():
    """Seraphism transforms BOTH GCD shields for its whole 20s, so every host
    cast inside the window is amplified — not just the first one committed."""
    inside_a = _shield_asn("Concitation", 100.0)
    inside_b = _shield_asn("Adloquium", 112.0)     # +12s — same 20s window
    outside = _shield_asn("Concitation", 400.0)    # far outside; 180s recast
    pms = [PlanMechanic(mech=mech("1#0", 100.0), assignments=[inside_a]),
           PlanMechanic(mech=mech("2#0", 112.0), assignments=[inside_b]),
           PlanMechanic(mech=mech("3#0", 400.0), assignments=[outside])]
    ctx = _Ctx(model([m.mech for m in pms]), [("H1", "Scholar")])
    _apply_windowed_shield_amps(pms, [("H1", "Scholar")], ctx)

    assert inside_a.shield_amount == 36_000.0, inside_a.shield_amount
    assert inside_b.shield_amount == 36_000.0, inside_b.shield_amount
    # Adloquium must be amped too — it becomes Manifestation, and modelling
    # only Concitation was the original under-count.
    assert outside.shield_amount == 36_000.0, outside.shield_amount
    # ...via a SECOND window: 400s is past the 180s recast, so both fire.
    assert ctx.timeline("H1", _sera()).casts == [100.0, 400.0]


def test_windowed_amp_respects_its_cooldown():
    """Two hosts 30s apart can't share one 20s window, and the 180s recast
    forbids a second Seraphism — so exactly one of them is amped."""
    early = _shield_asn("Concitation", 100.0)
    late = _shield_asn("Adloquium", 150.0)     # outside the window, inside the CD
    pms = [PlanMechanic(mech=mech("1#0", 100.0), assignments=[early]),
           PlanMechanic(mech=mech("2#0", 150.0), assignments=[late])]
    ctx = _Ctx(model([m.mech for m in pms]), [("H1", "Scholar")])
    _apply_windowed_shield_amps(pms, [("H1", "Scholar")], ctx)

    amped = [a for a in (early, late) if a.shield_amount > 30_000.0]
    assert len(amped) == 1, [early.shield_amount, late.shield_amount]
    assert len(ctx.timeline("H1", _sera()).casts) == 1


def test_windowed_amp_picks_the_richest_window():
    """Placement maximises amplified barrier, not merely the earliest host."""
    lone = _shield_asn("Concitation", 100.0, amount=10_000.0)
    rich_a = _shield_asn("Adloquium", 300.0, amount=40_000.0)
    rich_b = _shield_asn("Concitation", 310.0, amount=40_000.0)
    pms = [PlanMechanic(mech=mech("1#0", 100.0), assignments=[lone]),
           PlanMechanic(mech=mech("2#0", 300.0), assignments=[rich_a]),
           PlanMechanic(mech=mech("3#0", 310.0), assignments=[rich_b])]
    ctx = _Ctx(model([m.mech for m in pms]), [("H1", "Scholar")])
    _apply_windowed_shield_amps(pms, [("H1", "Scholar")], ctx)

    assert rich_a.shield_amount == 48_000.0, rich_a.shield_amount
    assert rich_b.shield_amount == 48_000.0, rich_b.shield_amount
    assert lone.shield_amount == 12_000.0, lone.shield_amount   # 2nd window
    assert ctx.timeline("H1", _sera()).casts == [100.0, 300.0]


def _sera():
    from mitplan.library import actions_for_job
    return next(a for a in actions_for_job("Scholar") if a.name == "Seraphism")


def test_determinism():
    mechanics = [mech(f"8#{i}", 15.0 + 21.0 * i,
                      kind="tankbuster" if i % 3 == 2 else "raidwide",
                      tank=300_000 if i % 3 == 2 else 100_000,
                      healer=0 if i % 3 == 2 else 165_000,
                      dps=0 if i % 3 == 2 else 175_000)
                 for i in range(12)]
    a = plan_fingerprint(run(mechanics))
    b = plan_fingerprint(run(mechanics))
    assert a == b


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("test_mitplan_planner: all passed")


if __name__ == "__main__":
    main()
