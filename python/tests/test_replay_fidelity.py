"""Replay fidelity — a job's own ideal line must replay back into its own state.

The deep-advice cascade measures `C(t) = score(replay(player casts <= t) ⊕
greedy-continue)`. Feed a job the line ITS OWN greedy solver produced and the
player is, by construction, perfect: every cut's continuation resumes from a
state the engine already reached, reproduces the same tail, and

    C(t) == C(0) == C(T)   for every cut t.

Any deviation is machinery error, not player error — state the replayed stream
cannot reconstruct — and `sidecar/advice.py` clamps each segment at
`max(0.0, ...)`, so the positive half of that jitter is promoted into
`cascade_pacing` cards blaming a clean player.

Two assertions per job:

  * the telescoping anchor `C(0) - C(T)` is EXACTLY zero (it always has been —
    this pins it), and
  * the promotable phantom (the sum of positive segment losses) stays at or
    below that job's LEDGER entry.

The ledger is a regression ratchet, not a target: entries may only be lowered.
Every non-zero one names its cause. This file would have caught all three bugs
the 2026-08-13 sweep found: Monk's cascade crashing on an argument-order
mismatch, Monk's replayed chakra budget sitting at zero, and Black Mage's
Polyglot clock frozen on its seed.

Run from python/:  python tests/test_replay_fidelity.py
"""
from __future__ import annotations

import functools
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs import _JOB_PACKAGES, get_job
from jobs._core.ability_metadata import get_metadata
from jobs._core.sim import counterfactual, engine, replay
from jobs._core.tincture import TINCTURE_ACTION_ID

DUR = 300.0
CUT_EVERY = 30.0

# Promotable phantom potency per job on the pinned setup below. 0 = the replayed
# state is a perfect reconstruction. LOWER ONLY.
LEDGER: dict[str, float] = {
    "astrologian": 0.0,
    "bard": 0.0,
    "blackmage": 0.0,
    "dancer": 0.0,
    "darkknight": 0.0,
    # Float-ULP divergence in charge regeneration (the engine accumulates per
    # weave, replay per cast) flipping an exact availability boundary: at t=211
    # the engine holds 0.8749999999999993 Life Surge charges and the replay
    # 0.875, so the continuation fits one extra weave. Fixing it needs an
    # epsilon or an absolute-clock charge model — NOT byte-identical, so it
    # would need a full corpus + <=100% re-validation. See NEXT_STEPS.
    "dragoon": 313.7,
    "gunbreaker": 144.0,        # same float-boundary class (cartridge timing)
    "machinist": 0.0,
    "monk": 0.0,
    "ninja": 444.0,             # same float-boundary class (mudra charges)
    "paladin": 0.0,
    "pictomancer": 0.0,
    "reaper": 0.0,
    "redmage": 0.0,
    # Residual Kenki divergence: the replayed state accrues a growing surplus
    # against the engine's own line (25 vs 40 at 120s, 40 vs 70 at 180s, 30 vs
    # 85 at 240s) while `tengentsu_procs` pops equally on both sides — a
    # cap-clamp or release-ordering path dependence, not a missing release.
    # `catch_up_run_state` already took this from 1905.9. See NEXT_STEPS.
    "samurai": 1405.9,
    "sage": 0.0,
    "scholar": 0.0,
    "summoner": 0.0,
    "viper": 0.0,
    "warrior": 0.0,
    "whitemage": 0.0,
}

_JOBS = sorted(_JOB_PACKAGES)
_TOL = 1e-6
# Half a potency point of float slack on the ledger comparison — the sums carry
# accumulated error, and a real regression is hundreds of potency, not tenths.
_LEDGER_SLACK = 0.5


def _module(job_name: str) -> str:
    return _JOB_PACKAGES[job_name] + ".simulator"


def _slug(job_name: str) -> str:
    return _JOB_PACKAGES[job_name].rsplit(".", 1)[-1]


def _player_stream(job_name: str, timeline) -> tuple[tuple[float, int], ...]:
    """The ideal line as `norm_casts` would carry it for a perfect player: the
    solver's own casts, plus the countdown instants FFLogs drops and
    `casts.py::_inject_proven_prepull_instants` reconstructs at t=-2.0. Without
    the injection a job like SAM replays without its pre-pull Meikyo and the
    measurement blames the model for the harness's own missing cast."""
    casts = [(float(t), int(a)) for t, a in timeline if a != TINCTURE_ACTION_ID]
    have_prepull = {a for t, a in casts if t < 0}
    for ability_id in (get_job(job_name).data.prepull_buff_ids or {}):
        if ability_id not in have_prepull:
            casts.append((-2.0, int(ability_id)))
    return tuple(sorted(casts, key=lambda c: c[0]))


def _gcd_ids(job_name: str, casts) -> tuple[int, ...]:
    """Mirror `sidecar.main._run_deep_pass`: the non-oGCD ids among the stream,
    from ability metadata. The `POTENCIES - OGCD_IDS` shortcut is NOT equivalent
    — MCH ships no `OGCD_IDS` table, so it would call every oGCD a GCD and
    corrupt the replay's slot bookkeeping."""
    data = importlib.import_module(_JOB_PACKAGES[job_name] + ".data")
    ogcd = set(getattr(data, "OGCD_IDS", ()) or ())
    out = []
    for ability_id in {a for _t, a in casts}:
        meta = get_metadata(ability_id)
        if meta is not None:
            if not meta.is_ogcd:
                out.append(ability_id)
        elif ogcd and ability_id not in ogcd:
            out.append(ability_id)
    return tuple(sorted(out))


@functools.lru_cache(maxsize=None)
def _measure(job_name: str) -> tuple[float, float, tuple[float, ...]]:
    """(telescoped gap, promotable phantom, per-segment losses) for one job."""
    module = _module(job_name)
    # Resolved through the cascade's OWN resolver, so a `_model_for` signature
    # that the deep pass can't call fails here loudly (Monk shipped
    # `examined: None` for exactly that reason).
    model, params, _score = counterfactual._resolve(module, None, DUR)
    timeline, _aux = engine.run_rotation(model, DUR, [], params)
    casts = _player_stream(job_name, timeline)
    cuts = counterfactual.normalized_cuts(
        [0.0] + [k * CUT_EVERY for k in range(1, int(DUR / CUT_EVERY))] + [DUR])
    scores = counterfactual.cascade_scores(
        module, DUR, (), None, casts, cuts, _gcd_ids(job_name, casts), ())
    segments = tuple(scores[i] - scores[i + 1] for i in range(len(scores) - 1))
    phantom = sum(s for s in segments if s > 0)
    return scores[0] - scores[-1], phantom, segments


@pytest.mark.parametrize("job_name", _JOBS)
def test_cascade_telescopes_to_zero(job_name: str) -> None:
    """A perfect player's cascade has nothing to explain: C(0) == C(T)."""
    gap, _phantom, _segs = _measure(job_name)
    assert abs(gap) < _TOL, f"{job_name}: C(0) - C(T) = {gap:.3f}, expected 0"


@pytest.mark.parametrize("job_name", _JOBS)
def test_phantom_within_ledger(job_name: str) -> None:
    """No segment jitter beyond the recorded ledger — the promotable half of a
    replay-fidelity gap, which the panel would card as the player's mistake."""
    slug = _slug(job_name)
    assert slug in LEDGER, f"{job_name}: add a LEDGER entry (measure it first)"
    _gap, phantom, segs = _measure(job_name)
    assert phantom <= LEDGER[slug] + _LEDGER_SLACK, (
        f"{job_name}: promotable phantom {phantom:.1f}p exceeds ledger "
        f"{LEDGER[slug]:.1f}p — a replayed state stopped reconstructing the "
        f"sim's own. Segments: {[round(s, 1) for s in segs]}")


def test_blackmage_polyglot_clock_advances() -> None:
    """The accrual-clock regression, directly: BLM releases Polyglot from its
    pick hooks, which replay never calls, so before `catch_up_run_state` a state
    replayed to t=150 still read `polyglot_next_t = 30.0` (frozen on its seed)
    and its continuation opened with a lump release that overcapped."""
    from jobs.blackmage import data as bd

    module = "jobs.blackmage.simulator"
    model, params, _score = counterfactual._resolve(module, None, DUR)
    timeline, _aux = engine.run_rotation(model, DUR, [], params)
    casts = _player_stream("Black Mage", timeline)
    gcd_ids = frozenset(_gcd_ids("Black Mage", casts))
    state = replay.replay_state(model, casts, 150.0, DUR, [],
                                gcd_ids=gcd_ids, params=params)
    assert state.polyglot_next_t > state.t, (
        f"Polyglot clock at {state.polyglot_next_t} is already due at "
        f"t={state.t:.2f} — the replay never released it")
    assert state.polyglot_next_t == pytest.approx(
        bd.POLYGLOT_INTERVAL_S * (int(state.t / bd.POLYGLOT_INTERVAL_S) + 1)), \
        f"clock {state.polyglot_next_t} is not the next accrual after {state.t:.2f}"


def test_samurai_tengentsu_schedule_consumed() -> None:
    """The same regression on a proc schedule: every Tengentsu block due before
    the cut must be spent by the time the replayed state reaches it."""
    module = "jobs.samurai.simulator"
    model, params, _score = counterfactual._resolve(module, None, DUR)
    timeline, _aux = engine.run_rotation(model, DUR, [], params)
    casts = _player_stream("Samurai", timeline)
    gcd_ids = frozenset(_gcd_ids("Samurai", casts))
    state = replay.replay_state(model, casts, 150.0, DUR, [],
                                gcd_ids=gcd_ids, params=params)
    overdue = [p for p in state.tengentsu_procs if p <= state.t]
    assert not overdue, (
        f"{len(overdue)} Tengentsu blocks still pending at t={state.t:.2f}: "
        f"{overdue} — the replay never released them")


def main() -> int:
    worst = 0.0
    for job_name in _JOBS:
        gap, phantom, _segs = _measure(job_name)
        slug = _slug(job_name)
        ok = abs(gap) < _TOL and phantom <= LEDGER.get(slug, 0.0) + _LEDGER_SLACK
        worst = max(worst, phantom)
        print(f"  [{'OK  ' if ok else 'FAIL'}] {job_name:14s} gap={gap:8.3f} "
              f"phantom={phantom:8.1f}p (ledger {LEDGER.get(slug, 0.0):.1f})")
        if not ok:
            return 1
    test_blackmage_polyglot_clock_advances()
    test_samurai_tengentsu_schedule_consumed()
    print(f"\nAll replay-fidelity checks passed ({len(_JOBS)} jobs, "
          f"worst phantom {worst:.1f}p)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
