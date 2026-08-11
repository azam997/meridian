"""Job-agnostic invariants for the shared rotation engine
(jobs/_core/sim/engine.py).

These exercise the engine through a tiny toy `RotationModel` — no real job — so
a regression here is unambiguously an engine bug, not a job-data change. The
per-job sims (MCH/RPR) are pinned separately by their own snapshot tests.

Covers: downtime skipping + the on_downtime_window hook, forbidden windows,
multi-charge regen (advance_time) + apply_cooldown, the weave budget + the
triple-weave clip, and parameter-sweep maximality.

Run from python/:  python tests/test_sim_engine.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim.engine import (
    BaseRotationModel,
    SimParamsBase,
    SimStateBase,
    advance_time,
    apply_cooldown,
    beam_search,
    in_top_window,
    is_forbidden,
    reachable_richer_window,
    run_rotation,
    sweep_best,
)
from jobs._core.sim.timing import InstantGCD

# --- Toy spell book ---------------------------------------------------------
FILLER = 10   # always-castable GCD, 100p
TOOL = 11     # high-value GCD, 300p, blockable
OGCD = 12     # always-ready weave, 50p
SETUP = 13    # 0p, primes PAYOFF (a delayed-reward fork)
PAYOFF = 14   # 500p, only castable when primed


_POTENCY = {FILLER: 100.0, TOOL: 300.0, OGCD: 50.0}


_POTENCY_EXTRA = {SETUP: 0.0, PAYOFF: 500.0}


def _score(timeline, aux, buff_intervals) -> float:
    return sum({**_POTENCY, **_POTENCY_EXTRA}.get(aid, 0.0) for _t, aid in timeline)


# --- Beam search (GCD-perfect generalization of run_rotation) ---------------

@dataclass
class _ForkState(SimStateBase):
    primed: bool = False


class ForkModel(BaseRotationModel):
    """A delayed-reward toy: SETUP scores 0 now but primes PAYOFF (500) next slot,
    beating FILLER+FILLER (200). The greedy picker is myopic (always FILLER unless
    already primed), so only beam search — given the SETUP fork — finds the
    SETUP->PAYOFF line. With `expose_fork=False` it has no fork, so beam search must
    reproduce greedy exactly."""

    cooldowns = {}
    timing = InstantGCD(base_s=2.5)
    agnostic_anchors = ()
    buff_anchors = ()
    canonical_anchors = ()

    def __init__(self, *, expose_fork: bool = False):
        self.expose_fork = expose_fork

    def init_state(self) -> _ForkState:
        return _ForkState()

    def pick_gcd(self, state, params) -> int:
        return PAYOFF if state.primed else FILLER

    def gcd_candidates(self, state, params) -> list[int]:
        if self.expose_fork and not state.primed:
            return [FILLER, SETUP]
        return [self.pick_gcd(state, params)]

    def pick_ogcd(self, state, params):
        return None

    def apply_cast(self, state, ability_id) -> None:
        state.timeline.append((round(state.t, 6), ability_id))
        if ability_id == SETUP:
            state.primed = True
        elif ability_id == PAYOFF:
            state.primed = False

    def sweep_params(self, extra_forbidden):
        yield SimParamsBase(forbidden_windows=extra_forbidden)


def test_beam_width1_default_equals_run_rotation() -> None:
    """With the default single greedy candidate, beam search reproduces
    `run_rotation` exactly at any width — it's a strict generalization."""
    model = ToyModel(prefer_tool=True)
    p = SimParamsBase()
    greedy, _ = run_rotation(model, 30.0, [(7.0, 12.0)], p)
    for w in (1, 4, 12):
        beam, _ = beam_search(model, _score, 30.0, [(7.0, 12.0)], p, w)
        assert beam == greedy, f"width {w}: {beam} != {greedy}"


def test_beam_finds_delayed_reward_greedy_misses() -> None:
    """Beam search beats the greedy line when a fork has a delayed payoff."""
    p = SimParamsBase()
    greedy, _ = run_rotation(ForkModel(expose_fork=True), 30.0, [], p)
    # Greedy is myopic -> all FILLER.
    assert all(aid == FILLER for _t, aid in greedy), greedy
    beam, _ = beam_search(ForkModel(expose_fork=True), _score, 30.0, [], p, width=4)
    assert _score(beam, 0, None) > _score(greedy, 0, None)
    assert PAYOFF in {aid for _t, aid in beam}, beam
    # Wider beam never scores lower.
    wide, _ = beam_search(ForkModel(expose_fork=True), _score, 30.0, [], p, width=16)
    assert _score(wide, 0, None) >= _score(beam, 0, None) - 1e-9


class ToyModel(BaseRotationModel):
    """FILLER every GCD, an always-ready oGCD weave. TOOL is preferred over
    FILLER unless forbidden — used by the forbidden-window test."""

    cooldowns = {OGCD: (0.0, 1)}
    timing = InstantGCD(base_s=2.5)
    agnostic_anchors = ()
    buff_anchors = ()
    canonical_anchors = ()

    def __init__(self, *, prefer_tool: bool = False, record=None):
        self.prefer_tool = prefer_tool
        self.downtime_calls = record if record is not None else []

    def init_state(self) -> SimStateBase:
        s = SimStateBase()
        s.cd_ready = {OGCD: 0.0}
        return s

    def pick_gcd(self, state, params) -> int:
        if self.prefer_tool and not is_forbidden(TOOL, state.t, params.forbidden_windows):
            return TOOL
        return FILLER

    def pick_ogcd(self, state, params):
        if state.cd_ready.get(OGCD, 0.0) <= state.t:
            return OGCD
        return None

    def apply_cast(self, state, ability_id) -> None:
        state.timeline.append((round(state.t, 6), ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

    def on_downtime_window(self, state, win_start, win_end) -> None:
        self.downtime_calls.append((win_start, win_end))

    def sweep_params(self, extra_forbidden):
        for mw in (2, 3):
            yield SimParamsBase(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _gcd_times(timeline, gcd_id=FILLER) -> list[float]:
    return [t for t, aid in timeline if aid == gcd_id]


# --- Downtime ---------------------------------------------------------------

def test_no_casts_during_downtime_and_hook_fires() -> None:
    calls: list[tuple[float, float]] = []
    model = ToyModel(record=calls)
    timeline, _ = run_rotation(model, 20.0, [(3.0, 8.0)], SimParamsBase())
    # No new GCD *starts* inside the window. (A weave tail of the slot that
    # started just before the window may extend a hair past the edge — the
    # engine only declines to start a fresh GCD slot in downtime, matching both
    # real sims.)
    assert not any(3.0 <= t < 8.0 for t, aid in timeline if aid == FILLER), timeline
    # Uptime resumes at the window edge.
    assert any(t >= 8.0 for t, _aid in timeline)
    # The downtime-edge hook saw exactly the window it skipped, once.
    assert calls == [(3.0, 8.0)], calls


# --- Forbidden windows ------------------------------------------------------

def test_forbidden_window_blocks_an_ability() -> None:
    model = ToyModel(prefer_tool=True)
    # TOOL forbidden for the whole fight -> the picker falls back to FILLER.
    params = SimParamsBase(forbidden_windows=((TOOL, 0.0, 1000.0),))
    timeline, _ = run_rotation(model, 30.0, [], params)
    assert TOOL not in {aid for _t, aid in timeline}
    assert FILLER in {aid for _t, aid in timeline}

    # Without the block, TOOL is picked.
    timeline2, _ = run_rotation(model, 30.0, [], SimParamsBase())
    assert TOOL in {aid for _t, aid in timeline2}


# --- advance_time charge regen + apply_cooldown -----------------------------

class _ChargeModel(BaseRotationModel):
    cooldowns = {TOOL: (20.0, 2), OGCD: (30.0, 1)}


def test_advance_time_regenerates_charges_and_caps() -> None:
    model = _ChargeModel()
    state = SimStateBase()
    state.charges = {TOOL: 0.0}
    advance_time(model, state, 10.0)
    assert abs(state.charges[TOOL] - 0.5) < 1e-9   # 10s / 20s recast
    advance_time(model, state, 90.0)               # +80s -> +4 charges, capped at 2
    assert state.charges[TOOL] == 2.0
    assert state.t == 90.0


def test_advance_time_backwards_is_noop_for_charges() -> None:
    model = _ChargeModel()
    state = SimStateBase()
    state.t = 5.0
    state.charges = {TOOL: 1.0}
    advance_time(model, state, 3.0)   # delta <= 0
    assert state.charges[TOOL] == 1.0
    assert state.t == 3.0


def test_apply_cooldown_spends_charge_and_sets_recast() -> None:
    cd = {TOOL: (20.0, 2), OGCD: (30.0, 1)}
    state = SimStateBase()
    state.t = 5.0
    state.charges = {TOOL: 2.0}
    apply_cooldown(state, cd, TOOL)
    assert state.charges[TOOL] == 1.0
    apply_cooldown(state, cd, OGCD)
    assert state.cd_ready[OGCD] == 35.0


# --- Weave budget + triple-weave clip ---------------------------------------

def test_weave_budget_caps_ogcds_per_gcd() -> None:
    model = ToyModel()
    timeline, _ = run_rotation(model, 6.0, [], SimParamsBase(max_weaves_per_gcd=2))
    # Count oGCDs between the first two GCDs — must respect the budget of 2.
    gcds = _gcd_times(timeline)
    first, second = gcds[0], gcds[1]
    weaved = [t for t, aid in timeline if aid == OGCD and first <= t < second]
    assert len(weaved) == 2, timeline


def test_triple_weave_clips_the_next_gcd() -> None:
    model = ToyModel()
    tl2, _ = run_rotation(model, 6.0, [], SimParamsBase(max_weaves_per_gcd=2))
    tl3, _ = run_rotation(model, 6.0, [], SimParamsBase(max_weaves_per_gcd=3))
    # Budget 2: no clip -> the 2nd GCD lands on the flat 2.5s GCD.
    assert abs(_gcd_times(tl2)[1] - 2.5) < 1e-6, _gcd_times(tl2)
    # Budget 3: the 3rd weave pushes the 2nd GCD out by triple_weave_clip_s.
    assert abs(_gcd_times(tl3)[1] - 3.0) < 1e-6, _gcd_times(tl3)
    weaved = [t for t, aid in tl3 if aid == OGCD and t < _gcd_times(tl3)[1]]
    assert len(weaved) == 3, tl3


# --- Sweep maximality -------------------------------------------------------

def test_sweep_best_returns_the_max_scoring_param() -> None:
    model = ToyModel()
    timeline, aux, params, score = sweep_best(model, _score, 30.0, [])
    # Recompute every swept option's score; sweep_best must return the best.
    manual = []
    for p in model.sweep_params(()):
        tl, ax = run_rotation(model, 30.0, [], p)
        manual.append(_score(tl, ax, None))
    assert abs(score - max(manual)) < 1e-9, (score, manual)
    # And the returned timeline is internally consistent with the score.
    assert abs(_score(timeline, aux, None) - score) < 1e-9


def test_buff_alignment_primitives() -> None:
    """`in_top_window` / `reachable_richer_window` — the shared spend-timing
    helpers (MCH Queen banking and any future snapshot job build on them)."""
    # Ramp: partial stack [5,10) x1.10, full stack [10,25) x1.25, then nothing.
    bi = [(5.0, 10.0, 1.10), (10.0, 25.0, 1.25)]
    assert in_top_window(12.0, bi) is True       # inside the full stack
    assert in_top_window(7.0, bi) is False       # only partial here
    assert in_top_window(30.0, bi) is False      # outside every segment
    assert in_top_window(0.0, None) is False     # no buffs

    # Targets the soonest window RICHER THAN NOW (not only the top stack).
    assert reachable_richer_window(0.0, bi, 12.0) == 5.0      # partial @5 beats x1.0
    assert reachable_richer_window(0.0, bi, 4.0) is None      # 5 is past the reach
    assert reachable_richer_window(7.0, bi, 12.0) == 10.0     # full @10 beats the partial
    assert reachable_richer_window(12.0, bi, 100.0) is None   # already in a top window
    assert reachable_richer_window(30.0, bi, 100.0) is None   # nothing richer ahead
    assert reachable_richer_window(0.0, None, 100.0) is None  # no buffs


def main() -> None:
    test_no_casts_during_downtime_and_hook_fires()
    test_forbidden_window_blocks_an_ability()
    test_advance_time_regenerates_charges_and_caps()
    test_advance_time_backwards_is_noop_for_charges()
    test_apply_cooldown_spends_charge_and_sets_recast()
    test_weave_budget_caps_ogcds_per_gcd()
    test_triple_weave_clips_the_next_gcd()
    test_sweep_best_returns_the_max_scoring_param()
    test_buff_alignment_primitives()
    print("test_sim_engine: all checks passed")


if __name__ == "__main__":
    main()
