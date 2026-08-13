"""The replay-prefix-seeded ceiling leg — the demonstrated-cadence-anchor
doctrine applied to rotation STRUCTURE.

A job's perfect path calls `seeded_ceiling_max_guard` after its base solver:
the player's demonstrated cast stream becomes a real search candidate, three
rungs, cheap → expensive:

  (a) the full replay of the demonstrated line,
  (b) greedy continuations from replayed prefixes at the given cut times
      (`replay.replay_prefix_states` ⊕ `engine.continue_rotation`),
  (c) a beam re-search seeded from the best cut's state
      (`engine.beam_search(roots=...)`) — run only when a cheaper rung already
      beats the base in the raw currency, so the expensive leg costs nothing on
      the (overwhelmingly common) pulls the base beam already dominates.

This is the structural version of the delivered-only witness guard
(`ScoringAspectBase.analyze`): an executed line is by definition a feasible
candidate for the optimum, so seeding the search with it closes a pure search
gap at the source instead of clamping the headline after the fact (the GNB
M12S-P1 "Fuseir Warblade" survivor: the player's 7/7 in-window Double Down
lattice replays legally and out-scores the width-256 beam; NEXT_STEPS.md holds
the full decomposition). The witness guard stays as the production backstop.

Two accepted approximations, both shared with the shipped cascade
continuations (`counterfactual.py`):

  * Replay performs no legality judgment — the demonstrated line is by
    definition executable, and per-id potency tables score a replayed prefix
    with the same currency as the delivered scorer, so the candidate is a
    valid lower bound on the true ceiling even where the model could not have
    generated it.
  * A replayed prefix crossing downtime skips `on_downtime_window`, so a cut
    landing between a long window's end and the player's first post-window
    press could resume a combo the game cleared — bounded to ~one combo'd GCD
    and self-correcting one cast later (the player's own restart presses re-set
    the combo state via `apply_cast`).

Currency discipline (the reason for `final_score`): the base solver's timeline
is re-potted downstream by `place_optimal_pots` (`scoring._finalize`), and the
DP uplift is line-dependent — a candidate that wins pot-stripped raw can lose
potted. Raw is therefore only the cheap GATE; adoption is decided in the
caller-supplied final (potted) currency, and any tie keeps the base, so the
ceiling can never regress.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from jobs._core.sim import engine, replay
from jobs._core.sim.engine import RotationModel, ScoreFn, SimStateBase
from jobs._core.tincture import TINCTURE_ACTION_ID

Candidate = tuple[list[tuple[float, int]], int]


def seeded_ceiling_max_guard(
        model: RotationModel,
        score_fn: ScoreFn,
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]],
        buff_intervals: list[tuple[float, float, float]] | None,
        base: Candidate,
        casts: Sequence[tuple[float, int]],
        *,
        cut_times: Sequence[float] = (),
        params_options: Sequence = (),
        gcd_ids: frozenset[int] = frozenset(),
        skip_ids: frozenset[int] = frozenset(),
        beam_width: Optional[int] = None,
        final_score: Optional[Callable[[list[tuple[float, int]], int], float]] = None,
) -> Candidate:
    """Max-guard `base` (the job's solver result) against candidates seeded from
    the demonstrated `casts` stream. Returns `base` unchanged — same objects —
    unless a candidate strictly beats it in the raw gate AND the `final_score`
    currency. `params_options[0]` drives replay slot bookkeeping and the greedy
    continuations; every option gets its own continuation/beam arm (mirroring
    `beam_perfect`'s param-set loop). `final_score=None` degrades the adoption
    decision to the raw currency (test convenience — production passes the
    potted scorer)."""
    if not casts or not params_options:
        return base

    def _raw(tl: list[tuple[float, int]], aux: int) -> float:
        return score_fn([(t, a) for t, a in tl if a != TINCTURE_ACTION_ID],
                        aux, buff_intervals)

    replay_params = params_options[0]
    try:
        candidates: list[tuple[float, Candidate, Optional[SimStateBase]]] = []

        # (a) The full replay of the demonstrated line.
        full = replay.replay_state(
            model, casts, fight_duration_s, fight_duration_s, downtime_windows,
            gcd_ids=gcd_ids, params=replay_params, skip_ids=skip_ids)
        full_cand = (list(full.timeline), model.final_aux(full))
        candidates.append((_raw(*full_cand), full_cand, None))

        # (b) Greedy continuations from replayed prefixes (one incremental walk).
        cuts = sorted({float(c) for c in cut_times if 0.0 < c < fight_duration_s})
        states = replay.replay_prefix_states(
            model, casts, cuts, fight_duration_s, downtime_windows,
            gcd_ids=gcd_ids, params=replay_params,
            skip_ids=skip_ids) if cuts else []
        for _cut, st in states:
            st.buff_intervals = buff_intervals or []
            for p in params_options:
                clone = engine._clone_state(st)
                tl, aux = engine.continue_rotation(
                    model, clone, fight_duration_s, downtime_windows, p)
                candidates.append((_raw(tl, aux), (tl, aux), st))

        # The raw gate: no strict win → the base stands, byte-identical.
        base_raw = _raw(*base)
        best_raw = max(r for r, _c, _s in candidates)
        if best_raw <= base_raw:
            return base

        # (c) Beam re-search seeded from the best cut's state — the expensive
        # rung, paid only once the gate is open. One root per run (beam_prune
        # ranks accumulated score; cross-time roots are prune-unfair).
        if beam_width:
            seed = max((c for c in candidates if c[2] is not None),
                       key=lambda c: c[0], default=None)
            if seed is not None:
                for p in params_options:
                    tl, aux = engine.beam_search(
                        model, score_fn, fight_duration_s, downtime_windows,
                        p, beam_width, buff_intervals,
                        roots=[engine._clone_state(seed[2])])
                    candidates.append((_raw(tl, aux), (tl, aux), None))

        # Adoption in the final (potted) currency; ties keep the base.
        entrants = [c for r, c, _s in candidates if r > base_raw]
        if final_score is None:
            return max(entrants, key=lambda c: _raw(*c))
        best, best_final = base, final_score(*base)
        for cand in entrants:
            f = final_score(*cand)
            if f > best_final:
                best, best_final = cand, f
        return best
    except Exception:
        # The leg is an upper-bound *improver*; any failure degrades to the
        # base solver's result (the witness guard still backstops production).
        # The Fuseir structural pin in test_gunbreaker_pulls.py keeps this
        # branch honest — a silently-broken leg fails that gate.
        return base
