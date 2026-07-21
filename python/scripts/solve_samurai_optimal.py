"""Exact SAM rotation solver — memoized DP + branch-and-bound DIAGNOSTIC.

The SAM idealized ceiling is built with **beam search** (`engine.beam_perfect` +
the model's `gcd_candidates`). Beam keeps the top-K partial rotations per GCD slot
and discards the rest — a bounded-memory heuristic that can prune a partial line
that is temporarily behind but would have won. On the one over-100.5% pull (Nana
Yuuki, M12S-P2, report DF7KMXGZxBjnq8db fight 10) the beam tops out ~0.55% under her
parse, and we cannot tell from beam alone whether that gap is *search* (beam pruned
the optimum) or *fidelity* (the model genuinely can't do better).

This script computes the **provable model optimum** for one pull and prints the
verdict. It is a diagnostic — NOT wired into the engine.

Method (see the approved plan / the [sam-exact-solver-diagnostic] memory):
  * Reuse the imperative model end-to-end: `engine._commit_gcd` is the transition
    (fires the GCD, greedily weaves oGCDs, advances time); `engine._clone_state`
    branches. oGCD weaves stay greedy *inside* the transition — exactly what the
    beam does — so the comparison isolates the GCD-level search question.
  * **Exact memoized DP over reachable discrete states.** For a no-downtime pull the
    state is already exactly discrete: every GCD lands on the 2.14s grid, so
    cooldowns / DoT-remaining / Kenki take finitely many values across *reachable*
    states. Memoizing on the discrete state gives the exact optimum — no bucketing.
  * **Branch-and-bound composed on top:** an incumbent (seeded from the beam) + an
    admissible upper bound used as a *lossless* prune (skip a subtree iff
    `g + UB <= incumbent`). Plus a lossless dominance memo (keep max g per state).
  * A **dense** legal-move generator (every legal GCD, a superset of the model's
    targeted `gcd_candidates`) so the search can find the better Sen-feeding build.

Scoring is buff-agnostic, no Fugetsu (same as the plan's reference numbers): beam
≈ 198,564; Nana's actual casts ≈ 199,864.

Run from python/ (FFLogs creds in ~/.fflogs_efficiency_analyzer/config.json):
    python scripts/solve_samurai_optimal.py                 # the Nana pull, defaults
    python scripts/solve_samurai_optimal.py --name Nana --enc 105
    python scripts/solve_samurai_optimal.py --segment 150   # directional, if full is slow
    python scripts/solve_samurai_optimal.py --time-box 120  # cap wall-clock, report best-so-far
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobs._core.sim import engine                                      # noqa: E402
from jobs._core.sim.engine import is_forbidden                         # noqa: E402
from jobs.samurai import data as sd                                    # noqa: E402
from jobs.samurai import simulator as sam                              # noqa: E402
from jobs.samurai.scoring import score_delivered_potency              # noqa: E402
from jobs.samurai.simulator import (                                   # noqa: E402
    HIGANBANA_REFRESH_AT_S,
    SAM_GCD_S,
    SimParams,
    _ALL_SEN,
    _SEN_ENDER,
    _SEN_SECOND,
    _missing_sen,
    _model_for,
    _sen_count,
)

sys.setrecursionlimit(200_000)


# --- Scoring constants (buff-agnostic; mirror score_delivered_potency) ------
CRIT = sd.GUARANTEED_CRIT_MULT
DOT_FULL = (sd.HIGANBANA_DOT_DURATION_S / sd.HIGANBANA_DOT_TICK_S
            * sd.HIGANBANA_DOT_TICK_P)   # one full 60s Higanbana DoT = 1000p
# Best single-GCD / single-oGCD table value (admissible per-slot ceiling pieces).
MAX_GCD_VALUE = max(
    sd.POTENCIES[a] * (CRIT if a in sd.ALWAYS_CRIT_IDS else 1.0)
    for a in sd.POTENCIES if a not in sd.OGCD_IDS)
MAX_OGCD_VALUE = max(
    (sd.POTENCIES[a] for a in sd.POTENCIES if a in sd.OGCD_IDS), default=0)


def _flat_value(aid: int) -> float:
    """A cast's table potency x guaranteed-crit (buff-agnostic) — the per-cast
    term of `score_delivered_potency` with Fugetsu/raid buffs off."""
    base = sd.POTENCIES.get(aid, 0)
    if base <= 0:
        return 0.0
    return base * (CRIT if aid in sd.ALWAYS_CRIT_IDS else 1.0)


def _dot_segment(gap_s: float) -> float:
    """Finalized Higanbana DoT for an application followed by the next `gap_s`
    later (capped at the 60s duration) — matches `_higanbana_dot_potency`."""
    covered = min(sd.HIGANBANA_DOT_DURATION_S, max(0.0, gap_s))
    return covered / sd.HIGANBANA_DOT_TICK_S * sd.HIGANBANA_DOT_TICK_P


def _fast_clone(st):
    """A lightweight `SimState` clone for the hot search loop — copies only the
    mutated containers (charges / cd_ready / timeline / tengentsu_procs); shares
    the read-only downtime list and value-copies the scalar gauge/flag fields. ~10x
    `copy.deepcopy` (engine._clone_state). Carries the search bookkeeping attrs
    (`_flat` etc.) via the shallow copy. Coupled to SimState's fields by design."""
    new = copy.copy(st)
    new.charges = dict(st.charges)
    new.cd_ready = dict(st.cd_ready)
    new.timeline = list(st.timeline)
    new.tengentsu_procs = list(st.tengentsu_procs)
    return new


# --- Dense legal-move generator (exactness; superset of gcd_candidates) ------

def legal_gcds(model, state, params) -> list[int]:
    """Every legal GCD at this slot — the dense move set the exact search needs
    (the shipped `gcd_candidates` is a *targeted* subset). Forced single move when
    mid-combo, a Kaeshi follow-up is pending, or 3 Sen must dump; otherwise the
    full fork: Ogi (if ready), Higanbana (lone Sen + refreshable DoT), and a Sen
    builder toward *each* missing Sen (a Meikyo instant ender, or a combo start
    whose 2nd step then forks on which Sen)."""
    model._release_tengentsu_kenki(state)
    cs = state.combo_step
    if cs == 1:                                    # which Sen this combo builds
        opts = [_SEN_SECOND.get(b, sd.YUKIKAZE) for b in _missing_sen(state.sen_mask)]
        return opts or [sd.GYOFU]
    if cs == 2:                                    # locked 3rd step
        return [_SEN_ENDER.get(state.combo_target, sd.GEKKO)]

    # combo_step == 0 — forced follow-ups first.
    if state.kaeshi_namikiri_ready:
        return [sd.KAESHI_NAMIKIRI]
    if state.tendo_kaeshi_ready:
        return [sd.TENDO_KAESHI_SETSUGEKKA]
    if state.kaeshi_setsugekka_ready:
        return [sd.KAESHI_SETSUGEKKA]

    moves: list[int] = []
    if state.ogi_ready and not is_forbidden(
            sd.OGI_NAMIKIRI, state.t, params.forbidden_windows):
        moves.append(sd.OGI_NAMIKIRI)
    if state.sen_mask == _ALL_SEN:
        moves.append(sd.TENDO_SETSUGEKKA if state.tendo else sd.MIDARE_SETSUGEKKA)
    else:
        missing = _missing_sen(state.sen_mask)
        if _sen_count(state.sen_mask) == 1 \
                and state.higanbana_dot_end - state.t <= HIGANBANA_REFRESH_AT_S:
            moves.append(sd.HIGANBANA)
        if state.meikyo_stacks > 0:                # instant ender for each missing Sen
            moves.extend(_SEN_ENDER[b] for b in missing)
        else:
            moves.append(sd.GYOFU)                 # start a combo (2nd-step forks Sen)

    seen: set[int] = set()
    out = [m for m in moves if not (m in seen or seen.add(m))]
    return out or [model.pick_gcd(state, params)]


# --- The bound-pruned memoized DP solver ------------------------------------

class Solver:
    """Exact DFS branch-and-bound + lossless dominance memo over the GCD tree.

    `g` (exact partial score) and the admissible `ub` are both buff-agnostic. The
    incumbent climbs to the provable optimum; `best_state` carries the optimal
    timeline. Set `use_dominance=False` to prove losslessness (same optimum)."""

    def __init__(self, model, params, duration_s, downtime, incumbent0,
                 max_weaves, *, time_box=None, use_dominance=True, tight=True):
        self.model = model
        self.params = params
        self.dur = duration_s
        self.downtime = downtime or []
        self.incumbent = incumbent0
        self.best_state = None
        self.best_leaf = float("-inf")     # best complete leaf seen (even if <= seed)
        self.best_leaf_state = None
        self.max_weaves = max_weaves
        self.time_box = time_box
        self.use_dominance = use_dominance
        self.tight = tight
        self.dominance: dict[tuple, float] = {}
        self.nodes = 0
        self.leaves = 0
        self.pruned_bound = 0
        self.pruned_dom = 0
        self.timed_out = False
        self._start = time.monotonic()

    # exact partial score (trailing Higanbana credited by elapsed, capped 60s)
    def g(self, st) -> float:
        base = st._flat + st._finalized_dot
        if st._last_higan is not None:
            base += _dot_segment(st.t - st._last_higan)
        return base

    # complete-rotation score: the trailing Higanbana is credited the full 60s
    # DoT, matching score_delivered_potency's span_end = max_cast + 60.
    def terminal(self, st) -> float:
        base = st._flat + st._finalized_dot
        if st._last_higan is not None:
            base += DOT_FULL
        return base

    # admissible upper bound on the remaining reward (never an underestimate).
    def ub(self, st) -> float:
        rem = self.dur - st.t
        if rem <= 0:
            return (DOT_FULL - _dot_segment(st.t - st._last_higan)) \
                if st._last_higan is not None else 0.0
        if not self.tight:
            n = math.ceil(rem / SAM_GCD_S)
            trailing = (DOT_FULL - _dot_segment(st.t - st._last_higan)) \
                if st._last_higan is not None else 0.0
            return n * MAX_GCD_VALUE + n * self.max_weaves * MAX_OGCD_VALUE + trailing
        return self._ub_tight(st, rem)

    def _ub_tight(self, st, rem: float) -> float:
        """Resource-gated admissible bound. Each high-value cast count is capped by
        its binding resource (Meikyo->Tendo, Ikishoten->Ogi, Sen-throughput->Midare);
        the n remaining GCD slots are filled highest-value-first, the rest at the best
        builder (420). oGCDs are Kenki/CD/meditation-gated. Higanbana initial hits
        ride in the GCD fill (200 < 420); ALL Higanbana DoT (incl. the trailing and
        the scorer's end-of-fight full-credit) is bounded by continuous uptime
        (rem/3*50) + one full DoT. Looser than reality, never below it."""
        n = int(math.ceil(rem / SAM_GCD_S)) + 1                # >= remaining GCD slots
        # Tendo Setsugekka pairs (Setsugekka + Tendo Kaeshi), gated by Meikyo presses.
        mk = st.charges.get(sd.MEIKYO_SHISUI, 0.0)
        mk_recast = sd.COOLDOWNS[sd.MEIKYO_SHISUI][0]
        n_tendo = (1 if st.tendo else 0) + int(math.floor(mk + rem / (mk_recast / 2.0))) + 1
        # Ogi pairs (Ogi + Kaeshi Namikiri), gated by Ikishoten.
        iki_in = max(0.0, st.cd_ready.get(sd.IKISHOTEN, 0.0) - st.t)
        n_iki = (1 if iki_in <= rem else 0) + int(rem / sd.COOLDOWNS[sd.IKISHOTEN][0]) + 1
        n_ogi = (1 if st.ogi_ready else 0) + n_iki
        # Midare pairs: remaining 3-Sen sets beyond the Tendo ones (Sen-throughput cap).
        max_sets = (_sen_count(st.sen_mask) + n) // 3
        n_midare = max(0, max_sets - n_tendo)
        # Fill the n GCD slots highest-value-first.
        hi = [1782.0] * (2 * n_tendo) + [1620.0] * (2 * n_ogi) + [1102.0] * (2 * n_midare)
        hi.sort(reverse=True)
        gcd_ub = sum(hi[:n]) if len(hi) >= n else sum(hi) + 420.0 * (n - len(hi))
        # oGCDs (Kenki / CD / meditation gated).
        senei_in = max(0.0, st.cd_ready.get(sd.HISSATSU_SENEI, 0.0) - st.t)
        n_senei = (1 if senei_in <= rem else 0) + int(rem / 60.0) + 1
        n_iaijutsu = n_tendo + n_midare + n_ogi
        n_shoha = (st.meditation + n_iaijutsu) // 3
        kenki_income = (st.kenki + n * 15 + n_iki * 50
                        + len(st.tengentsu_procs) * sd.TENGENTSU_KENKI_PER_PROC)
        n_shinten = max(0, int((kenki_income - n_senei * 25 - n_ogi * 50) // 25))
        ogcd_ub = n_senei * 800 + n_ogi * 940 + n_shoha * 640 + n_shinten * 250
        # All Higanbana DoT (future ticks + the end-of-fight full-credit artifact).
        dot_ub = (rem / sd.HIGANBANA_DOT_TICK_S) * sd.HIGANBANA_DOT_TICK_P + DOT_FULL
        return gcd_ub + ogcd_ub + dot_ub

    def key(self, st) -> tuple:
        iki = max(0.0, st.cd_ready.get(sd.IKISHOTEN, 0.0) - st.t)
        senei = max(0.0, st.cd_ready.get(sd.HISSATSU_SENEI, 0.0) - st.t)
        mk = st.charges.get(sd.MEIKYO_SHISUI, 0.0)
        return (
            round(st.t, 2), st.kenki, st.sen_mask, st.meditation,
            st.meikyo_stacks, st.combo_step, st.combo_target, st.tendo,
            st.ogi_ready, st.zanshin_ready, st.kaeshi_setsugekka_ready,
            st.tendo_kaeshi_ready, st.kaeshi_namikiri_ready,
            round(st.higanbana_dot_end - st.t, 2), round(mk, 4),
            round(iki, 2), round(senei, 2),
        )

    def _step(self, st, gcd_id):
        """Apply one GCD into a cloned child and carry the incremental score
        bookkeeping (`_flat`, `_finalized_dot`, `_last_higan`)."""
        child = _fast_clone(st)
        old_len = len(child.timeline)
        dur_slot = self.model.gcd_duration(child, gcd_id, self.params)
        engine._commit_gcd(self.model, child, self.params, gcd_id, dur_slot)
        flat, fin, last_h = st._flat, st._finalized_dot, st._last_higan
        for ct, aid in child.timeline[old_len:]:
            flat += _flat_value(aid)
            if aid == sd.HIGANBANA:
                if last_h is not None:
                    fin += _dot_segment(ct - last_h)
                last_h = ct
        child._flat, child._finalized_dot, child._last_higan = flat, fin, last_h
        return child

    def _init_root(self):
        root = self.model.init_state()
        root.fight_duration_s = self.dur
        root.downtime_windows = self.downtime
        self.model.prepull(root, self.params)
        root._flat = sum(_flat_value(a) for _t, a in root.timeline)
        root._finalized_dot = 0.0
        higans = [ct for ct, a in root.timeline if a == sd.HIGANBANA]
        root._last_higan = max(higans) if higans else None
        return root

    def solve(self):
        self._dfs(self._init_root())
        return self.incumbent, self.best_state

    def _dfs(self, st):
        if self.time_box and (time.monotonic() - self._start) > self.time_box:
            self.timed_out = True
            return
        self.model._release_tengentsu_kenki(st)
        # Downtime skip (no-op on a clean pull; kept for generality). Meditate /
        # pre-reappear Meikyo are 0-potency, so the score bookkeeping is unchanged.
        while engine.in_downtime(st.t, self.downtime):
            win = engine.containing_window(st.t, self.downtime)
            if win is not None:
                self.model.on_downtime_window(st, win[0], win[1])
            engine.advance_time(self.model, st, engine.next_uptime(st.t, self.downtime))

        if st.t >= self.dur:
            self.leaves += 1
            val = self.terminal(st)
            if val > self.best_leaf:
                self.best_leaf = val
                self.best_leaf_state = st
            if val > self.incumbent:
                self.incumbent = val
                self.best_state = st
            return

        self.nodes += 1
        gv = self.g(st)
        if gv + self.ub(st) <= self.incumbent + 1e-6:      # bound prune (lossless)
            self.pruned_bound += 1
            return
        if self.use_dominance:
            k = self.key(st)
            prev = self.dominance.get(k)
            if prev is not None and gv <= prev + 1e-6:      # dominance prune (lossless)
                self.pruned_dom += 1
                return
            self.dominance[k] = gv

        cands = legal_gcds(self.model, st, self.params)
        if len(cands) > 1:                                  # best-first: greedy line first
            greedy = self.model.pick_gcd(st, self.params)
            cands.sort(key=lambda m: (m != greedy, -_flat_value(m)))
        for m in cands:
            self._dfs(self._step(st, m))

    # --- Diverse beam (the prototype of the proposed shipped fix) ------------
    def _prune_score(self, s) -> float:
        """Beam ranking key = optimistic score (full trailing DoT) + an admissible
        banked-Sen credit — keeps a build-toward-Midare line that is momentarily
        behind alive (mirrors the shipped `beam_prune`)."""
        base = s._flat + s._finalized_dot + (DOT_FULL if s._last_higan is not None else 0.0)
        return base + _sen_count(s.sen_mask) * sam._SEN_PRUNE_VALUE

    def _sig(self, s, coarse: bool):
        """Beam dedup signature. `coarse` drops the fine fields (kenki / Meikyo
        charge / cooldown timers / sub-slot t) so near-duplicate lines collapse to
        one slot, forcing STRATEGIC diversity (the anti-collapse lever)."""
        if not coarse:
            return self.key(s)
        return (s.sen_mask, s.meikyo_stacks, s.tendo, s.ogi_ready, s.zanshin_ready,
                s.combo_step, s.combo_target, s.kaeshi_setsugekka_ready,
                s.tendo_kaeshi_ready, s.kaeshi_namikiri_ready, s.meditation,
                round(s.higanbana_dot_end - s.t, 0))

    def diverse_beam(self, width: int, coarse: bool = True, dense: bool = True):
        """Beam search over the dense `legal_gcds` with **signature dedup** — at each
        GCD layer keep the top-`width` successors by `_prune_score`, but at most one
        per `_sig` (so the K slots hold K *distinct* lines, not near-duplicates of the
        locally-best one — the fix for the diversity collapse). `dense=False` uses the
        model's shipped (targeted) `gcd_candidates` instead. Returns (score, state)."""
        beams = [self._init_root()]
        while True:
            successors, active = [], False
            for b in beams:
                self.model._release_tengentsu_kenki(b)
                while engine.in_downtime(b.t, self.downtime):
                    win = engine.containing_window(b.t, self.downtime)
                    if win is not None:
                        self.model.on_downtime_window(b, win[0], win[1])
                    engine.advance_time(self.model, b, engine.next_uptime(b.t, self.downtime))
                if b.t >= self.dur:
                    successors.append(b)
                    continue
                active = True
                cands = (legal_gcds(self.model, b, self.params) if dense
                         else self.model.gcd_candidates(b, self.params))
                for m in cands:
                    successors.append(self._step(b, m))
            if not active:
                break
            successors.sort(key=self._prune_score, reverse=True)
            seen, kept = set(), []
            for s in successors:
                sig = self._sig(s, coarse)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append(s)
                if len(kept) >= width:
                    break
            beams = kept
        best = max(beams, key=self.terminal)
        return self.terminal(best), best


def diverse_beam_best(duration_s, downtime, sim_context, *, width=64, coarse=True,
                      max_weaves=2):
    """Diverse-beam ceiling for one pull -> (score, timeline)."""
    model = _model_for(duration_s, sim_context)
    params = SimParams(max_weaves_per_gcd=max_weaves, forbidden_windows=())
    solver = Solver(model, params, duration_s, downtime, 0.0, max_weaves)
    score, state = solver.diverse_beam(width, coarse=coarse)
    return score, list(state.timeline)


def solve_optimal(duration_s, downtime, sim_context, *, max_weaves_set=(2,),
                  incumbent0=0.0, time_box=None, use_dominance=True, tight=True):
    """Exact optimum over the given `max_weaves` options (keep the best). Returns
    (score, timeline, stats)."""
    best = (incumbent0, None, None)
    for mw in max_weaves_set:
        model = _model_for(duration_s, sim_context)
        params = SimParams(max_weaves_per_gcd=mw, forbidden_windows=())
        solver = Solver(model, params, duration_s, downtime, best[0], mw,
                        time_box=time_box, use_dominance=use_dominance, tight=tight)
        score, state = solver.solve()
        if state is not None and score >= best[0]:
            best = (score, list(state.timeline), solver)
        elif best[2] is None:
            best = (best[0], None, solver)   # carry stats even if no improvement
    return best


# --- Replay: is a real parse a LEGAL path in our model? ---------------------

def replay_legality(sim_context, casts) -> list[tuple]:
    """Drive the model's state machine with a REAL parse's full cast stream,
    checking every Iaijutsu / Kaeshi / Ogi precondition against the state our model
    computes (apply_cast always realizes a cast, so Sen/flags accumulate as the
    parse actually played). **No violations ⇒ the parse's rotation is a legal path
    in our model ⇒ the exact optimum is >= the parse's score ⇒ the gap is SEARCH,
    not fidelity.** Each violation localizes the exact rule our model and her play
    disagree on (e.g. a Tendo with no model-tendo ⇒ a Meikyo/Tendo modeling gap)."""
    model = _model_for(600.0, sim_context)
    st = model.init_state()
    # Seed the pre-pull (countdown) Meikyo Shisui — FFLogs omits casts before the
    # pull timer, so the opener's Tendo/3-stacks aren't in the stream; without this
    # the first Tendo Setsugekka reads as a (spurious) tendo=False violation.
    model.prepull(st, SimParams())
    viol: list[tuple] = []
    for t, aid in sorted(casts, key=lambda c: c[0]):
        st.t = t
        model._release_tengentsu_kenki(st)
        if aid == sd.MIDARE_SETSUGEKKA and st.sen_mask != _ALL_SEN:
            viol.append((round(t, 2), "Midare", f"sen_mask={st.sen_mask} (<3 Sen)"))
        elif aid == sd.TENDO_SETSUGEKKA and not (st.sen_mask == _ALL_SEN and st.tendo):
            viol.append((round(t, 2), "Tendo", f"sen_mask={st.sen_mask} tendo={st.tendo}"))
        elif aid == sd.HIGANBANA and _sen_count(st.sen_mask) < 1:
            viol.append((round(t, 2), "Higanbana", "no Sen held"))
        elif aid == sd.KAESHI_SETSUGEKKA and not st.kaeshi_setsugekka_ready:
            viol.append((round(t, 2), "Kaeshi", "not armed"))
        elif aid == sd.TENDO_KAESHI_SETSUGEKKA and not st.tendo_kaeshi_ready:
            viol.append((round(t, 2), "TendoKaeshi", "not armed"))
        elif aid == sd.KAESHI_NAMIKIRI and not st.kaeshi_namikiri_ready:
            viol.append((round(t, 2), "KaeshiNamikiri", "not armed"))
        elif aid == sd.OGI_NAMIKIRI and not st.ogi_ready:
            viol.append((round(t, 2), "Ogi", "not armed"))
        model.apply_cast(st, aid)
    return viol


# --- Step 0: cheap reduced-objective feasibility (max Setsugekka count) ------

def max_setsugekka_count(duration_s, downtime, sim_context, min_higanbana=8,
                         max_weaves=2, time_box=30.0):
    """How many Setsugekka (Midare/Tendo + their Kaeshi count as the dump pairs)
    can the model fit while still casting >= `min_higanbana` Higanbana? A coarse
    feasibility check: if this is < Nana's 17, throughput can't reach her mix →
    lean fidelity (independent of the exact reward search)."""
    model = _model_for(duration_s, sim_context)
    params = SimParams(max_weaves_per_gcd=max_weaves, forbidden_windows=())
    best = {"setsu": -1, "higan": 0}
    seen: dict[tuple, int] = {}
    start = time.monotonic()

    def count(tl):
        c = Counter(a for _t, a in tl)
        setsu = c[sd.MIDARE_SETSUGEKKA] + c[sd.TENDO_SETSUGEKKA]
        return setsu, c[sd.HIGANBANA]

    def dfs(st):
        if time.monotonic() - start > time_box:
            return
        model._release_tengentsu_kenki(st)
        while engine.in_downtime(st.t, downtime or []):
            win = engine.containing_window(st.t, downtime or [])
            if win is not None:
                model.on_downtime_window(st, win[0], win[1])
            engine.advance_time(model, st, engine.next_uptime(st.t, downtime or []))
        if st.t >= duration_s:
            setsu, higan = count(st.timeline)
            if higan >= min_higanbana and setsu > best["setsu"]:
                best["setsu"], best["higan"] = setsu, higan
            return
        setsu, _ = count(st.timeline)
        # Admissible: each remaining slot can add at most ~1/3 Setsugekka.
        n = math.ceil((duration_s - st.t) / SAM_GCD_S)
        if setsu + math.ceil(n / 3) + 1 < best["setsu"]:
            return
        k = (round(st.t, 1), st.kenki, st.sen_mask, st.meikyo_stacks,
             st.combo_step, st.combo_target, st.tendo, st.ogi_ready,
             round(st.charges.get(sd.MEIKYO_SHISUI, 0.0), 3),
             round(st.higanbana_dot_end - st.t, 1))
        if seen.get(k, -1) >= setsu:
            return
        seen[k] = setsu
        for m in legal_gcds(model, st, params):
            child = engine._clone_state(st)
            engine._commit_gcd(model, child, params, m,
                               model.gcd_duration(child, m, params))
            dfs(child)

    root = model.init_state()
    root.fight_duration_s = duration_s
    root.downtime_windows = downtime or []
    model.prepull(root, params)
    dfs(root)
    return best["setsu"], best["higan"]


# --- Cast-mix reporting -----------------------------------------------------

def cast_mix(timeline) -> dict:
    c = Counter(a for t, a in timeline if t >= 0)
    gcd_ids = {a for a in sd.POTENCIES if a not in sd.OGCD_IDS} | {sd.GYOFU}
    return {
        "midare": c[sd.MIDARE_SETSUGEKKA], "tendo": c[sd.TENDO_SETSUGEKKA],
        "kaeshi": c[sd.KAESHI_SETSUGEKKA], "tendo_kaeshi": c[sd.TENDO_KAESHI_SETSUGEKKA],
        "higanbana": c[sd.HIGANBANA], "ogi": c[sd.OGI_NAMIKIRI],
        "kaeshi_nami": c[sd.KAESHI_NAMIKIRI],
        "gcds": sum(1 for t, a in timeline if t >= 0 and a in gcd_ids),
        "casts": sum(1 for t, a in timeline if t >= 0),
    }


def _fmt_mix(m: dict) -> str:
    setsu = m["midare"] + m["tendo"] + m["kaeshi"] + m["tendo_kaeshi"]
    return (f"Setsugekka {setsu} (Midare {m['midare']} Tendo {m['tendo']} "
            f"Kaeshi {m['kaeshi']}+{m['tendo_kaeshi']}) | Higanbana {m['higanbana']} "
            f"| Ogi {m['ogi']}+{m['kaeshi_nami']} | GCDs {m['gcds']} casts {m['casts']}")


# --- Pull resolution (network) ----------------------------------------------

def _load_pull(code, fight_id, name):
    """analyze_pull on a real pull -> (duration, downtime, sim_context, nana_casts).
    Mirrors validate_job_ceiling's resolution path."""
    from jobs import analyze_pull
    from scripts.validate_job_ceiling import _client, _resolve_src, _top_rankings

    client = _client()
    if code is None:
        ranks = _top_rankings(client, "Samurai", name_enc[1], 25)
        match = next((r for r in ranks
                      if (name or "").lower() in r.get("name", "").lower()), None)
        if match is None:
            raise SystemExit(f"no top Samurai pull matching {name!r} on enc {name_enc[1]}")
        code, fight_id, name = (match["report"]["code"], match["report"]["fightID"],
                                match["name"])
    src = _resolve_src(client, code, fight_id, name, "Samurai")
    if src is None:
        raise SystemExit(f"no Samurai actor in {code} fight {fight_id}")
    mr = analyze_pull("Samurai", client, code, fight_id, ranking_name=name, label=name)
    st = mr.aspects["Scoring"].state
    nana = [(t, a) for t, a in mr.norm_casts if t >= 0]
    full_casts = list(mr.norm_casts)        # incl. pre-pull (t<0) for replay
    return (st["fight_duration_s"], st["downtime_windows"], st["sim_context"],
            nana, code, fight_id, name, full_casts)


name_enc = (None, 105)   # filled by main() for the --name resolver


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", default="DF7KMXGZxBjnq8db", help="report code")
    ap.add_argument("--fight", type=int, default=10, help="fight id")
    ap.add_argument("--name", default="Nana", help="ranking-name substring")
    ap.add_argument("--enc", type=int, default=105, help="encounter id (for --name)")
    ap.add_argument("--resolve", action="store_true",
                    help="resolve code/fight from rankings via --name/--enc")
    ap.add_argument("--duration", type=float, help="override fight duration (s)")
    ap.add_argument("--segment", type=float,
                    help="solve only the first N seconds (directional, faster)")
    ap.add_argument("--max-weaves", type=int, nargs="*", default=[2],
                    help="weave budgets to try (default 2)")
    ap.add_argument("--time-box", type=float, help="cap solver wall-clock (s)")
    ap.add_argument("--no-dominance", action="store_true",
                    help="disable the dominance memo (losslessness cross-check)")
    ap.add_argument("--step0-only", action="store_true",
                    help="run only the cheap max-Setsugekka feasibility check")
    ap.add_argument("--exact", action="store_true",
                    help="also run the (time-boxed) exact DP/B&B solver as supporting "
                         "evidence — its best-so-far is a LOWER bound (552s won't "
                         "converge in Python); the replay is the proven verdict")
    ap.add_argument("--step0", action="store_true",
                    help="also run the cheap max-Setsugekka feasibility pre-check")
    args = ap.parse_args()

    global name_enc
    name_enc = (args.name, args.enc)
    code = None if args.resolve else args.code
    dur, downtime, sim_ctx, nana, code, fight_id, name, full_casts = _load_pull(
        code, args.fight, args.name)
    if args.duration:
        dur = args.duration
    seg = bool(args.segment)
    solve_dur = args.segment if seg else dur

    nana_score = score_delivered_potency(nana)
    beam_tl = sam.simulate_idealized_perfect(dur, downtime, sim_context=sim_ctx)[0]
    beam_score = score_delivered_potency(beam_tl)

    print(f"\n=== {name}  ({code} fight {fight_id}, enc {args.enc}) ===")
    print(f"duration {dur:.1f}s | downtime {len(downtime)} window(s) | "
          f"sim_context {sim_ctx}")
    print(f"beam ceiling (shipped)   {beam_score:11.1f}   {_fmt_mix(cast_mix(beam_tl))}")
    print(f"Nana actual casts        {nana_score:11.1f}   {_fmt_mix(cast_mix(nana))}")

    # === DECISIVE TEST: is her actual rotation a LEGAL path in our model? ===
    # Replay her real casts through the model's transitions (the same apply_cast the
    # sim uses); the standard opener Meikyo is seeded since FFLogs omits countdown
    # casts. No violations => her line is in the model's feasible set => the model
    # optimum >= her score => the gap is SEARCH, not fidelity.
    viol = replay_legality(sim_ctx, full_casts)
    print(f"\n[replay] {len(full_casts)} casts driven through the model -> "
          f"{len(viol)} precondition violation(s)")
    for vt, vlabel, vwhy in viol[:25]:
        print(f"    t={vt:7.2f}  {vlabel:<16} {vwhy}")

    # --- Optional supporting evidence (the replay above is the proven verdict) ---
    if args.step0 or args.step0_only:
        s0 = time.monotonic()
        max_setsu, hb = max_setsugekka_count(solve_dur, [] if seg else downtime, sim_ctx)
        print(f"\n[step0] max (Midare+Tendo) @>=8 Higanbana: {max_setsu} "
              f"(with {hb} Higanbana) in {time.monotonic()-s0:.1f}s")
        if args.step0_only:
            return 0

    if args.exact or seg:
        print(f"\n[exact] solving {'segment ' if seg else ''}{solve_dur:.1f}s "
              f"(seed beam {beam_score:.0f}; full 552s won't converge in Python) ...")
        t0 = time.monotonic()
        _score, _tl, solver = solve_optimal(
            solve_dur, [] if seg else downtime, sim_ctx,
            max_weaves_set=tuple(args.max_weaves),
            incumbent0=(beam_score if not seg else 0.0),
            time_box=args.time_box, use_dominance=not args.no_dominance)
        wall = time.monotonic() - t0
        status = "TIMEOUT(lower-bound)" if (solver and solver.timed_out) \
            else "CONVERGED-OPTIMAL"
        print(f"[exact] best {solver.best_leaf:.1f} [{status}] in {wall:.1f}s  "
              f"(nodes {solver.nodes} | pruned bound {solver.pruned_bound} "
              f"dom {solver.pruned_dom} | states {len(solver.dominance)})")
        if solver.best_leaf_state is not None:
            print(f"[exact] mix  {_fmt_mix(cast_mix(list(solver.best_leaf_state.timeline)))}")
        if seg:
            print("(segment — directional only; not comparable to the full-pull numbers)")
            return 0

    # === VERDICT (from the replay — the proven test) ===
    print("\n--- VERDICT " + "-" * 52)
    if not viol:
        print(f"SEARCH (proven): {name}'s actual rotation is a LEGAL path in our model, so "
              f"the model optimum >= her {nana_score:.0f} > beam {beam_score:.0f} "
              f"(+{100*(nana_score/beam_score-1):.2f}%). The beam (targeted candidates + "
              f"top-K diversity collapse) just fails to FIND it -- the over-100% is a "
              f"SEARCH gap, NOT fidelity. Fix the search (diverse-beam / dense candidates "
              f"/ a DP-B&B engine), which raises the ceiling back above her parse.")
    else:
        print(f"FIDELITY (localized): {len(viol)} of her casts are illegal under our model "
              f"(above) -- the state machine cannot reproduce her line, so no search reaches "
              f"her {nana_score:.0f}. Fix the model rule(s) the violations name "
              f"(Meikyo/Tendo economy, Sen, or Kaeshi timing).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
