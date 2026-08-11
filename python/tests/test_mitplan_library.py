"""Mitigation-action library sanity (pure data checks).

The VALUES themselves are cross-checked against observed per-hit multipliers
by scripts/validate_mit_values.py (network); this suite pins the shape: every
duo has the tools the planner assumes, every non-healer job brings party
mitigation, and each row is internally consistent.

Run from python/:  python tests/test_mitplan_library.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mitplan.library import (  # noqa: E402
    ACTIONS, ALL_JOB_NAMES, DPS_JOBS, FILLER_GCD_POTENCY, HEALER_JOBS,
    REGEN_HEALERS, RESOURCE_POOLS, ROLE_MAX_HP_DEFAULT, SHIELD_HEALERS,
    TANK_JOBS, Target, Tier, actions_for_job, internal_job_name, role_for_job,
)


def test_duo_toolkit_coverage():
    for shield in SHIELD_HEALERS:
        for regen in REGEN_HEALERS:
            rows = actions_for_job(shield) + actions_for_job(regen)
            assert any(a.is_mit and a.target == Target.PARTY for a in rows), \
                (shield, regen, "party mit")
            assert any(a.is_shield and a.target == Target.PARTY for a in rows), \
                (shield, regen, "party shield")
            assert any(a.recovery and a.target == Target.PARTY for a in rows), \
                (shield, regen, "party recovery")
            assert any(a.target == Target.SINGLE and (a.is_mit or a.is_shield)
                       for a in rows), (shield, regen, "single-target tool")
            # The regen healer must expose an insertable AoE GCD heal.
            assert any(a.tier is Tier.HEALER_GCD and a.target == Target.PARTY
                       and a.heal_potency > 0
                       for a in actions_for_job(regen)), (regen, "aoe gcd")


def test_every_non_healer_brings_party_mitigation():
    for job in TANK_JOBS + DPS_JOBS:
        rows = [a for a in actions_for_job(job) if a.tier is Tier.PARTY_OTHER]
        assert rows, job
        assert any(a.is_mit or a.is_shield for a in rows), job


def test_tanks_have_suggestions_and_invulns():
    for job in TANK_JOBS:
        rows = actions_for_job(job)
        assert any(a.tier is Tier.TANK_SUGGESTION for a in rows), job
        assert sum(1 for a in rows if a.tier is Tier.INVULN) == 1, job
    # post_hp1 on exactly the three invulns that leave the tank needing a
    # full re-heal; Hallowed Ground keeps HP intact.
    debts = {a.name: a.post_hp1 for a in ACTIONS if a.tier is Tier.INVULN}
    assert debts == {"Hallowed Ground": False, "Holmgang": True,
                     "Living Dead": True, "Superbolide": True}


def test_row_sanity():
    for a in ACTIONS:
        ctx = (a.job, a.name)
        assert a.job in ALL_JOB_NAMES, ctx
        assert a.action_id > 0, ctx
        assert 0.0 <= a.mit_all <= 0.40 and 0.0 <= a.mit_phys <= 0.40 \
            and 0.0 <= a.mit_magic <= 0.40, ctx
        if a.tier is not Tier.INVULN:
            # No inert rows: every action either carries its own value or
            # amplifies a host that does.
            assert a.is_mit or a.is_shield or a.heal_potency > 0 \
                or a.heal_pct_maxhp > 0 or a.regen_potency_per_tick > 0 \
                or a.is_amplifier, ctx
        if a.cooldown_s > 0 and not a.is_gcd:
            assert a.duration_s <= a.cooldown_s * a.charges, ctx
        assert a.charges >= 1, ctx
        if a.is_gcd:
            assert a.tier is Tier.HEALER_GCD, ctx
            assert a.gcd_cost_potency >= 0.0, ctx
        if a.resource is not None:
            assert a.resource in RESOURCE_POOLS, ctx
            assert a.job in HEALER_JOBS, ctx
        if a.regen_potency_per_tick > 0:
            assert a.regen_ticks > 0 and a.duration_s > 0, ctx
        if a.tier in (Tier.TANK_SUGGESTION, Tier.INVULN):
            assert a.job in TANK_JOBS and a.target == Target.SELF, ctx


def test_amplifier_rows_are_well_formed():
    """An amp scales a host; the schema must say which host, and how."""
    by_job_name = {(a.job, a.name): a for a in ACTIONS}
    for a in ACTIONS:
        ctx = (a.job, a.name)
        if a.heal_mult > 0:
            assert a.heal_mult_scope in ("caster", "receiver"), ctx
            assert a.heal_mult <= 0.50, ctx
        else:
            assert not a.heal_mult_scope, ctx
        if a.shield_mult_windowed:
            # A window with no duration amplifies nothing.
            assert a.shield_mult > 0 and a.duration_s > 0, ctx
        if a.shield_mult > 0:
            # A rider is inert without a host, and the host must be a shield
            # on the same job (the duo's other healer can't power it).
            assert a.amp_partner, ctx
            assert a.shield_mult <= 1.0, ctx
            for host in a.amp_partner:
                assert (a.job, host) in by_job_name, (ctx, host)
                assert by_job_name[(a.job, host)].is_shield, (ctx, host)
        else:
            assert not a.amp_partner, ctx


def test_stack_groups_are_consistent():
    by_group: dict[str, set] = {}
    for a in ACTIONS:
        if a.stack_group:
            by_group.setdefault(a.stack_group, set()).add(
                (a.mit_all, a.mit_phys, a.mit_magic, a.duration_s))
    # Same non-stacking status → identical effect rows (Reprisal is Reprisal).
    for group, effects in by_group.items():
        if group in ("reprisal", "feint", "addle"):
            assert len(effects) == 1, (group, effects)


def test_constants_and_name_mapping():
    assert set(FILLER_GCD_POTENCY) == set(HEALER_JOBS)
    assert set(ROLE_MAX_HP_DEFAULT) == {"tank", "healer", "dps"}
    assert internal_job_name("WhiteMage") == "White Mage"
    assert internal_job_name("DarkKnight") == "Dark Knight"
    assert internal_job_name("Samurai") == "Samurai"
    assert internal_job_name("LimitBreak") is None
    assert internal_job_name("Unknown") is None
    assert role_for_job("Gunbreaker") == "tank"
    assert role_for_job("Sage") == "healer"
    assert role_for_job("Pictomancer") == "dps"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [OK  ] {name}")
    print("test_mitplan_library: all passed")


if __name__ == "__main__":
    main()
