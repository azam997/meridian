"""Idealized DRK rotation — the Dark Knight `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the DRK-specific rotation: the Blood + MP dual economy,
Darkside tracked as sim state (reconstructed identically on the delivered side),
the interleavable Delirium chain, the Scorn -> Disesteem window, the Salted Earth
-> Salt and Darkness gate, and the Living Shadow pet fold with its downtime hold +
tail escape. The four `simulate_*` shims at the bottom bind this model to the
engine (names kept so the sidecar / scorer / tests call them unchanged).

DRK-specific rotation encoded:
- **A Blood/MP spend-cadence fork** (why DRK needs the beam). At every GCD slot the
  rotation chooses: progress the combo, spend 50 Blood (Bloodspiller), play a
  Delirium-chain step (620/720/820 — interleavable, the basic combo survives it,
  probe-verified), or cash Scorn (Disesteem 1000). The fork is exposed in
  `gcd_candidates`; the beam picks the cadence (burst packing under raid windows,
  overcap avoidance). Banking falls out of the fork + the admissible per-resource
  `beam_prune` credits — no explicit hold state needed.
- **Darkside folded INTO the incremental score** (the GNB No-Mercy pattern with
  extend-and-cap semantics): +10% while active, extended +30s (cap 60s) by each
  Edge/Flood cast, from zero at the pull. The exact same per-cast math
  `score_delivered_potency` runs, so the prune key matches the final score.
- **MP as bespoke time-based state**: the passive combat tick (200 per 3s) accrues
  lazily via `_settle` — including across downtime windows, which is the point (the
  re-burst Edge dump after a gap is real) — plus the combo/Carve/Blood-Weapon/chain
  grants. The Blackest Night is MP-net-neutral for optimal play and deliberately
  un-modeled (data.py header; probe-validated ledger).
- **Living Shadow = the SMN fixed-count fold**: 2450 potency credited at the summon
  cast (both sides). The only hold is the downtime guard (don't summon into a real
  gap that eats > 4s of the ~20s pet window) with the mandatory fight-end escape —
  the fold symmetry rule: a player's full-credit tail summon must always be
  matchable by the ceiling (observed live: LS cast at 607s of a 616s kill).
- **In-game expiries** — the 15s Delirium/Blood Weapon buffs kill unspent stacks;
  the 30s combo timer clears mid-step combos across long downtime; Scorn lasts 30s.

Out of scope for v1 (documented, intentionally not modeled):
- The exact-DP solver seam (DRK ships beam-only, like RPR/DRG/GNB).
- TBN/Dark Arts (MP-net-neutral; see data.py) and defensive GCD interactions.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from jobs._core.entry_gauge import EntryState, seed_entry_gauge
from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.darkknight import data as dd


# --- Ability IDs (aliased for readability) --------------------------------
HARD_SLASH        = dd.HARD_SLASH
SYPHON_STRIKE     = dd.SYPHON_STRIKE
SOULEATER         = dd.SOULEATER
BLOODSPILLER      = dd.BLOODSPILLER
QUIETUS           = dd.QUIETUS
SCARLET_DELIRIUM  = dd.SCARLET_DELIRIUM
COMEUPPANCE       = dd.COMEUPPANCE
TORCLEAVER        = dd.TORCLEAVER
IMPALEMENT        = dd.IMPALEMENT
UNLEASH           = dd.UNLEASH
STALWART_SOUL     = dd.STALWART_SOUL
DISESTEEM         = dd.DISESTEEM
EDGE_OF_SHADOW    = dd.EDGE_OF_SHADOW
FLOOD_OF_SHADOW   = dd.FLOOD_OF_SHADOW
DELIRIUM          = dd.DELIRIUM
LIVING_SHADOW     = dd.LIVING_SHADOW
SALTED_EARTH      = dd.SALTED_EARTH
SALT_AND_DARKNESS = dd.SALT_AND_DARKNESS
CARVE_AND_SPIT    = dd.CARVE_AND_SPIT
ABYSSAL_DRAIN     = dd.ABYSSAL_DRAIN
SHADOWBRINGER     = dd.SHADOWBRINGER

_CHAIN_NEXT = {0: SCARLET_DELIRIUM, 1: COMEUPPANCE, 2: TORCLEAVER}


# --- Rotation tuning ------------------------------------------------------
# DRK has NO haste self-buff -> the GCD is the gear-true 2.50 base (per-player Skill
# Speed threads in via `gcd_base_s`, only ever faster -> monotone-safe). Probe: top
# parses run a flat 2.496-2.498s cadence in and out of burst.
DRK_GCD_S = 2.50
_AOE_MIN_TARGETS = 3   # the dedicated AoE line wins from 3 targets

# Living Shadow downtime hold (the SMN demi guard, verbatim semantics): Esteem's
# last hit lands +19.5s after the summon, so a downtime window that eats a
# meaningful chunk of that span means hold — but NEVER at the fight tail (the fold
# symmetry rule), and never for a sub-slot targetability blip.
LS_WINDOW_S = 20.0
LS_DOWNTIME_HOLD_S = 20.0
LS_HOLD_MIN_OVERLAP_S = 4.0
LS_TAIL_ESCAPE_S = 20.0

# Admissible banked-resource credits in the beam-prune key (raw potency, un-buffed):
# each keeps an investing/banking line alive until its payoff lands.
_BLOOD_PRUNE_VALUE = float(dd.POTENCIES[BLOODSPILLER])       # per 50 banked Blood
_EDGE_PRUNE_VALUE = float(dd.POTENCIES[EDGE_OF_SHADOW])      # per 3000 banked MP
_CHAIN_PRUNE_VALUE = float(dd.POTENCIES[SCARLET_DELIRIUM])   # per live Delirium stack
_SCORN_PRUNE_VALUE = float(dd.POTENCIES[DISESTEEM])          # a held Disesteem
_SND_PRUNE_VALUE = float(dd.POTENCIES[SALT_AND_DARKNESS])    # an armed Salt and Darkness

# Hold the burst enablers into raid-buff windows; the payoff GCDs follow them.
_ANCHORS: tuple[int, ...] = (DELIRIUM, LIVING_SHADOW)
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
# `beam_signature` buckets every burst / cooldown timer to this granularity (one
# GCD). DRK's slots are uniform 2.5s (no sub-GCD recasts), so beams advance in
# lockstep and `state.t` stays exact in the key (the GNB shape, not SMN's fine
# t-bucket).
_SIG_BUCKET_S = DRK_GCD_S

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    dd.JOB_DATA.tincture_main_stat, dd.JOB_DATA.tincture_role_coeff)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """DRK picker tunables — no axis beyond the shared knobs (max_weaves /
    forbidden_windows). The Blood/MP spend cadence is the beam's job, not a
    sweep axis."""
    pass


@dataclass
class SimState(SimStateBase):
    # Blood Gauge (0-100). Name matches GaugeModel("blood") for entry seeding.
    blood: int = 0
    # MP: starts full every pull (out-of-combat regen refills it — cold starts and
    # phase continuations alike, which is why there is no entry-MP axis), accrues
    # the passive tick lazily via `_settle`, spent 3000 per Edge/Flood.
    mp: int = dd.MP_MAX
    _mp_anchor: float = 0.0     # tick accrual watermark (preserves tick phase)
    # Combo state machines.
    basic_combo_step: int = 0   # 0 boundary, 1 expect Syphon, 2 expect Souleater
    aoe_combo_step: int = 0     # 0 boundary, 1 expect Stalwart Soul
    # Delirium: the two 15s stack buffs one button grants. The chain is
    # interleavable — `delirium_step` persists across other GCDs until the buff
    # expires (probe-verified live behavior).
    delirium_stacks: int = 0    # chain charges (Scarlet/Comeuppance/Torcleaver)
    delirium_step: int = 0      # 0 expect Scarlet, 1 expect Comeuppance, 2 Torcleaver
    bw_stacks: int = 0          # Blood Weapon: +10 Blood +600 MP per weaponskill
    delirium_end: float = 0.0
    # Scorn (Living Shadow -> Disesteem), Salted Earth, Darkside.
    scorn_end: float = 0.0
    salted_earth_end: float = 0.0
    salt_darkness_ready: bool = False
    darkside_end: float = 0.0
    # Incremental (buff-agnostic, DARKSIDE-aware) running score for the O(1)
    # beam-prune key — the exact per-cast math `score_delivered_potency` runs with
    # raid buffs / tincture off.
    _score_flat: float = 0.0


def _darkside_mult(state: SimState, t: float) -> float:
    """The Darkside multiplier active at `t` (set by an EARLIER Edge/Flood cast)."""
    return dd.DARKSIDE_MULT if state.darkside_end > t else 1.0


class DarkKnightRotationModel(engine.BaseRotationModel):
    # Edge/Flood carry a local 1s recast so a slot can double-weave Edge (probe:
    # 1.0s Edge-Edge gaps live) but never triple. They are NOT in JobData.cooldowns
    # — MP-gated, and the drift detector must not watch them.
    cooldowns = {**dd.COOLDOWNS,
                 EDGE_OF_SHADOW: (1.0, 1), FLOOD_OF_SHADOW: (1.0, 1)}
    timing = InstantGCD(base_s=DRK_GCD_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, entry: EntryState | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Phase-continuation entry state (carried Blood). Measured: M12S-P2 opens
        # COLD (the Blood ledger closes at entry 0), so this is a safety net —
        # None on a cold start -> byte-identical.
        self.entry = entry
        # Multi-target N(t) schedule. Where N>=3 the picker forks to the dedicated
        # AoE line (Unleash/Stalwart + Quietus/Impalement/Flood/Abyssal); the
        # innately-cleaving ST casts (Disesteem / Shadowbringer / Salted Earth /
        # Salt and Darkness) get free-splash via `potency_for`. Empty () ->
        # single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed: faster-than-constant only (monotone-safe). None
        # keeps the 2.50 constant, byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    # --- Economy upkeep ------------------------------------------------------

    def _settle(self, state: SimState) -> None:
        """Lazy upkeep called before every decision/transition: accrue the passive
        MP tick up to `state.t` (through downtime too — combat regen continues
        while the boss is untargetable), and expire dead Delirium stacks so the
        signature doesn't fragment on unusable state."""
        ticks = int((state.t - state._mp_anchor) / dd.MP_TICK_S)
        if ticks > 0:
            state.mp = min(dd.MP_MAX, state.mp + ticks * dd.MP_PER_TICK)
            state._mp_anchor += ticks * dd.MP_TICK_S
        if state.t >= state.delirium_end and (
                state.delirium_stacks or state.bw_stacks or state.delirium_step):
            state.delirium_stacks = 0
            state.bw_stacks = 0
            state.delirium_step = 0

    def _chain_ready(self, state: SimState) -> bool:
        return state.delirium_stacks > 0 and state.t < state.delirium_end

    def _ls_burns_into_downtime(self, state: SimState) -> bool:
        """True when an imminent downtime window would eat a MEANINGFUL chunk of
        Esteem's ~20s attack window (summon after the gap instead — a player never
        summons into a real gap, but summons straight through a sub-slot
        targetability blip). NEVER holds at the fight tail: the fold is credited
        at cast, so a truncated window still pays (the fold symmetry rule)."""
        t = state.t
        if state.fight_duration_s - t < LS_TAIL_ESCAPE_S:
            return False
        for s, e in state.downtime_windows:
            if not (t < s < t + LS_DOWNTIME_HOLD_S):
                continue
            overlap = min(e, t + LS_WINDOW_S) - s
            if overlap > LS_HOLD_MIN_OVERLAP_S \
                    and e < state.fight_duration_s - LS_TAIL_ESCAPE_S:
                return True
        return False

    # --- Lifecycle ---------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {SHADOWBRINGER: 2.0}
        state.cd_ready = {DELIRIUM: 0.0, LIVING_SHADOW: 0.0, SALTED_EARTH: 0.0,
                          CARVE_AND_SPIT: 0.0, EDGE_OF_SHADOW: 0.0,
                          FLOOD_OF_SHADOW: 0.0}
        # Phase-continuation: seed carried Blood (name == SimState field).
        if self.entry is not None:
            seed_entry_gauge(state, self.entry.gauge_map, dd.JOB_DATA.gauges)
        return state

    def prepull(self, state: SimState, params) -> None:
        # Tank: already on the boss at the pull, no run-in (MELEE_TANK engage_delay 0).
        state.t = dd.JOB_DATA.role_policy.engage_delay_s
        state._mp_anchor = state.t

    # --- GCD selection -----------------------------------------------------

    def _combo_step_pick(self, state: SimState) -> int:
        if state.basic_combo_step == 1:
            return SYPHON_STRIKE
        if state.basic_combo_step == 2:
            return SOULEATER
        return HARD_SLASH

    def _st_pick(self, state: SimState) -> int:
        """Greedy pick (single target): the chain first (it dies with the 15s
        buff), then Scorn's Disesteem, then Blood spends, then the combo. The
        beam explores the full interleave via `gcd_candidates`; this only needs
        a sane steady-state baseline."""
        if self._chain_ready(state):
            return _CHAIN_NEXT[state.delirium_step]
        if state.scorn_end > state.t:
            return DISESTEEM
        if state.blood >= 50:
            return BLOODSPILLER
        return self._combo_step_pick(state)

    def _aoe_pick(self, state: SimState) -> int:
        if self._chain_ready(state):
            return IMPALEMENT
        if state.scorn_end > state.t:
            return DISESTEEM
        if state.blood >= 50:
            return QUIETUS
        if state.aoe_combo_step == 1:
            return STALWART_SOUL
        return UNLEASH

    def pick_gcd(self, state: SimState, params) -> int:
        self._settle(state)
        if self._n(state.t) >= _AOE_MIN_TARGETS:
            return self._aoe_pick(state)
        return self._st_pick(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The dense GCD move set. The Delirium chain is interleavable (the basic
        combo persists across it — probe-verified), so there is no forced step:
        every legal option is on offer and the beam (with `beam_signature` dedup)
        finds the burst packing a top parse plays. Legality is encoded HERE (the
        beam ignores greedy priority and will exploit any gate left to it)."""
        self._settle(state)
        if self._n(state.t) >= _AOE_MIN_TARGETS:
            return self._aoe_candidates(state)
        return self._st_candidates(state)

    def _st_candidates(self, state: SimState) -> list[int]:
        cands: list[int] = [self._combo_step_pick(state)]
        if state.blood >= 50:
            cands.append(BLOODSPILLER)
        if self._chain_ready(state):
            cands.append(_CHAIN_NEXT[state.delirium_step])
        if state.scorn_end > state.t:
            cands.append(DISESTEEM)
        return cands

    def _aoe_candidates(self, state: SimState) -> list[int]:
        cands: list[int] = [STALWART_SOUL if state.aoe_combo_step == 1 else UNLEASH]
        if state.blood >= 50:
            cands.append(QUIETUS)
        if self._chain_ready(state):
            cands.append(IMPALEMENT)
        if state.scorn_end > state.t:
            cands.append(DISESTEEM)
        return cands

    # --- oGCD weaves -------------------------------------------------------

    def pick_ogcd(self, state: SimState, params):
        self._settle(state)
        t = state.t
        fw = params.forbidden_windows

        # Living Shadow — the 120s anchor (the 2450 fold + Scorn). Held only for
        # the downtime guard; never at the tail.
        if state.cd_ready.get(LIVING_SHADOW, 0.0) <= t \
                and not self._ls_burns_into_downtime(state) \
                and not is_forbidden(LIVING_SHADOW, t, fw):
            return LIVING_SHADOW
        # Delirium — the 60s chain enabler. Fired ASAP (the engine's refine pass
        # optimizes its timing via forbidden-window holds, and the beam packs the
        # GCD burst around it).
        if state.cd_ready.get(DELIRIUM, 0.0) <= t \
                and not is_forbidden(DELIRIUM, t, fw):
            return DELIRIUM
        # Salted Earth — the 90s ground fold; arms Salt and Darkness.
        if state.cd_ready.get(SALTED_EARTH, 0.0) <= t \
                and not is_forbidden(SALTED_EARTH, t, fw):
            return SALTED_EARTH
        # Carve and Spit — 540 + 600 MP on the 60s cadence.
        if state.cd_ready.get(CARVE_AND_SPIT, 0.0) <= t \
                and not is_forbidden(CARVE_AND_SPIT, t, fw):
            return CARVE_AND_SPIT
        # Salt and Darkness — gated on the LIVE Salted Earth patch (1:1, probed).
        if state.salt_darkness_ready and state.salted_earth_end > t \
                and not is_forbidden(SALT_AND_DARKNESS, t, fw):
            return SALT_AND_DARKNESS
        # Shadowbringer — 2 charges on 60s; requires Darkside (trivially live).
        if state.charges.get(SHADOWBRINGER, 0.0) >= 1.0 \
                and state.darkside_end > t \
                and not is_forbidden(SHADOWBRINGER, t, fw):
            return SHADOWBRINGER
        # Edge of Shadow — the MP dump + the Darkside refresh. In the strict
        # (raid-buff-agnostic) scenario spend timing is potency-neutral, so ASAP
        # maximizes Edges (no MP-cap tick waste); the buff-aware layers re-time it.
        if self._n(t) >= _AOE_MIN_TARGETS:
            if state.mp >= dd.EDGE_MP_COST \
                    and state.cd_ready.get(FLOOD_OF_SHADOW, 0.0) <= t \
                    and not is_forbidden(FLOOD_OF_SHADOW, t, fw):
                return FLOOD_OF_SHADOW
        elif state.mp >= dd.EDGE_MP_COST \
                and state.cd_ready.get(EDGE_OF_SHADOW, 0.0) <= t \
                and not is_forbidden(EDGE_OF_SHADOW, t, fw):
            return EDGE_OF_SHADOW
        return None

    # --- Cast transitions --------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        self._settle(state)
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental running score — the per-cast math `score_delivered_potency`
        # runs with raid buffs / tincture off. `potency_for` scales cleaving casts
        # by the live target count; at N==1 it equals POTENCIES.get.
        base_p = potency_for(ability_id, self._n(t), dd.JOB_DATA)
        if base_p > 0:
            state._score_flat += base_p * _darkside_mult(state, t)

        is_gcd = ability_id not in dd.OGCD_IDS

        # Blood Weapon: each weaponskill under the live buff = +10 Blood +600 MP,
        # one stack (chain and combo GCDs alike — the beam may interleave).
        if is_gcd and state.bw_stacks > 0 and t < state.delirium_end:
            state.bw_stacks -= 1
            state.blood = min(dd.BLOOD_CAP, state.blood + dd.BLOOD_WEAPON_BLOOD)
            state.mp = min(dd.MP_MAX, state.mp + dd.BLOOD_WEAPON_MP)

        # Combo finishers generate Blood; spenders consume it.
        if ability_id in (SOULEATER, STALWART_SOUL):
            state.blood = min(dd.BLOOD_CAP, state.blood + 20)
        elif ability_id in (BLOODSPILLER, QUIETUS):
            state.blood = max(0, state.blood - 50)

        # MP grants (the sim always combos, so Syphon/Stalwart carry the grant).
        if ability_id in (SYPHON_STRIKE, STALWART_SOUL):
            state.mp = min(dd.MP_MAX, state.mp + dd.COMBO_MP_GRANT)
        elif ability_id == CARVE_AND_SPIT:
            state.mp = min(dd.MP_MAX, state.mp + dd.CARVE_MP_GRANT)
        elif ability_id in (EDGE_OF_SHADOW, FLOOD_OF_SHADOW):
            state.mp -= dd.EDGE_MP_COST

        # The Delirium chain: consume a stack, advance the step, +200 MP restore.
        if ability_id in (SCARLET_DELIRIUM, COMEUPPANCE, TORCLEAVER):
            state.delirium_stacks = max(0, state.delirium_stacks - 1)
            state.delirium_step = (state.delirium_step + 1) % 3
            state.mp = min(dd.MP_MAX, state.mp + dd.CHAIN_RESTORE_MP)
        elif ability_id == IMPALEMENT:
            state.delirium_stacks = max(0, state.delirium_stacks - 1)
            state.mp = min(dd.MP_MAX, state.mp + dd.CHAIN_RESTORE_MP)

        # Grants / windows (AFTER scoring, so a buff never amps its granting cast).
        if ability_id == DELIRIUM:
            state.delirium_stacks = dd.DELIRIUM_STACKS
            state.bw_stacks = dd.BLOOD_WEAPON_STACKS
            state.delirium_step = 0
            state.delirium_end = t + dd.DELIRIUM_DURATION_S
        elif ability_id == LIVING_SHADOW:
            state.scorn_end = t + dd.SCORN_DURATION_S
        elif ability_id == DISESTEEM:
            state.scorn_end = 0.0
        elif ability_id == SALTED_EARTH:
            state.salted_earth_end = t + 15.0
            state.salt_darkness_ready = True
        elif ability_id == SALT_AND_DARKNESS:
            state.salt_darkness_ready = False
        elif ability_id in (EDGE_OF_SHADOW, FLOOD_OF_SHADOW):
            state.darkside_end = min(t + dd.DARKSIDE_CAP_S,
                                     max(state.darkside_end, t) + dd.DARKSIDE_EXTEND_S)

        # Combo transitions (the chain does NOT touch the basic combo — probed).
        if ability_id == HARD_SLASH:
            state.basic_combo_step = 1
            state.aoe_combo_step = 0
        elif ability_id == SYPHON_STRIKE:
            state.basic_combo_step = 2 if state.basic_combo_step == 1 else 0
        elif ability_id == SOULEATER:
            state.basic_combo_step = 0
        elif ability_id == UNLEASH:
            state.aoe_combo_step = 1
            state.basic_combo_step = 0
        elif ability_id == STALWART_SOUL:
            state.aoe_combo_step = 0

        # Generic cooldown / charges.
        apply_cooldown(state, self.cooldowns, ability_id)

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # The in-game combo timer: a mid-step combo (or chain step) does not
        # survive a downtime that puts the next GCD more than 30s after the last
        # — the ceiling must not resume a combo a real player would have lost.
        # (The Delirium/Blood Weapon stacks expire via `delirium_end` in `_settle`;
        # MP keeps ticking through the gap, which is correct and intended.)
        if win_end - state.last_gcd_t > dd.COMBO_TIMEOUT_S:
            state.basic_combo_step = 0
            state.aoe_combo_step = 0
            state.delirium_step = 0

    # --- Beam search seam --------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental Darkside-aware running score
        (no re-scan), plus admissible banked-resource credits so an investing /
        banking line isn't pruned before its payoff lands: banked Blood at
        Bloodspiller value, banked MP at Edge value, live chain stacks, a held
        Disesteem, an armed Salt and Darkness. The engine re-scores survivors
        under `buff_intervals`, so ranking on raw potency only steers survival.
        No time-fairness term: DRK's slots are uniform 2.5s, so beams advance in
        lockstep (the SMN short-slot starvation cannot occur)."""
        t = state.t
        credit = (state.blood // 50) * _BLOOD_PRUNE_VALUE
        credit += (state.mp // dd.EDGE_MP_COST) * _EDGE_PRUNE_VALUE
        if state.delirium_stacks and state.delirium_end > t:
            credit += state.delirium_stacks * _CHAIN_PRUNE_VALUE
        if state.scorn_end > t:
            credit += _SCORN_PRUNE_VALUE
        if state.salt_darkness_ready and state.salted_earth_end > t:
            credit += _SND_PRUNE_VALUE
        return state._score_flat + credit

    def beam_signature(self, state: SimState):
        """Diversity-dedup key (engine.beam_search): keep ONE beam per distinct
        *decision* state so the width holds genuinely different lines, not
        near-duplicates that differ only by sub-GCD timing noise. Gauge / combo /
        stack state is exact; `mp` is exact in 200-units (every MP delta is a
        multiple of 200 and all beams share the tick phase, so this is lossless);
        the buff / cooldown timers are bucketed to a GCD (the GNB shape —
        uniform-slot lockstep keeps `state.t` exact in the key)."""
        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)
        return (
            round(state.t, 2),
            state.blood,
            state.mp // 200,
            state.basic_combo_step, state.aoe_combo_step,
            state.delirium_stacks, state.delirium_step, state.bw_stacks,
            state.salt_darkness_ready,
            slot(state.delirium_end - state.t),
            slot(state.scorn_end - state.t),
            slot(state.salted_earth_end - state.t),
            slot(state.darkside_end - state.t),
            round(state.charges.get(SHADOWBRINGER, 0.0), 1),
            slot(state.cd_ready.get(DELIRIUM, 0.0) - state.t),
            slot(state.cd_ready.get(LIVING_SHADOW, 0.0) - state.t),
            slot(state.cd_ready.get(SALTED_EARTH, 0.0) - state.t),
            slot(state.cd_ready.get(CARVE_AND_SPIT, 0.0) - state.t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding -----------------------------------

def _model_for(sim_context) -> DarkKnightRotationModel:
    """Build a model bound to this run's per-pull context: a per-player effective
    GCD (CeilingContext), the free-splash N(t) schedule (MultiTargetContext),
    and/or the phase-continuation entry state (carried Blood)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    return DarkKnightRotationModel(entry=entry, gcd_base_s=gcd,
                                   mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to
    a free-splash N(t) `schedule`. Buff-aware when given. Empty schedule -> single
    target, byte-identical. Lazy import to avoid a scoring<->simulator cycle."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.darkknight.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(sim_context)
    score = _make_score(model.mt_schedule)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, score, duration_key, list(downtime_tuple), buff_intervals,
        width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The DRK ceiling: the diverse beam over the Blood/MP/chain fork on top of
    the burst-timing refinement. (Beam-only — the exact DP seam is deferred, like
    RPR/DRG/GNB.)"""
    tl, aux = _perfect_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        tuple((round(s, 3), round(e, 3), round(m, 4))
              for s, e, m in buff_intervals) if buff_intervals else None,
        sim_context)
    return list(tl), aux


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once (greedy baseline). Returns (timeline, 0) —
    the pet fold rides the Living Shadow cast id, so aux is always 0."""
    if params is None:
        params = SimParams()
    model = _model_for(sim_context)
    return engine.run_rotation(model, fight_duration_s, downtime_windows or [], params)


def simulate_idealized_optimal(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling (buff-aware when given)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Alias for the optimal ceiling — the beam over the Blood/MP/chain forks on
    top of the refined burst timing (the real upper bound; no greedy floor)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_canonical_aligned(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Idealized rotation with the 2-min burst forced into the raid-buff windows.
    Falls back to the throughput optimum when there are no party buffs."""
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
