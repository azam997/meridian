"""The replay-prefix-seeded ceiling leg (jobs/_core/sim/seeded.py).

Invariants:
  * no strict raw win → the base candidate is returned as the SAME object
    (byte-identical ceiling, zero churn on dominated pulls),
  * a demonstrated stream that out-scores the base is adopted, and the seeded
    beam rung may only raise it further,
  * adoption is decided in the caller's final currency — a raw win that loses
    the final currency keeps the base (the never-regress guard),
  * the base's own stream never displaces it (raw tie → base), even though the
    base carries in-sim tincture markers and replay candidates never do.

Run from python/:  python tests/test_seeded_ceiling.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine
from jobs._core.sim.seeded import seeded_ceiling_max_guard
from jobs._core.tincture import TINCTURE_ACTION_ID
from jobs.gunbreaker import data as gd
from jobs.gunbreaker.simulator import SimParams, _make_score, _model_for

DUR = 120.0
WIDTH = 8
_GCD_IDS = frozenset(gd.POTENCIES) - gd.OGCD_IDS

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    (_PASSED if cond else _FAILED).append(name if cond else (name, detail))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}  {'' if cond else detail}")
    if not cond:
        raise AssertionError(f"{name}  {detail}".rstrip())


def _strip(tl):
    return [(t, a) for t, a in tl if a != TINCTURE_ACTION_ID]


def _raw(score, tl, aux):
    return score(_strip(tl), aux, None)


def _greedy(model, params):
    tl, aux = engine.run_rotation(model, DUR, [], params)
    return list(tl), aux


def test_no_win_returns_base_object() -> None:
    print("\nTest: a dominated stream leaves the base untouched (same object)")
    model = _model_for(DUR, None)
    score = _make_score(())
    base = _greedy(model, SimParams())
    casts = _strip(base[0])[:10]            # a stub of the line — cannot win
    out = seeded_ceiling_max_guard(
        model, score, DUR, [], None, base, casts,
        params_options=tuple(model.sweep_params(())),
        gcd_ids=_GCD_IDS, beam_width=WIDTH, final_score=None)
    _check("base returned as-is", out is base, "helper rebuilt the candidate")


def test_better_stream_adopted() -> None:
    print("\nTest: a demonstrated line beating the base is adopted (raw currency)")
    model = _model_for(DUR, None)
    score = _make_score(())
    # Handicapped base: No Mercy forbidden for the first 60s.
    base = _greedy(model, SimParams(
        forbidden_windows=((gd.NO_MERCY, 0.0, 60.0),)))
    full = _greedy(model, SimParams())      # the unhandicapped line
    casts = _strip(full[0])
    base_raw = _raw(score, *base)
    full_raw = _raw(score, *full)
    _check("handicap is real", full_raw > base_raw + 1.0,
           f"{full_raw} vs {base_raw}")
    out = seeded_ceiling_max_guard(
        model, score, DUR, [], None, base, casts,
        cut_times=(30.0, 60.0),
        params_options=tuple(model.sweep_params(())),
        gcd_ids=_GCD_IDS, beam_width=WIDTH, final_score=None)
    out_raw = _raw(score, *out)
    _check("adopted line >= the demonstrated full replay",
           out_raw >= full_raw - 1e-6, f"{out_raw} < {full_raw}")
    _check("adopted line beats the base strictly", out_raw > base_raw,
           f"{out_raw} <= {base_raw}")


def test_final_currency_vetoes_raw_win() -> None:
    print("\nTest: a raw win that loses the final currency keeps the base")
    model = _model_for(DUR, None)
    score = _make_score(())
    base = _greedy(model, SimParams(
        forbidden_windows=((gd.NO_MERCY, 0.0, 60.0),)))
    casts = _strip(_greedy(model, SimParams())[0])
    out = seeded_ceiling_max_guard(
        model, score, DUR, [], None, base, casts,
        cut_times=(30.0, 60.0),
        params_options=tuple(model.sweep_params(())),
        gcd_ids=_GCD_IDS, beam_width=None,
        final_score=lambda tl, aux: -_raw(score, tl, aux))   # inverted currency
    _check("base survives the inverted final currency", out is base,
           "a raw-only win was adopted")


def test_own_stream_is_a_tie() -> None:
    print("\nTest: the base's own stream never displaces it (raw tie -> base)")
    model = _model_for(DUR, None)
    score = _make_score(())
    base = _greedy(model, SimParams())      # carries in-sim pot markers
    _check("base carries tincture markers",
           any(a == TINCTURE_ACTION_ID for _t, a in base[0]),
           "greedy line did not pot — tie test loses its point")
    out = seeded_ceiling_max_guard(
        model, score, DUR, [], None, base, _strip(base[0]),
        cut_times=(45.0,),
        params_options=tuple(model.sweep_params(())),
        gcd_ids=_GCD_IDS, beam_width=WIDTH, final_score=None)
    _check("base returned as-is", out is base,
           f"raw {_raw(score, *out)} vs base {_raw(score, *base)}")


def main() -> int:
    test_no_win_returns_base_object()
    test_better_stream_adopted()
    test_final_currency_vetoes_raw_win()
    test_own_stream_is_a_tie()
    print("\n" + "=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    for item in _FAILED:
        print(f"  - {item}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
