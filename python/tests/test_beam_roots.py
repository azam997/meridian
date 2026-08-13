"""The `beam_search(roots=)` seam (jobs/_core/sim/engine.py) — seeded beam roots
for the replay-prefix ceiling leg.

Three invariants:
  * passing the hand-built default root through `roots=` is byte-identical to
    the no-roots call (the seam adds nothing when unused),
  * a lock-bearing model rejects seeded roots (they skip `_locks_init`),
  * a mid-fight replayed root returns prefix ⊕ tail (prefix verbatim, tail at
    or after the cut) and never scores below the greedy continuation from the
    same state (the greedy line is inside the beam's fork set).

Run from python/:  python tests/test_beam_roots.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine
from jobs._core.sim.engine import _clone_state, _locks_init
from jobs._core.sim.replay import replay_state
from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.gunbreaker import data as gd
from jobs.gunbreaker.simulator import SimParams, _make_score, _model_for

DUR = 90.0
WIDTH = 8
_GCD_IDS = frozenset(gd.POTENCIES) - gd.OGCD_IDS

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _default_root(model, params):
    """Mirror beam_search's own root construction (init + fields + locks +
    seed_run_state + prepull)."""
    root = model.init_state()
    root.fight_duration_s = DUR
    root.downtime_windows = []
    root.buff_intervals = []
    _locks_init(model, root)
    model.seed_run_state(root)
    model.prepull(root, params)
    return root


def test_default_root_byte_identity() -> None:
    print("\nTest: roots=[hand-built default root] == no-roots call, byte-identical")
    params = SimParams()
    score = _make_score(())
    base_tl, base_aux = engine.beam_search(
        _model_for(DUR, None), score, DUR, [], params, WIDTH)
    model = _model_for(DUR, None)
    seeded_tl, seeded_aux = engine.beam_search(
        model, score, DUR, [], params, WIDTH,
        roots=[_default_root(model, params)])
    _check("timelines identical", seeded_tl == base_tl,
           f"{len(seeded_tl)} vs {len(base_tl)} casts")
    _check("aux identical", seeded_aux == base_aux,
           f"{seeded_aux} vs {base_aux}")


def test_locked_model_rejects_roots() -> None:
    print("\nTest: a lock-bearing model rejects seeded roots")
    params = SimParams()
    model = _model_for(DUR, None)
    root = _default_root(model, params)
    model.locked_gcd_windows = (("dummy-lock",),)
    try:
        engine.beam_search(model, _make_score(()), DUR, [], params, WIDTH,
                           roots=[root])
    except AssertionError:
        _check("assertion fired", True)
    else:
        _check("assertion fired", False, "locked model accepted seeded roots")


def test_midfight_seeded_root() -> None:
    print("\nTest: mid-fight replayed root -> prefix ⊕ tail, >= greedy continuation")
    params = SimParams()
    model = _model_for(DUR, None)
    score = _make_score(())
    greedy_tl, _aux = engine.run_rotation(model, DUR, [], params)
    casts = [(t, a) for t, a in greedy_tl if a != TINCTURE_ACTION_ID]
    cut = 30.0
    st = replay_state(model, casts, cut, DUR, [],
                      gcd_ids=_GCD_IDS, params=params)
    prefix = list(st.timeline)
    _check("prefix non-empty and pre-cut", bool(prefix) and prefix[0][0] < cut,
           f"{prefix[:3]}")
    cont_tl, cont_aux = engine.continue_rotation(
        model, _clone_state(st), DUR, [], params)
    beam_tl, beam_aux = engine.beam_search(
        model, score, DUR, [], params, WIDTH, roots=[_clone_state(st)])
    _check("prefix preserved verbatim", beam_tl[:len(prefix)] == prefix,
           f"{beam_tl[:len(prefix)][:5]} != {prefix[:5]}")
    _check("tail casts at/after the root clock",
           all(t >= cut - 1e-9 for t, _a in beam_tl[len(prefix):]),
           f"first tail cast {beam_tl[len(prefix):][:1]}")
    beam_score = score(beam_tl, beam_aux, None)
    cont_score = score(cont_tl, cont_aux, None)
    _check("seeded beam >= greedy continuation",
           beam_score >= cont_score - 1e-6,
           f"{beam_score} < {cont_score}")


def main() -> int:
    test_default_root_byte_identity()
    test_locked_model_rejects_roots()
    test_midfight_seeded_root()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
