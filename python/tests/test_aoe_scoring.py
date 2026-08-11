"""Per-target AoE scoring math (jobs._core.sim.aoe_potency).

Pins the `potency_for` / `n_at` / `schedule_target_fn` contract the whole
multi-target arc rests on — in particular the **byte-identical at n<=1**
invariant that keeps single-target pulls unchanged. Runs under pytest and
standalone (its `main()` runs the same asserts).
"""
from __future__ import annotations

from jobs._core.job import JobData
from jobs._core.sim.aoe_potency import (
    DEFAULT_AOE_CAP, TargetSpec, n_at, potency_for, schedule_target_fn)


def _jd() -> JobData:
    """A throwaway JobData exercising every potency class."""
    return JobData(
        job_name="Test", patch_version="7.x",
        potencies={1: 300, 2: 140, 3: 500, 4: 660, 5: 0},
        splash_potencies={2: 98},          # free-splash: 140 primary + 98/extra (falloff)
        aoe_potencies={3: 500, 4: 462},    # 3 = full-to-all (==primary); 4 = falloff
        aoe_target_caps={4: 3},            # ability 4 caps at 3 targets
    )


def test_n1_byte_identical():
    """At n<=1 (and for an unknown id) potency_for == POTENCIES.get(aid, 0)."""
    jd = _jd()
    for aid in (1, 2, 3, 4, 5, 999):
        assert potency_for(aid, 1, jd) == jd.potencies.get(aid, 0)
        assert potency_for(aid, 0, jd) == jd.potencies.get(aid, 0)


def test_st_ability_ignores_n():
    """An ability with no splash/aoe entry never scales with n."""
    jd = _jd()
    for n in (1, 2, 5, 8):
        assert potency_for(1, n, jd) == 300


def test_splash_scaling():
    """Free-splash: primary + secondary*(n-1)."""
    jd = _jd()
    assert potency_for(2, 2, jd) == 140 + 98 * 1
    assert potency_for(2, 3, jd) == 140 + 98 * 2


def test_full_to_all_aoe():
    """Full-to-all (secondary == primary) => primary * n."""
    jd = _jd()
    assert potency_for(3, 1, jd) == 500
    assert potency_for(3, 2, jd) == 500 * 2
    assert potency_for(3, 5, jd) == 500 * 5


def test_falloff_and_cap():
    """Falloff secondary plus a per-ability target cap."""
    jd = _jd()
    assert potency_for(4, 2, jd) == 660 + 462 * 1
    assert potency_for(4, 3, jd) == 660 + 462 * 2
    assert potency_for(4, 8, jd) == 660 + 462 * 2   # capped at 3 targets


def test_default_cap():
    """Abilities with no explicit cap use DEFAULT_AOE_CAP."""
    jd = _jd()
    assert potency_for(3, DEFAULT_AOE_CAP, jd) == 500 * DEFAULT_AOE_CAP
    assert potency_for(3, DEFAULT_AOE_CAP + 5, jd) == 500 * DEFAULT_AOE_CAP


def test_n_at():
    sched = ((10.0, 20.0, 3), (30.0, 40.0, 2))
    assert n_at(5.0, sched) == 1          # outside every interval
    assert n_at(10.0, sched) == 3         # start inclusive
    assert n_at(19.999, sched) == 3
    assert n_at(20.0, sched) == 1         # end exclusive
    assert n_at(35.0, sched) == 2
    assert n_at(5.0, ()) == 1
    assert n_at(5.0, None) == 1


def test_schedule_target_fn():
    assert schedule_target_fn(()) is None
    assert schedule_target_fn(None) is None
    fn = schedule_target_fn(((10.0, 20.0, 4),))
    assert fn(15.0, 999) == 4
    assert fn(5.0, 999) == 1


def test_target_spec_duck_typing():
    """`schedule_target_fn` / `n_at` accept a TargetSpec exactly like the bare
    schedule tuple — an empty-schedule spec still maps to None (byte-identical
    single-target path), and capless specs behave like the bare tuple."""
    sched = ((10.0, 20.0, 4),)
    assert schedule_target_fn(TargetSpec()) is None
    assert n_at(15.0, TargetSpec(schedule=sched)) == 4
    fn_bare = schedule_target_fn(sched)
    fn_spec = schedule_target_fn(TargetSpec(schedule=sched))
    for t, aid in ((15.0, 1), (15.0, 999), (5.0, 1)):
        assert fn_spec(t, aid) == fn_bare(t, aid)


def test_target_spec_ability_caps():
    """The observed-reach caps bind per ability: a capped id is held at its
    observed max, an uncapped id keeps the schedule N, and a cap above the
    schedule N never lifts it."""
    spec = TargetSpec(schedule=((10.0, 20.0, 4),),
                      ability_caps=((7, 2), (8, 6)))
    fn = schedule_target_fn(spec)
    assert fn(15.0, 7) == 2      # cap binds (observed max 2 < schedule 4)
    assert fn(15.0, 8) == 4      # cap above schedule N -> schedule N
    assert fn(15.0, 999) == 4    # unobserved ability -> uncapped schedule N
    assert fn(5.0, 7) == 1       # outside the window: single target regardless


def main() -> None:
    test_n1_byte_identical()
    test_st_ability_ignores_n()
    test_splash_scaling()
    test_full_to_all_aoe()
    test_falloff_and_cap()
    test_default_cap()
    test_n_at()
    test_schedule_target_fn()
    test_target_spec_duck_typing()
    test_target_spec_ability_caps()
    print("aoe_scoring: all checks passed")


if __name__ == "__main__":
    main()
