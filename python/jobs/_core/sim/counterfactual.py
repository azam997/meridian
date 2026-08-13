"""Prefix-cascade counterfactuals for the deep-advice pass.

The measurement primitive, in the strict buff-agnostic currency:

    C(t) = score( replay(player casts <= t)  ⊕  greedy-continue(state@t → end) )

For a segment [t0, t1], `C(t0) − C(t1)` is the potency lost by playing that
segment as the player did *given the arrival state*: the later cut's
continuation re-optimizes from the mistake-laden state, so every downstream
consequence it cannot recover — drifted cooldowns, stranded gauge, a use pushed
past fight end — lands inside the difference automatically. The segment losses
telescope (`Σ = C(0) − C(T_end)`), which is the conservation anchor the
re-attribution in `sidecar/advice.py` scales against.

Both sides of every difference use the SAME greedy continuation policy
(`engine.continue_rotation` with one fixed `SimParams`), so greedy-vs-perfect
slack cancels in the difference. In-sim potting is disabled (a full-fight
`forbidden_windows` entry on the tincture marker) so the currency is purely
rotational: the player's prefix carries no pot markers — `norm_casts` are real
ability ids — and letting the continuation pot would credit phantom pot value
asymmetrically. Pot value already has its own card.

Module-level entry points take only picklable arguments and resolve the job by
simulator module name, mirroring `sidecar/sim_pool.py::_sim_worker` — so a
batch of cuts fans out over the existing process pool. A job qualifies by
exposing the (already-universal) simulator-module conventions: `_model_for`,
`SimParams`, and `_make_score`/`_score`; anything short of that raises
`AdviceUnavailable`, which callers treat as "degrade to analytic-only", never a
request failure.
"""
from __future__ import annotations

import importlib
from typing import Any, Sequence

from jobs._core.sim import engine, replay
from jobs._core.tincture import TINCTURE_ACTION_ID


class AdviceUnavailable(RuntimeError):
    """The job's simulator module doesn't expose the conventions the cascade
    pass needs. Callers degrade to analytic-only advice."""


def _resolve(sim_module: str, sim_context, fight_duration_s: float):
    """(model, params, score_fn) for one cascade run, or AdviceUnavailable."""
    try:
        mod = importlib.import_module(sim_module)
    except Exception as e:                      # pragma: no cover - import guard
        raise AdviceUnavailable(f"cannot import {sim_module}: {e}") from e
    model_for = getattr(mod, "_model_for", None)
    params_cls = getattr(mod, "SimParams", None)
    if model_for is None or params_cls is None:
        raise AdviceUnavailable(
            f"{sim_module} lacks _model_for/SimParams (advice conventions)")
    try:
        model = model_for(sim_context)
    except TypeError:
        # A few older modules take (duration, sim_context).
        model = model_for(fight_duration_s, sim_context)
    make_score = getattr(mod, "_make_score", None)
    if make_score is not None:
        score = make_score(getattr(model, "mt_schedule", ()) or ())
    else:
        score = getattr(mod, "_score", None)
    if score is None:
        raise AdviceUnavailable(f"{sim_module} lacks _make_score/_score")
    # Pot-free cascade currency (see module docstring).
    params = params_cls(forbidden_windows=(
        (TINCTURE_ACTION_ID, 0.0, float(fight_duration_s)),))
    return model, params, score


def normalized_cuts(cuts: Sequence[float]) -> tuple[float, ...]:
    """The canonical cut tuple `cascade_scores` aligns its result with:
    deduped (to 2 decimals) and ascending."""
    return tuple(sorted({round(float(c), 2) for c in cuts}))


def cascade_scores(sim_module: str, fight_duration_s: float,
                   downtime: tuple[tuple[float, float], ...],
                   sim_context: Any,
                   casts: tuple[tuple[float, int], ...],
                   cuts: tuple[float, ...],
                   gcd_ids: tuple[int, ...] = (),
                   skip_ids: tuple[int, ...] = ()) -> tuple[float, ...]:
    """C(t) for every cut, aligned with `normalized_cuts(cuts)`. Deterministic;
    one incremental replay walk shared by all cuts."""
    model, params, score = _resolve(sim_module, sim_context, fight_duration_s)
    asc = normalized_cuts(cuts)
    pos = [c for c in asc if c > 0]
    states = replay.replay_prefix_states(
        model, casts, pos, fight_duration_s, list(downtime),
        gcd_ids=frozenset(gcd_ids), params=params,
        skip_ids=frozenset(skip_ids)) if pos else []
    by_cut: dict[float, float] = {}
    for cut, st in states:
        timeline, aux = engine.continue_rotation(
            model, st, fight_duration_s, list(downtime), params)
        by_cut[cut] = float(score(timeline, aux, None))
    if any(c <= 0 for c in asc):
        # C(0) is "perfect play from the very start": the model's own prepull
        # + greedy, from scratch. No replay — the anchor is exact (a clean
        # stream telescopes to a 0.0 total gap by construction), and any
        # player prepull deviation lands in the first segment where it
        # belongs, via C(t1)'s replayed prefix.
        timeline, aux = engine.run_rotation(
            model, fight_duration_s, list(downtime), params)
        zero_score = float(score(timeline, aux, None))
        for c in asc:
            if c <= 0:
                by_cut[c] = zero_score
    return tuple(by_cut[c] for c in asc)


def _snapshot(state) -> dict:
    """Picklable, comparable reading of a sim state: clock, per-ability charge
    pools, remaining cooldowns, and every public scalar gauge field the job's
    state subclass adds (heat, battery, soul, mana …). Engine plumbing and
    private incremental-score accumulators are excluded."""
    skip = {"t", "charges", "cd_ready", "last_gcd_t", "timeline",
            "fight_duration_s", "downtime_windows", "buff_intervals",
            "tincture_cd_ready", "tincture_used", "lock_done"}
    gauges: dict[str, float] = {}
    for name, val in vars(state).items():
        if name.startswith("_") or name in skip:
            continue
        if isinstance(val, bool):
            gauges[name] = float(val)
        elif isinstance(val, (int, float)):
            # Sentinel clocks (e.g. MCH `wf_cast_t = -1e9`) are engine plumbing,
            # not gauges — leaking one puts `-1000000000.0` in the evidence.
            if abs(float(val)) >= 1e8:
                continue
            gauges[name] = round(float(val), 3)
    return {
        "t": round(float(state.t), 2),
        "charges": {int(a): round(float(v), 3)
                    for a, v in sorted(state.charges.items())},
        "cd_remaining": {int(a): round(max(0.0, float(r) - float(state.t)), 2)
                         for a, r in sorted(state.cd_ready.items())},
        "gauges": dict(sorted(gauges.items())),
    }


def state_delta(sim_module: str, fight_duration_s: float,
                downtime: tuple[tuple[float, float], ...],
                sim_context: Any,
                casts: tuple[tuple[float, int], ...],
                t0: float, t1: float,
                gcd_ids: tuple[int, ...] = (),
                skip_ids: tuple[int, ...] = ()) -> dict:
    """Evidence for one segment [t0, t1]: the player's replayed state at t1 vs
    the state an ideal continuation *started at t0* reaches by t1. The ideal
    side keeps full-fight awareness (`state.fight_duration_s` stays the real
    duration) — only its loop is stopped at t1. Both sides are `_snapshot`
    dicts; the caller phrases the comparison ("arrived with Air Anchor 6.2s
    drifted and 35 heat stranded")."""
    model, params, _score = _resolve(sim_module, sim_context, fight_duration_s)
    gid, sid = frozenset(gcd_ids), frozenset(skip_ids)
    player = replay.replay_state(
        model, casts, t1, fight_duration_s, list(downtime),
        gcd_ids=gid, params=params, skip_ids=sid)
    ideal = replay.replay_state(
        model, casts, t0, fight_duration_s, list(downtime),
        gcd_ids=gid, params=params, skip_ids=sid)
    ideal_tl, _aux = engine.continue_rotation(
        model, ideal, min(float(t1), fight_duration_s), list(downtime), params)
    return {
        "player": _snapshot(player),
        "ideal": _snapshot(ideal),
        "idealSegmentTimeline": [(round(t, 2), a) for t, a in ideal_tl
                                 if float(t0) <= t <= float(t1)],
        # Stable time-only sort — same-timestamp cast ORDER is state-bearing
        # (replay._ordered_casts protects exactly this invariant), so the
        # evidence must not lexicographically reorder what the replay ran.
        "playerSegmentCasts": [(round(float(t), 2), int(a)) for t, a in
                               sorted(casts, key=lambda c: c[0])
                               if float(t0) <= t <= float(t1)],
    }


class Runner:
    """In-process façade the orchestrator uses: binds one pull's constants and
    dispatches the module-level entries through the sim pool when one is
    installed (`scoring.set_sim_pool`), in-process otherwise — the same degrade
    semantics as the perfect-sim cache. Deterministic either way."""

    _MODULE = "jobs._core.sim.counterfactual"

    def __init__(self, sim_module: str, fight_duration_s: float,
                 downtime: Sequence[tuple[float, float]],
                 sim_context: Any,
                 casts: Sequence[tuple[float, int]],
                 gcd_ids: Sequence[int] = (),
                 skip_ids: Sequence[int] = ()):
        self._args = (sim_module, float(fight_duration_s),
                      tuple((float(s), float(e)) for s, e in downtime or ()),
                      sim_context,
                      tuple((float(t), int(a)) for t, a in casts),
                      )
        self._gcd_ids = tuple(sorted(int(a) for a in gcd_ids))
        self._skip_ids = tuple(sorted(int(a) for a in skip_ids))

    @staticmethod
    def _pool():
        from jobs._core.sim import scoring
        return scoring._SIM_POOL

    def scores(self, cuts: Sequence[float]) -> dict[float, float]:
        """{cut: C(cut)} for the normalized cut set."""
        asc = normalized_cuts(cuts)
        if not asc:
            return {}
        args = self._args + (asc, self._gcd_ids, self._skip_ids)
        pool = self._pool()
        if pool is not None:
            vals = pool.run(self._MODULE, "cascade_scores", args)
        else:
            vals = cascade_scores(*args)
        return dict(zip(asc, vals))

    def deltas(self, segments: Sequence[tuple[float, float]]) -> list[dict]:
        """One `state_delta` per (t0, t1) segment, order-preserving; fanned out
        over the pool when installed."""
        segs = [(float(a), float(b)) for a, b in segments]
        if not segs:
            return []
        calls = [(self._MODULE, "state_delta",
                  self._args + (a, b, self._gcd_ids, self._skip_ids), None)
                 for a, b in segs]
        pool = self._pool()
        if pool is not None:
            return list(pool.run_many(calls))
        return [state_delta(*c[2]) for c in calls]
