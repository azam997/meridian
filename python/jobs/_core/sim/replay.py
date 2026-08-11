"""Replay a player's real cast stream through a job's `RotationModel`.

The deep-advice cascade pass needs the sim's reading of *the player's* position
at an arbitrary cut time t: gauge, cooldowns, charges, procs — everything
`apply_cast` accumulates. This module reconstructs that state by driving the
model's own transition function over the delivered `(t, ability_id)` stream
(`ModuleResult.norm_casts`), so the reconstruction and the ceiling share one
state machine by construction.

Two rules distinguish this from the ad-hoc replay in
`scripts/solve_samurai_optimal.py::replay_legality` (the proof-pattern
ancestor):

* Time moves via `engine.advance_time`, never bare `state.t = t` — that call is
  what regenerates fractional charges (MCH Drill/Reassemble/Double Check…), and
  a bare assignment silently freezes every charge pool at its opener value.
* GCD casts stamp `state.last_gcd_t` and remember the slot end
  (`model.gcd_duration` at the pre-apply state, mirroring `_commit_gcd`'s
  order), so a continuation resumed at the cut cannot fire a GCD inside the
  player's still-rolling slot.

`apply_cast` "always realizes a cast" (every job's contract): unknown or
utility ids are structural no-ops, and model-illegal player sequences apply
anyway — the replayed state is the sim's best reading of the stream, not a
legality judgment.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from jobs._core.sim import engine
from jobs._core.tincture import TINCTURE_ACTION_ID


def _ordered_casts(casts: Iterable[tuple[float, int]],
                   skip_ids: frozenset[int]) -> list[tuple[float, int]]:
    """Time-sorted casts with sim-internal markers and job skip-ids removed.
    The sort is STABLE on time alone — same-timestamp casts keep their stream
    order, because `apply_cast` order is state-bearing (a weave applied before
    vs after its GCD flips buff consumption, payload counting, gauge order).
    Pre-pull (t < 0) casts stay — the player's own opener channel is the
    pre-pull, so `model.prepull` must NOT also run on a replayed state."""
    return sorted(((float(t), int(a)) for t, a in casts
                   if a != TINCTURE_ACTION_ID and a not in skip_ids),
                  key=lambda c: c[0])


def _seed_state(model, first_t: float, fight_duration_s: float,
                downtime_windows: Sequence[tuple[float, float]]):
    state = model.init_state()
    state.fight_duration_s = fight_duration_s
    state.downtime_windows = list(downtime_windows or [])
    if first_t < state.t:
        # Pre-pull rewind: nothing has been spent yet, so a bare assignment is
        # exact (advance_time only regenerates on forward moves anyway).
        state.t = first_t
    return state


def _apply_one(model, state, t: float, aid: int, gcd_ids: frozenset[int],
               params, slot_end: float) -> float:
    """Advance to `t`, realize one cast, return the updated GCD slot end."""
    engine.advance_time(model, state, max(state.t, t))
    if aid in gcd_ids:
        state.last_gcd_t = state.t
        try:
            dur = model.gcd_duration(state, aid, params)
        except Exception:
            dur = 0.0          # id outside the model's timing table — no slot
        slot_end = max(slot_end, state.t + dur)
    model.apply_cast(state, aid)
    return slot_end


def replay_state(model, casts: Iterable[tuple[float, int]], until_t: float,
                 fight_duration_s: float,
                 downtime_windows: Sequence[tuple[float, float]],
                 gcd_ids: frozenset[int] = frozenset(),
                 params=None,
                 skip_ids: frozenset[int] = frozenset()):
    """The sim state after playing the stream's casts with `t <= until_t`.

    The returned state's clock sits at `max(until_t, last GCD slot end)` — the
    first instant a resumed rotation may legally act — and its `timeline`
    contains exactly the replayed prefix, so
    `engine.continue_rotation(model, state, ...)` returns the full
    prefix ⊕ continuation timeline in one list.

    `gcd_ids`: the ability ids to treat as GCDs for slot bookkeeping (callers
    derive it once via `ability_metadata`; kept an explicit argument so this
    module stays pure and pool-friendly). `params` is only consulted for
    `gcd_duration`; None skips slot-end tracking entirely.
    """
    ordered = [c for c in _ordered_casts(casts, skip_ids) if c[0] <= until_t]
    first_t = ordered[0][0] if ordered else 0.0
    state = _seed_state(model, first_t, fight_duration_s, downtime_windows)
    slot_end = state.t
    for t, aid in ordered:
        slot_end = _apply_one(model, state, t, aid,
                              gcd_ids if params is not None else frozenset(),
                              params, slot_end)
    engine.advance_time(model, state, max(until_t, slot_end, state.t))
    return state


def replay_prefix_states(model, casts: Iterable[tuple[float, int]],
                         cuts: Sequence[float],
                         fight_duration_s: float,
                         downtime_windows: Sequence[tuple[float, float]],
                         gcd_ids: frozenset[int] = frozenset(),
                         params=None,
                         skip_ids: frozenset[int] = frozenset()):
    """States at every cut from ONE incremental walk: `[(cut_t, state), ...]`
    in ascending cut order, each state an independent clone positioned exactly
    as `replay_state(..., until_t=cut_t)` would leave it. O(casts + cuts·clone)
    instead of O(cuts·casts)."""
    ordered = _ordered_casts(casts, skip_ids)
    asc = sorted(float(c) for c in cuts)
    first_t = ordered[0][0] if ordered else 0.0
    state = _seed_state(model, first_t, fight_duration_s, downtime_windows)
    slot_end = state.t
    out: list[tuple[float, object]] = []
    i = 0
    for cut in asc:
        while i < len(ordered) and ordered[i][0] <= cut:
            t, aid = ordered[i]
            slot_end = _apply_one(model, state, t, aid,
                                  gcd_ids if params is not None else frozenset(),
                                  params, slot_end)
            i += 1
        snap = model.clone(state)
        engine.advance_time(model, snap, max(cut, slot_end, snap.t))
        out.append((cut, snap))
    return out
