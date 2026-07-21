"""Idealized BRD rotation — the Bard `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the BRD-specific rotation: the WM→MB→AP song cycle,
the two DoTs with Iron Jaws refreshes, the budgeted Repertoire economy
(Refulgent / Pitch Perfect / Apex+Blast / Heartbreak — see data.py), the Coda →
Radiant Finale → Radiant Encore chain, Barrage's armed ×3 Refulgent, and the
Army's Paeon haste window. The four `simulate_*` shims at the bottom bind the
model to the engine (names kept so the sidecar / scorer / tests call them
unchanged).

BRD-specific rotation encoded:
- **All-instant GCDs** (`InstantGCD`, physical ranged) at the 2.5s global, with
  the Army's Paeon repertoire haste (4%/stack, ramping to 16%) and the 10s
  Army's Muse tail (12%) overlaid as a *time-window* `gcd_duration` override —
  the BLM Ley Lines analog, keyed on the sim's own song schedule.
- **The song cycle** as absolute-schedule oGCDs (WM 43.5s → MB 40s → AP 36.5s,
  live-measured): each song is a 120s-recast, 0-potency cast that drives Coda,
  the haste window, and Pitch Perfect legality. `on_downtime_window` keeps the
  cycle rolling through boss-untargetable gaps (songs are targetless).
- **Budgeted RNG resources** (the DNC pattern): the model spends exactly the
  player's measured Refulgent/PP/Apex/Blast/Heartbreak counts (`sim_context`),
  paced linearly with buff-aware banking (Apex/Heartbreak) or ASAP-in-window
  (Refulgent); Pitch Perfect spends eagerly under Wanderer's Minuet (its only
  legal window, so eagerness IS the pacing — and it can never strand budget).
- **Radiant Finale gating**: greedy ASAP would fire RF at 110s cadence and
  desync it from the 3-Coda cycle (an 800p Encore instead of 1100p), so RF
  waits for 3 Coda after its first use — which self-aligns it to the 2-minute
  window exactly as live top parses play it — with a fight-end escape so the
  last usable Finale (+Encore) is never silently forfeited.
- **Beam fork** (`gcd_candidates`): the early-Iron-Jaws re-snapshot (refresh
  both DoTs inside a buff window vs. ride the ticks) — the one real GCD-level
  choice in the kit. The buff-agnostic beam discards it (an early refresh only
  clips ticks); the buff-aware beam picks up the snapshot value the scorer sees.

Out of scope (documented, intentionally not modeled):
- Pitch Perfect stack tiers and Apex gauge scaling: budgets count SPENDS, both
  sides score them at the full-tier potency (360 / 700), so tier-mix cancels in
  the efficiency ratio (see scoring.py for the fairness argument).
- Multi-dotting a second target (AoE windows use the Ladonsbite/Shadowbite/Rain
  of Death swaps + free-splash; DoTs stay on the primary).
"""
from __future__ import annotations

from dataclasses import dataclass

from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.bard import data as bd


# --- Ability IDs (aliased from data for readability) ------------------------
BURST_SHOT       = bd.BURST_SHOT
REFULGENT_ARROW  = bd.REFULGENT_ARROW
CAUSTIC_BITE     = bd.CAUSTIC_BITE
STORMBITE        = bd.STORMBITE
IRON_JAWS        = bd.IRON_JAWS
APEX_ARROW       = bd.APEX_ARROW
BLAST_ARROW      = bd.BLAST_ARROW
RESONANT_ARROW   = bd.RESONANT_ARROW
RADIANT_ENCORE   = bd.RADIANT_ENCORE
HEARTBREAK_SHOT  = bd.HEARTBREAK_SHOT
RAIN_OF_DEATH    = bd.RAIN_OF_DEATH
EMPYREAL_ARROW   = bd.EMPYREAL_ARROW
SIDEWINDER       = bd.SIDEWINDER
PITCH_PERFECT    = bd.PITCH_PERFECT
BARRAGE          = bd.BARRAGE
RAGING_STRIKES   = bd.RAGING_STRIKES
BATTLE_VOICE     = bd.BATTLE_VOICE
RADIANT_FINALE   = bd.RADIANT_FINALE
WANDERERS_MINUET = bd.WANDERERS_MINUET
MAGES_BALLAD     = bd.MAGES_BALLAD
ARMYS_PAEON      = bd.ARMYS_PAEON
LADONSBITE       = bd.LADONSBITE
SHADOWBITE       = bd.SHADOWBITE


# --- Rotation tuning ---------------------------------------------------------
BRD_GCD_S = 2.50             # BRD physical-ranged global (outside Army's Paeon)
# Refresh both DoTs via Iron Jaws when the tighter one has ≤ ~1 GCD left.
IJ_REFRESH_AT_S = 2.6
# Beam fork: offer an early Iron Jaws once the tighter DoT is inside this window
# (the re-snapshot decision the buff-aware search prices).
IJ_FORK_AT_S = 30.0
# Buff-aware banking reach for the Apex/Heartbreak spends (the MCH
# `_queen_should_bank` lead budget, via engine.reachable_richer_window).
_BANK_LEAD_S = 20.0
# Fire the held Radiant Finale regardless of Coda when this little fight remains
# (every hold rule needs a fight-end escape — see the add-job skill).
_RF_END_ESCAPE_S = 30.0
# Fallback budget rates when no sim_context is supplied (≈ live top-parse rates:
# 63 Refulgent / 26 PP / 10 Apex / 10 Blast / 77 Heartbreak per ~630s).
_DEFAULT_REFULGENT_RATE_S = 10.0
_DEFAULT_PP_RATE_S = 24.0
_DEFAULT_APEX_RATE_S = 63.0
_DEFAULT_HB_RATE_S = 8.2

# Beam width for the GCD-perfect search. BRD's fork axis is narrow (the early
# Iron Jaws re-snapshot), so a modest diverse beam converges; raise only if the
# convergence test shows the ceiling still rising with width.
_BEAM_WIDTH = 24


@dataclass(frozen=True)
class BardCtx:
    """Per-pull context threaded as `sim_context` (hashable → joins the
    perfect-sim cache key). The five budgets are the player's MEASURED counts,
    so the ceiling spends the same number of RNG-fed resources (Repertoire /
    Hawk's Eye / Soul Voice luck never costs efficiency)."""
    refulgent_budget: int = 0     # Refulgent Arrow + Shadowbite (Hawk's Eye spends)
    pp_budget: int = 0            # Pitch Perfect casts
    apex_budget: int = 0          # Apex Arrow casts
    blast_budget: int = 0         # Blast Arrow casts
    hb_budget: int = 0            # Heartbreak Shot + Rain of Death casts

    def __bool__(self) -> bool:
        return bool(self.refulgent_budget or self.pp_budget or self.apex_budget
                    or self.blast_budget or self.hb_budget)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """BRD picker tunables — no axis beyond the shared knobs (max_weaves /
    forbidden_windows); the budgets are per-pull model parameters."""
    pass


@dataclass
class SimState(SimStateBase):
    # Song cycle
    song: int = 0                 # active song ability id (0 = none)
    song_start: float = 0.0
    song_idx: int = 0             # index into SONG_ORDER (next song to play)
    song_due_t: float = 0.0       # when the next song should be cast
    muse_end: float = -1.0        # Army's Muse haste tail end
    coda: int = 0                 # songs banked since the last Radiant Finale (cap 3)
    rf_used: bool = False         # the opener Finale fires at 1 Coda; later ones wait for 3
    # DoTs
    stormbite_end: float = -1.0
    caustic_end: float = -1.0
    # Armed follow-ups
    barrage_armed: bool = False          # next Refulgent lands 3 hits
    resonant_ready_until: float = -1.0   # Resonant Arrow Ready expiry
    encore_ready_until: float = -1.0     # Radiant Encore Ready expiry
    blast_ready: bool = False            # from a full-gauge Apex
    # Budgets remaining (matched to the player's measured counts)
    refulgent_remaining: int = 0
    pp_remaining: int = 0
    apex_remaining: int = 0
    blast_remaining: int = 0
    hb_remaining: int = 0


# --- Refinement / canonical anchors ------------------------------------------
# Hold the burst enablers into raid windows; the payoff GCDs (Encore, Resonant,
# the armed Refulgent) follow them. The engine adds the tincture marker itself.
_PERFECT_ANCHORS: tuple[int, ...] = (RAGING_STRIKES, BARRAGE, RADIANT_FINALE)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (RAGING_STRIKES, BARRAGE, RADIANT_FINALE)

_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    bd.JOB_DATA.tincture_main_stat, bd.JOB_DATA.tincture_role_coeff)


class BardRotationModel(engine.BaseRotationModel):
    cooldowns = bd.COOLDOWNS
    timing = InstantGCD(base_s=BRD_GCD_S)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, ctx: BardCtx | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()) -> None:
        self.ctx = ctx or BardCtx()
        self.gcd_base_s = BRD_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != BRD_GCD_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)
        # Multi-target N(t) schedule: Burst/Refulgent/Heartbreak swap to their AoE
        # forms where they win; the ST burst cleaves via SPLASH_POTENCIES. Empty ()
        # → single target, byte-identical.
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    def init_state(self) -> SimState:
        state = SimState()
        state.cd_ready = {aid: 0.0 for aid, (_r, ch) in bd.COOLDOWNS.items()
                          if ch == 1}
        # Heartbreak charges are budget-paced (the Mage's Ballad CDR makes the
        # 15s recast non-binding), so the charge pool is intentionally unseeded.
        state.refulgent_remaining = self.ctx.refulgent_budget
        state.pp_remaining = self.ctx.pp_budget
        state.apex_remaining = self.ctx.apex_budget
        state.blast_remaining = self.ctx.blast_budget
        state.hb_remaining = self.ctx.hb_budget
        return state

    def prepull(self, state: SimState, params) -> None:
        # Ranged: acts at t=0 from range (PHYSICAL_RANGED engage 0). The first
        # song is woven after the first GCD (song_due_t starts at 0), matching
        # the live opener (Stormbite → Wanderer's Minuet at ~1.3s).
        return None

    # --- GCD timing: the Army's Paeon haste window + Army's Muse tail --------
    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        base = self.gcd_base_s
        if state.song == ARMYS_PAEON:
            stacks = min(bd.AP_MAX_STACKS,
                         int((state.t - state.song_start) / bd.AP_STACK_INTERVAL_S))
            return base * (1.0 - bd.AP_HASTE_PER_STACK * stacks)
        if state.t < state.muse_end:
            return base * bd.MUSE_MULT
        return base

    # --- Budget pacing (the DNC `_spend_now` pattern) -------------------------
    def _due(self, total: int, state: SimState) -> float:
        d = state.fight_duration_s
        return total * state.t / d if d > 0 else 0.0

    def _spend_now(self, state: SimState, used: int, total: int,
                   *, use_bank: bool) -> bool:
        """Linear pacing (spend only when behind schedule, so the opener can't
        front-load luck), with buff-aware overrides: always spend inside a top
        raid window; bank toward a reachable richer window when `use_bank`."""
        if total <= 0:
            return False
        if used < self._due(total, state):
            return True
        bi = state.buff_intervals
        if not bi:
            return False
        if engine.in_top_window(state.t, bi):
            return True
        if use_bank and engine.reachable_richer_window(
                state.t, bi, _BANK_LEAD_S) is not None:
            return False
        return True

    # --- GCD picker ------------------------------------------------------------
    def pick_gcd(self, state: SimState, params) -> int:
        return self._maybe_aoe(state, self._pick_gcd_st(state, params))

    def _maybe_aoe(self, state: SimState, gcd: int) -> int:
        """Swap the AoE counterpart where it out-potencies at the live target
        count. The armed (Barraged) Refulgent stays single-target — 3×280 beats
        Shadowbite's 270-to-all below 4 targets. N<2 → unchanged."""
        n = self._n(state.t)
        if n < 2:
            return gcd
        if gcd == BURST_SHOT and potency_for(LADONSBITE, n, bd.JOB_DATA) > \
                potency_for(BURST_SHOT, n, bd.JOB_DATA):
            return LADONSBITE
        if gcd == REFULGENT_ARROW and not state.barrage_armed and \
                potency_for(SHADOWBITE, n, bd.JOB_DATA) > \
                potency_for(REFULGENT_ARROW, n, bd.JOB_DATA):
            return SHADOWBITE
        return gcd

    def _pick_gcd_st(self, state: SimState, params) -> int:
        t = state.t

        # 1. (Re)apply missing/expired DoTs (the opener, or after long downtime).
        if state.stormbite_end <= t:
            return STORMBITE
        if state.caustic_end <= t:
            return CAUSTIC_BITE

        # 2. Iron Jaws when the tighter DoT is about to fall (refresh as late as
        #    possible; both DoTs are alive here by construction).
        if min(state.stormbite_end, state.caustic_end) - t <= IJ_REFRESH_AT_S:
            return IRON_JAWS

        # 3. Armed follow-ups, tightest expiry first (Blast Arrow Ready ~10s).
        if state.blast_ready and state.blast_remaining > 0:
            return BLAST_ARROW
        if state.encore_ready_until > t:
            return RADIANT_ENCORE
        if state.barrage_armed:
            return REFULGENT_ARROW
        if state.resonant_ready_until > t:
            return RESONANT_ARROW

        # 4. Apex Arrow — budgeted, buff-aware banked (Soul Voice holds free).
        apex_used = self.ctx.apex_budget - state.apex_remaining
        if state.apex_remaining > 0 and self._spend_now(
                state, apex_used, self.ctx.apex_budget, use_bank=True):
            return APEX_ARROW

        # 5. Refulgent Arrow — budgeted Hawk's Eye spends (procs can't bank long).
        ref_used = self.ctx.refulgent_budget - state.refulgent_remaining
        if state.refulgent_remaining > 0 and self._spend_now(
                state, ref_used, self.ctx.refulgent_budget, use_bank=False):
            return REFULGENT_ARROW

        # 6. Filler.
        return BURST_SHOT

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The greedy pick, plus the one real GCD fork: an EARLY Iron Jaws once
        the tighter DoT is inside the fork window (re-snapshot both DoTs under
        the current buffs vs. ride the remaining ticks). The buff-agnostic beam
        discards it — an early refresh only clips ticks — so the strict ceiling
        stays byte-identical to the greedy line; the buff-aware beam prices the
        snapshot against the clip via the scorer."""
        greedy = self.pick_gcd(state, params)
        moves = [greedy]
        t = state.t
        if (greedy != IRON_JAWS
                and state.stormbite_end > t and state.caustic_end > t
                and min(state.stormbite_end, state.caustic_end) - t <= IJ_FORK_AT_S):
            moves.append(IRON_JAWS)
        return moves

    def beam_signature(self, state: SimState):
        """Decision-relevant diversity key, timers bucketed to ~one GCD (the
        add-job guidance: a lossless key fragments the beam into sub-GCD
        near-duplicates). `t` stays (bucketed) because the Army's Paeon haste
        makes slot lengths window-dependent — forks are not strictly lockstep."""
        g = self.gcd_base_s
        t = state.t
        return (
            int(t / g), state.song_idx, state.coda, state.rf_used,
            int(max(0.0, state.stormbite_end - t) / g),
            int(max(0.0, state.caustic_end - t) / g),
            state.barrage_armed, state.blast_ready,
            state.resonant_ready_until > t, state.encore_ready_until > t,
            state.refulgent_remaining, state.pp_remaining,
            state.apex_remaining, state.blast_remaining, state.hb_remaining,
            int(max(0.0, state.cd_ready.get(RAGING_STRIKES, 0.0) - t) / 10.0),
            int(max(0.0, state.cd_ready.get(RADIANT_FINALE, 0.0) - t) / 10.0),
            int(max(0.0, state.cd_ready.get(BARRAGE, 0.0) - t) / 10.0),
            int(max(0.0, state.cd_ready.get(SIDEWINDER, 0.0) - t) / 10.0),
        )

    # --- oGCD picker -------------------------------------------------------------
    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # 1. The song cycle — the cadence anchor everything else hangs off.
        song = bd.SONG_ORDER[state.song_idx % len(bd.SONG_ORDER)]
        if (t >= state.song_due_t - 1e-9 and state.cd_ready.get(song, 0.0) <= t
                and not is_forbidden(song, t, fw)):
            return song

        # 2. Raging Strikes — the job-owned +15% window. Ranked ABOVE the burst
        #    oGCDs so the big casts land inside it (the DRG Lance Charge lesson);
        #    at a 120s recast, ASAP == at-burst.
        if (state.cd_ready.get(RAGING_STRIKES, 0.0) <= t
                and not is_forbidden(RAGING_STRIKES, t, fw)):
            return RAGING_STRIKES

        # 3. Battle Voice — party buff, cast for timeline realism (0 potency;
        #    raid_buffs.py owns its multiplier).
        if (state.cd_ready.get(BATTLE_VOICE, 0.0) <= t
                and not is_forbidden(BATTLE_VOICE, t, fw)):
            return BATTLE_VOICE

        # 4. Radiant Finale — party buff + Radiant Encore enabler. Waits for 3
        #    Coda after its first use so the 110s recast never desyncs the 1100p
        #    Encore off the 2-min cycle; fight-end escape fires the last one.
        if (state.cd_ready.get(RADIANT_FINALE, 0.0) <= t and state.coda >= 1
                and not is_forbidden(RADIANT_FINALE, t, fw)):
            if (state.coda >= 3 or not state.rf_used
                    or state.fight_duration_s - t < _RF_END_ESCAPE_S):
                return RADIANT_FINALE

        # 5. Barrage — arms the ×3 Refulgent + Resonant Arrow Ready.
        if (state.cd_ready.get(BARRAGE, 0.0) <= t
                and not is_forbidden(BARRAGE, t, fw)):
            return BARRAGE

        # 6-7. Recast-gated damage oGCDs.
        if (state.cd_ready.get(EMPYREAL_ARROW, 0.0) <= t
                and not is_forbidden(EMPYREAL_ARROW, t, fw)):
            return EMPYREAL_ARROW
        if (state.cd_ready.get(SIDEWINDER, 0.0) <= t
                and not is_forbidden(SIDEWINDER, t, fw)):
            return SIDEWINDER

        # 8. Pitch Perfect — eager under Wanderer's Minuet (its only legal song,
        #    so the song windows ARE the pacing; eagerness can't strand budget).
        if state.song == WANDERERS_MINUET and state.pp_remaining > 0:
            return PITCH_PERFECT

        # 9. Heartbreak Shot — budgeted, buff-aware banked (real play banks the
        #    3 charges into the burst).
        hb_used = self.ctx.hb_budget - state.hb_remaining
        if state.hb_remaining > 0 and self._spend_now(
                state, hb_used, self.ctx.hb_budget, use_bank=True):
            n = self._n(t)
            if n >= 2 and potency_for(RAIN_OF_DEATH, n, bd.JOB_DATA) > \
                    potency_for(HEARTBREAK_SHOT, n, bd.JOB_DATA):
                return RAIN_OF_DEATH
            return HEARTBREAK_SHOT
        return None

    # --- Cast transitions ---------------------------------------------------------
    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))
        apply_cooldown(state, self.cooldowns, ability_id)

        # DoTs
        if ability_id == STORMBITE:
            state.stormbite_end = t + bd.DOT_DURATION_S
        elif ability_id == CAUSTIC_BITE:
            state.caustic_end = t + bd.DOT_DURATION_S
        elif ability_id == IRON_JAWS:
            # Refreshes only ACTIVE DoTs (the in-game rule; mirrored in scoring).
            if state.stormbite_end > t:
                state.stormbite_end = t + bd.DOT_DURATION_S
            if state.caustic_end > t:
                state.caustic_end = t + bd.DOT_DURATION_S

        # Hawk's Eye spends (the armed Barrage cast is free — Barrage grants
        # usability itself, so it doesn't consume the measured proc budget).
        elif ability_id in (REFULGENT_ARROW, SHADOWBITE):
            if state.barrage_armed:
                state.barrage_armed = False
            else:
                state.refulgent_remaining = max(0, state.refulgent_remaining - 1)

        # Soul Voice spends
        elif ability_id == APEX_ARROW:
            state.apex_remaining = max(0, state.apex_remaining - 1)
            if state.blast_remaining > 0:
                state.blast_ready = True
        elif ability_id == BLAST_ARROW:
            state.blast_ready = False
            state.blast_remaining = max(0, state.blast_remaining - 1)

        # Armed follow-ups
        elif ability_id == RESONANT_ARROW:
            state.resonant_ready_until = -1.0
        elif ability_id == RADIANT_ENCORE:
            state.encore_ready_until = -1.0
        elif ability_id == PITCH_PERFECT:
            state.pp_remaining = max(0, state.pp_remaining - 1)
        elif ability_id in (HEARTBREAK_SHOT, RAIN_OF_DEATH):
            state.hb_remaining = max(0, state.hb_remaining - 1)

        # Enablers
        elif ability_id == BARRAGE:
            state.barrage_armed = True
            state.resonant_ready_until = t + bd.BARRAGE_READY_DURATION_S
        elif ability_id == RADIANT_FINALE:
            state.encore_ready_until = t + bd.ENCORE_READY_DURATION_S
            state.coda = 0
            state.rf_used = True

        # The song cycle
        elif ability_id in bd.SONG_ORDER:
            if state.song == ARMYS_PAEON and ability_id != ARMYS_PAEON:
                state.muse_end = t + bd.MUSE_DURATION_S
            state.song = ability_id
            state.song_start = t
            state.song_idx += 1
            state.song_due_t = t + bd.SONG_SPLITS[ability_id]
            state.coda = min(3, state.coda + 1)

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        """Keep the song cycle rolling through a boss-untargetable gap: songs are
        targetless, and a real BRD re-songs during downtime so the haste/Coda/PP
        cadence is intact when the boss returns. Resources only — the damage
        casts stay in uptime."""
        saved_t = state.t
        try:
            while state.song_due_t < win_end - 0.5:
                song = bd.SONG_ORDER[state.song_idx % len(bd.SONG_ORDER)]
                ready = state.cd_ready.get(song, 0.0)
                cast_t = max(win_start, state.song_due_t, ready)
                if cast_t >= win_end - 1e-9 or ready > win_end:
                    break
                state.t = cast_t
                self.apply_cast(state, song)
        finally:
            state.t = saved_t

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction --------------------------------------------------------

def _default_ctx(duration_s: float) -> BardCtx:
    apex = max(0, int(duration_s / _DEFAULT_APEX_RATE_S))
    return BardCtx(
        refulgent_budget=max(0, int(duration_s / _DEFAULT_REFULGENT_RATE_S)),
        pp_budget=max(0, int(duration_s / _DEFAULT_PP_RATE_S)),
        apex_budget=apex,
        blast_budget=apex,
        hb_budget=max(0, int(duration_s / _DEFAULT_HB_RATE_S)),
    )


def _model_for(duration_s: float, sim_context) -> BardRotationModel:
    """Build a model bound to this run's per-pull context. After unwrapping any
    per-player effective GCD (CeilingContext) and multi-target schedule, the
    payload is the BardCtx of measured budgets; falls back to duration-scaled
    estimates when absent (warm-cache / Theorizer)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    ctx = payload if isinstance(payload, BardCtx) else _default_ctx(duration_s)
    return BardRotationModel(ctx=ctx, gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Engine-facing score_fn bound to a multi-target N(t) `schedule`. Lazy
    scoring import avoids a scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.bard.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


_score = _make_score()


# --- Module-level entrypoints (bind the model to the shared engine) ------------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — BRD has no pet/
    payload scalar, so aux is always 0."""
    if params is None:
        params = SimParams()
    model = _model_for(fight_duration_s, sim_context)
    return engine.run_rotation(model, fight_duration_s, downtime_windows or [], params)


def simulate_idealized_optimal(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The beam-searched optimum (buff-aware when given)."""
    return simulate_idealized_perfect(fight_duration_s, downtime_windows,
                                      buff_intervals, sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: sweep + burst-timing refinement + the diverse
    beam over the Iron Jaws fork (`beam_perfect`; width 1 == `perfect`).
    Buff-aware when `buff_intervals` is given."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.beam_perfect(model, _make_score(model.mt_schedule),
                               fight_duration_s, downtime_windows or [],
                               buff_intervals, width=_BEAM_WIDTH)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows.
    Falls back to the throughput optimum when there are no party buffs."""
    model = _model_for(fight_duration_s, sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
