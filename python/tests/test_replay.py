"""Unit tests for the production cast-replay facility
(jobs/_core/sim/replay.py) — the deep-advice cascade's state reconstruction.

Run from python/:  python tests/test_replay.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine
from jobs._core.sim.counterfactual import _snapshot
from jobs._core.sim.replay import replay_prefix_states, replay_state
from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.machinist.simulator import SimParams, _model_for

DRILL = 16498
DUR = 120.0
_POT_FREE = SimParams(forbidden_windows=((TINCTURE_ACTION_ID, 0.0, DUR),))

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _gcd_ids(timeline) -> frozenset[int]:
    """GCD-ness straight from the model's cooldown/potency tables would need
    metadata; for these tests, everything the sim cast that isn't an obvious
    oGCD id set is irrelevant — slot-end tracking only needs SOME GCD ids, and
    Drill is the one the tests reason about."""
    return frozenset({DRILL})


def test_charge_regen_via_advance_time() -> None:
    print("\nTest: replay regenerates fractional charges (the advance_time rule)")
    model = _model_for(None)
    st10 = replay_state(model, [(0.0, DRILL)], 10.0, DUR, [],
                        gcd_ids=frozenset({DRILL}), params=_POT_FREE)
    # Drill: 2 charges, 20s recast. One cast at t=0 -> 1 charge; +10s -> +0.5.
    _check("charges[Drill] ≈ 1.5 at t=10",
           abs(st10.charges.get(DRILL, -1) - 1.5) < 1e-9,
           f"got {st10.charges.get(DRILL)}")
    st30 = replay_state(model, [(0.0, DRILL)], 30.0, DUR, [],
                        gcd_ids=frozenset({DRILL}), params=_POT_FREE)
    _check("charges[Drill] capped at 2.0 by t=30",
           abs(st30.charges.get(DRILL, -1) - 2.0) < 1e-9,
           f"got {st30.charges.get(DRILL)}")


def test_prepull_and_unknown_ids() -> None:
    print("\nTest: pre-pull casts keep their negative time; unknown ids no-op")
    model = _model_for(None)
    st = replay_state(model, [(-2.0, DRILL), (1.0, 999999)], 5.0, DUR, [],
                      gcd_ids=frozenset({DRILL}), params=_POT_FREE)
    _check("prepull cast recorded at its real (negative) time",
           st.timeline[0] == (-2.0, DRILL), f"got {st.timeline[:2]}")
    _check("unknown id realized as a structural no-op cast",
           (1.0, 999999) in st.timeline, f"got {st.timeline}")
    _check("clock advanced to the cut", abs(st.t - 5.0) < 1e-9, f"t={st.t}")
    _snapshot(st)   # must not raise


def test_tincture_marker_skipped() -> None:
    print("\nTest: sim-internal tincture markers never replay")
    model = _model_for(None)
    st = replay_state(model, [(0.0, DRILL), (1.0, TINCTURE_ACTION_ID)],
                      5.0, DUR, [], gcd_ids=frozenset({DRILL}),
                      params=_POT_FREE)
    _check("marker filtered from the replayed timeline",
           all(a != TINCTURE_ACTION_ID for _t, a in st.timeline),
           f"got {st.timeline}")


def test_prefix_states_match_single_replays() -> None:
    print("\nTest: the incremental walk == one replay_state per cut")
    model = _model_for(None)
    timeline, _aux = engine.run_rotation(model, DUR, [], _POT_FREE)
    casts = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    cuts = [30.05, 60.15, 90.25]
    walked = replay_prefix_states(model, casts, cuts, DUR, [],
                                  gcd_ids=_gcd_ids(timeline),
                                  params=_POT_FREE)
    _check("one state per cut, ascending",
           [c for c, _s in walked] == sorted(cuts),
           f"got {[c for c, _s in walked]}")
    for cut, st in walked:
        solo = replay_state(model, casts, cut, DUR, [],
                            gcd_ids=_gcd_ids(timeline), params=_POT_FREE)
        _check(f"cut {cut}: snapshots identical",
               _snapshot(st) == _snapshot(solo),
               f"{_snapshot(st)} != {_snapshot(solo)}")


def test_determinism() -> None:
    print("\nTest: same stream, same cut → byte-identical snapshot")
    model = _model_for(None)
    timeline, _aux = engine.run_rotation(model, DUR, [], _POT_FREE)
    casts = [(t, a) for t, a in timeline if a != TINCTURE_ACTION_ID]
    a = _snapshot(replay_state(model, casts, 61.3, DUR, [],
                               gcd_ids=_gcd_ids(timeline), params=_POT_FREE))
    b = _snapshot(replay_state(model, casts, 61.3, DUR, [],
                               gcd_ids=_gcd_ids(timeline), params=_POT_FREE))
    _check("snapshots equal", a == b, f"{a} != {b}")


def main() -> int:
    test_charge_regen_via_advance_time()
    test_prepull_and_unknown_ids()
    test_tincture_marker_skipped()
    test_prefix_states_match_single_replays()
    test_determinism()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
