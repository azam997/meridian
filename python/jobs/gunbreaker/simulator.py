"""Idealized GNB rotation — the Gunbreaker `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the GNB-specific rotation: the Powder Gauge (cartridge)
economy with its Bloodfest cap-expansion, the No Mercy burst self-buff folded into
the incremental score, the combo / Continuation machine, and the two snapshotted
DoTs. The four `simulate_*` shims at the bottom bind this model to the engine (names
kept so the sidecar / scorer / tests call them unchanged).

GNB-specific rotation encoded:
- **A cartridge spend-cadence fork** (why GNB needs the beam). At a combo boundary the
  rotation chooses which cartridge spender to play — Burst Strike / Gnashing Fang /
  Double Down — or keeps building (Keen Edge), or fires Sonic Break / the Reign combo
  when their procs are up. The fork is exposed in `gcd_candidates`; the beam picks the
  cadence (which spender under No Mercy, and whether to bank a cartridge for the next
  No Mercy vs spend now to avoid overcap). Banking falls out of the fork + the
  admissible per-cartridge `beam_prune` credit — no explicit hold state needed.
- **No Mercy self-buff folded INTO the incremental score** (the PLD Fight-or-Flight /
  DRG Lance Charge pattern): +20% for 20s, derived from the No Mercy casts on the
  timeline (never the cast that grants it), so the beam-prune key sees the true value of
  GCDs landed under the buff. The exact same per-cast math `score_delivered_potency`
  runs, so the prune key matches the final score (modulo raid buffs / tincture, applied
  at final selection). Symmetric on delivered + idealized -> the >100% guard holds.
- **Continuations** (Hypervelocity / Jugular Rip / Abdomen Tear / Eye Gouge / Fated
  Brand) — forced oGCDs proc'd off the preceding GCD, emitted at TOP priority in
  `pick_ogcd` so heavy weave load never drops them.
- **Sonic Break + Bow Shock DoTs** — 15s, scored by time-to-next-cast capped at 15s
  (the SAM Higanbana / DRG Chaotic Spring model), snapshotting No Mercy at cast.
- **In-game expiries** — Ready to Break (30s) gates Sonic Break; the 30s combo timer
  clears a mid-step basic/Gnashing/Reign chain across long downtime; Ready to Reign
  is consumed by Reign of Beasts and expires 30s after Bloodfest. The 10s continuation
  windows need no timer — each proc fires in its enabling GCD's own weave slot.
- **Bloodfest cap-expansion** — Bloodfest grants +3 cartridges AND raises the cap 3->6
  for 30s (a recent QoL change). Modeled with a `bloodfest_cap_end` timer; the cap is
  read dynamically in `apply_cast` and the cartridge count is in `beam_signature` as a
  full-fidelity integer so cap-6 and cap-3 states never collapse.

Out of scope for v1 (documented, intentionally not modeled):
- The exact-DP solver seam (GNB ships beam-only, like RPR/DRG; the diverse beam holds
  the ceiling on real 300-650s kills).
- Frame-perfect animation timing / defensive GCD interactions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from jobs._core.entry_gauge import EntryState, seed_entry_gauge
from jobs._core.sim import engine
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.gunbreaker import data as gd


# --- Ability IDs (aliased for readability) --------------------------------
KEEN_EDGE        = gd.KEEN_EDGE
BRUTAL_SHELL     = gd.BRUTAL_SHELL
SOLID_BARREL     = gd.SOLID_BARREL
BURST_STRIKE     = gd.BURST_STRIKE
GNASHING_FANG    = gd.GNASHING_FANG
SAVAGE_CLAW      = gd.SAVAGE_CLAW
WICKED_TALON     = gd.WICKED_TALON
DOUBLE_DOWN      = gd.DOUBLE_DOWN
SONIC_BREAK      = gd.SONIC_BREAK
REIGN_OF_BEASTS  = gd.REIGN_OF_BEASTS
NOBLE_BLOOD      = gd.NOBLE_BLOOD
LION_HEART       = gd.LION_HEART
DEMON_SLICE      = gd.DEMON_SLICE
DEMON_SLAUGHTER  = gd.DEMON_SLAUGHTER
FATED_CIRCLE     = gd.FATED_CIRCLE
NO_MERCY         = gd.NO_MERCY
BLOODFEST        = gd.BLOODFEST
BLASTING_ZONE    = gd.BLASTING_ZONE
BOW_SHOCK        = gd.BOW_SHOCK
HYPERVELOCITY    = gd.HYPERVELOCITY
JUGULAR_RIP      = gd.JUGULAR_RIP
ABDOMEN_TEAR     = gd.ABDOMEN_TEAR
EYE_GOUGE        = gd.EYE_GOUGE
FATED_BRAND      = gd.FATED_BRAND


# --- Rotation tuning ------------------------------------------------------
# GNB has NO haste self-buff -> the GCD is the gear-true 2.50 base (per-player Skill
# Speed threads in via `gcd_base_s`, only ever faster -> monotone-safe). ⚠ confirm the
# BiS GCD on the live calibration (it should land at ~2.50).
GNB_GCD_S = 2.50
_AOE_MIN_TARGETS = 3   # the dedicated AoE line wins from 3 targets

# One full DoT in potency (the trailing application is credited this much in the
# incremental beam-prune key, matching score_delivered_potency).
_SONIC_FULL_DOT_P = (gd.SONIC_BREAK_DOT_DURATION_S / gd.SONIC_BREAK_DOT_TICK_S
                     * gd.SONIC_BREAK_DOT_TICK_P)
_BOW_FULL_DOT_P = (gd.BOW_SHOCK_DOT_DURATION_S / gd.BOW_SHOCK_DOT_TICK_S
                   * gd.BOW_SHOCK_DOT_TICK_P)
# Admissible per-cartridge credit in the beam-prune key: each held cartridge is worth at
# LEAST a Burst Strike (the minimum-value spender; Gnashing ~full-combo and Double Down
# 500/cart are higher). Keeps a cartridge-banking line alive until its spender lands. Not
# scaled by No Mercy at bank time, so the bound holds even if No Mercy doesn't land.
_CARTRIDGE_PRUNE_VALUE = float(gd.POTENCIES[BURST_STRIKE])

# Hold the burst enablers into raid-buff windows; the payoff GCDs follow them.
_ANCHORS: tuple[int, ...] = (NO_MERCY, BLOODFEST)
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
# `beam_signature` buckets every burst / cooldown timer to this granularity (one GCD):
# two states within a GCD of each other on every timer choose the same next action, so
# they should dedup to one beam. Keeps the width spent on genuinely distinct lines
# (cartridge / combo / proc states) rather than sub-GCD timing near-duplicates.
_SIG_BUCKET_S = GNB_GCD_S

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    gd.JOB_DATA.tincture_main_stat, gd.JOB_DATA.tincture_role_coeff)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """GNB picker tunables — no axis beyond the shared knobs (max_weaves /
    forbidden_windows). The cartridge build-vs-spend cadence is the beam's job, not a
    sweep axis."""
    pass


@dataclass
class SimState(SimStateBase):
    # Cartridges (0-6; dynamic cap via `bloodfest_cap_end`).
    cartridges: int = 0
    bloodfest_cap_end: float = 0.0
    # Combo state machines.
    basic_combo_step: int = 0   # 0 boundary, 1 expect Brutal Shell, 2 expect Solid Barrel
    aoe_combo_step: int = 0      # 0 boundary, 1 expect Demon Slaughter
    gnashing_step: int = 0       # 0 none, 1 expect Savage Claw, 2 expect Wicked Talon
    reign_step: int = 0          # 0 none, 1 expect Noble Blood, 2 expect Lion Heart
    # No Mercy burst window end (the folded self-buff).
    no_mercy_end: float = 0.0
    # Procs. Ready to Break / Ready to Reign carry their in-game expiries; the five
    # continuation procs (10s in-game) need none — pick_ogcd fires each in its
    # enabling GCD's own weave slot, so they can never lapse in-sim.
    ready_to_break: bool = False     # No Mercy -> Sonic Break
    ready_to_break_end: float = 0.0  # 30s in-game window
    ready_to_reign: bool = False     # Bloodfest -> Reign combo
    ready_to_reign_end: float = 0.0
    ready_to_blast: bool = False     # Burst Strike -> Hypervelocity
    ready_to_rip: bool = False       # Gnashing Fang -> Jugular Rip
    ready_to_tear: bool = False      # Savage Claw -> Abdomen Tear
    ready_to_gouge: bool = False     # Wicked Talon -> Eye Gouge
    ready_to_raze: bool = False      # Fated Circle -> Fated Brand
    # DoT expiries (for the signature).
    sonic_dot_end: float = 0.0
    bow_dot_end: float = 0.0
    # Incremental (buff-agnostic, No-Mercy-AWARE) running score for the O(1) beam-prune
    # key — the exact per-cast math `score_delivered_potency` runs with raid buffs /
    # tincture off. `_score_flat` = sum of cast potencies x active No Mercy; `_score_fin_dot`
    # = finalized DoT of superseded applications; `_score_last_{sonic,bow}` = (cast_t,
    # snapshot_mult) of the trailing DoT (credited the full duration in the prune key).
    _score_flat: float = 0.0
    _score_fin_dot: float = 0.0
    _score_last_sonic: tuple[float, float] | None = None
    _score_last_bow: tuple[float, float] | None = None


def _no_mercy_mult(state: SimState, t: float) -> float:
    """The No Mercy self-buff multiplier active at `t` (set by an EARLIER cast)."""
    return gd.NO_MERCY_MULT if state.no_mercy_end > t else 1.0


class GunbreakerRotationModel(engine.BaseRotationModel):
    cooldowns = gd.COOLDOWNS
    timing = InstantGCD(base_s=GNB_GCD_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, entry: EntryState | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Phase-continuation entry state (carried cartridges out of M12S-P1). None on a
        # cold start -> byte-identical. Seeded onto init_state, so the ceiling can
        # front-load the same carried burst the player did (symmetric -> >100% guard holds).
        self.entry = entry
        # Multi-target N(t) schedule. Where N>=3 the picker forks to the dedicated AoE
        # line (Demon Slice/Slaughter + Fated Circle); the innately-cleaving ST casts
        # (Double Down / Reign / Bow Shock) get free-splash via `potency_for`. Empty ()
        # -> single target, byte-identical.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed: faster-than-constant only (monotone-safe). None keeps
        # the 2.50 constant, byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)

    def _n(self, t: float) -> int:
        return n_at(t, self.mt_schedule)

    def _cap(self, state: SimState) -> int:
        return (gd.CARTRIDGE_CAP_BLOODFEST if state.t < state.bloodfest_cap_end
                else gd.CARTRIDGE_CAP)

    def _dd_ready(self, state: SimState) -> bool:
        return state.cd_ready.get(DOUBLE_DOWN, 0.0) <= state.t

    def _gnashing_ready(self, state: SimState) -> bool:
        return state.charges.get(GNASHING_FANG, 0.0) >= 1.0

    def _reign_ready(self, state: SimState) -> bool:
        return state.ready_to_reign and state.t < state.ready_to_reign_end

    def _break_ready(self, state: SimState) -> bool:
        return state.ready_to_break and state.t < state.ready_to_break_end

    # --- Lifecycle ---------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {GNASHING_FANG: 2.0}
        state.cd_ready = {NO_MERCY: 0.0, BLOODFEST: 0.0, DOUBLE_DOWN: 0.0,
                          BLASTING_ZONE: 0.0, BOW_SHOCK: 0.0}
        # Phase-continuation: seed carried cartridges (name == SimState field).
        if self.entry is not None:
            seed_entry_gauge(state, self.entry.gauge_map, gd.JOB_DATA.gauges)
        return state

    def prepull(self, state: SimState, params) -> None:
        # Tank: already on the boss at the pull, no run-in (MELEE_TANK engage_delay 0).
        state.t = gd.JOB_DATA.role_policy.engage_delay_s

    # --- GCD selection -----------------------------------------------------

    def _forced_step(self, state: SimState) -> int | None:
        """The single forced next GCD when inside a locked combo, else None."""
        if state.gnashing_step == 1:
            return SAVAGE_CLAW
        if state.gnashing_step == 2:
            return WICKED_TALON
        if state.reign_step == 1:
            return NOBLE_BLOOD
        if state.reign_step == 2:
            return LION_HEART
        if state.basic_combo_step == 1:
            return BRUTAL_SHELL
        if state.basic_combo_step == 2:
            return SOLID_BARREL
        if state.aoe_combo_step == 1:
            return DEMON_SLAUGHTER
        return None

    def _st_boundary_pick(self, state: SimState) -> int:
        """Greedy boundary pick (single target): procs first, then spenders under No
        Mercy, then dump-near-cap / build. The beam explores the fork via
        `gcd_candidates`; this only needs a sane steady-state baseline."""
        if self._reign_ready(state):
            return REIGN_OF_BEASTS
        if self._break_ready(state):
            return SONIC_BREAK
        in_nm = state.no_mercy_end > state.t
        if in_nm:
            if state.cartridges >= 2 and self._dd_ready(state):
                return DOUBLE_DOWN
            if state.cartridges >= 1 and self._gnashing_ready(state):
                return GNASHING_FANG
            if state.cartridges >= 1:
                return BURST_STRIKE
        # Outside No Mercy: dump only to avoid overcap, else build toward the next burst.
        if state.cartridges >= self._cap(state):
            if state.cartridges >= 2 and self._dd_ready(state):
                return DOUBLE_DOWN
            if self._gnashing_ready(state):
                return GNASHING_FANG
            return BURST_STRIKE
        return KEEN_EDGE

    def _aoe_boundary_pick(self, state: SimState) -> int:
        if self._reign_ready(state):
            return REIGN_OF_BEASTS
        if self._break_ready(state):
            return SONIC_BREAK
        if state.cartridges >= 2 and self._dd_ready(state):
            return DOUBLE_DOWN
        if state.cartridges >= 1:
            return FATED_CIRCLE
        return DEMON_SLICE

    def pick_gcd(self, state: SimState, params) -> int:
        forced = self._forced_step(state)
        if forced is not None:
            return forced
        if self._n(state.t) >= _AOE_MIN_TARGETS:
            return self._aoe_boundary_pick(state)
        return self._st_boundary_pick(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The dense GCD move set. Inside a locked combo there is one forced move; at a
        combo boundary the cartridge spend-cadence fork exposes every legal option, and
        the beam (with `beam_signature` dedup) finds the cadence a top parse plays."""
        forced = self._forced_step(state)
        if forced is not None:
            return [forced]
        if self._n(state.t) >= _AOE_MIN_TARGETS:
            return self._aoe_candidates(state)
        return self._st_candidates(state)

    def _st_candidates(self, state: SimState) -> list[int]:
        cands: list[int] = [KEEN_EDGE]
        if self._reign_ready(state):
            cands.append(REIGN_OF_BEASTS)
        if self._break_ready(state):
            cands.append(SONIC_BREAK)
        if state.cartridges >= 1:
            cands.append(BURST_STRIKE)
            if self._gnashing_ready(state):
                cands.append(GNASHING_FANG)
        if state.cartridges >= 2 and self._dd_ready(state):
            cands.append(DOUBLE_DOWN)
        return self._drop_builder_near_cap(state, cands, KEEN_EDGE)

    def _aoe_candidates(self, state: SimState) -> list[int]:
        cands: list[int] = [DEMON_SLICE]
        if self._reign_ready(state):
            cands.append(REIGN_OF_BEASTS)
        if self._break_ready(state):
            cands.append(SONIC_BREAK)
        if state.cartridges >= 1:
            cands.append(FATED_CIRCLE)
        if state.cartridges >= 2 and self._dd_ready(state):
            cands.append(DOUBLE_DOWN)
        return self._drop_builder_near_cap(state, cands, DEMON_SLICE)

    def _drop_builder_near_cap(self, state: SimState, cands: list[int],
                               builder: int) -> list[int]:
        """At cap the builder's next finisher would overcap a cartridge — force a spend
        by dropping the builder (provided a spender is on offer)."""
        if state.cartridges >= self._cap(state):
            spenders = [c for c in cands if c != builder]
            if spenders:
                return spenders
        return cands

    # --- oGCD weaves -------------------------------------------------------

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows

        # Continuations FIRST — forced procs off the preceding GCD; if not woven their
        # potency is lost, so they outrank every other oGCD. Fired in the enabling
        # GCD's own weave slot (the first weave always fits a 2.5s slot), so the
        # in-game 10s Ready-to-X windows can never lapse in-sim.
        if state.ready_to_blast:
            return HYPERVELOCITY
        if state.ready_to_rip:
            return JUGULAR_RIP
        if state.ready_to_tear:
            return ABDOMEN_TEAR
        if state.ready_to_gouge:
            return EYE_GOUGE
        if state.ready_to_raze:
            return FATED_BRAND

        # No Mercy — open the +20% burst window. Fired ASAP (the engine's refine pass
        # then optimizes its timing via forbidden-window holds, and the beam packs the
        # GCD burst around it — both do better than any greedy hold heuristic here).
        if state.cd_ready.get(NO_MERCY, 0.0) <= t \
                and not is_forbidden(NO_MERCY, t, fw):
            return NO_MERCY
        # Bloodfest — +3 carts + Ready to Reign. Fire when the carts fit (always true at
        # the normal cap-3); aligns with No Mercy by sharing the 60s cadence.
        if state.cd_ready.get(BLOODFEST, 0.0) <= t \
                and state.cartridges <= gd.CARTRIDGE_CAP \
                and not is_forbidden(BLOODFEST, t, fw):
            return BLOODFEST
        # Blasting Zone — direct-damage oGCD.
        if state.cd_ready.get(BLASTING_ZONE, 0.0) <= t \
                and not is_forbidden(BLASTING_ZONE, t, fw):
            return BLASTING_ZONE
        # Bow Shock — the AoE DoT oGCD.
        if state.cd_ready.get(BOW_SHOCK, 0.0) <= t \
                and not is_forbidden(BOW_SHOCK, t, fw):
            return BOW_SHOCK
        return None

    # --- Cast transitions --------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Lazy dynamic-cap clamp: once the Bloodfest window ends, cartridges above the
        # base cap are lost (the player can't hold them either).
        if state.cartridges > gd.CARTRIDGE_CAP and t >= state.bloodfest_cap_end:
            state.cartridges = gd.CARTRIDGE_CAP

        # Incremental running score — the per-cast math `score_delivered_potency` runs
        # with raid buffs / tincture off. `potency_for` scales cleaving casts by the live
        # target count; at N==1 it equals POTENCIES.get, byte-identical.
        base_p = potency_for(ability_id, self._n(t), gd.JOB_DATA)
        if base_p > 0:
            state._score_flat += base_p * _no_mercy_mult(state, t)

        # DoTs (time-to-next; snapshot No Mercy at cast time).
        if ability_id == SONIC_BREAK:
            if state._score_last_sonic is not None:
                last_t, last_m = state._score_last_sonic
                gap = min(gd.SONIC_BREAK_DOT_DURATION_S, max(0.0, t - last_t))
                state._score_fin_dot += (gap / gd.SONIC_BREAK_DOT_TICK_S
                                         * gd.SONIC_BREAK_DOT_TICK_P * last_m)
            state._score_last_sonic = (t, _no_mercy_mult(state, t))
            state.sonic_dot_end = t + gd.SONIC_BREAK_DOT_DURATION_S
        elif ability_id == BOW_SHOCK:
            if state._score_last_bow is not None:
                last_t, last_m = state._score_last_bow
                gap = min(gd.BOW_SHOCK_DOT_DURATION_S, max(0.0, t - last_t))
                state._score_fin_dot += (gap / gd.BOW_SHOCK_DOT_TICK_S
                                         * gd.BOW_SHOCK_DOT_TICK_P * last_m)
            state._score_last_bow = (t, _no_mercy_mult(state, t))
            state.bow_dot_end = t + gd.BOW_SHOCK_DOT_DURATION_S

        # Bloodfest raises the cap FIRST so its +3 fits the expanded gauge.
        if ability_id == BLOODFEST:
            state.bloodfest_cap_end = t + gd.BLOODFEST_CAP_DURATION_S
            state.ready_to_reign = True
            state.ready_to_reign_end = t + gd.BLOODFEST_CAP_DURATION_S

        # Cartridge gauge (generators / spenders) under the live cap.
        cap = self._cap(state)
        if ability_id in gd.CARTRIDGE_GENERATORS:
            state.cartridges = min(cap, state.cartridges + gd.CARTRIDGE_GENERATORS[ability_id])
        if ability_id in gd.CARTRIDGE_SPENDERS:
            state.cartridges = max(0, state.cartridges - gd.CARTRIDGE_SPENDERS[ability_id])

        # No Mercy window + Ready to Break (AFTER scoring this cast, so the buff never
        # amps the No Mercy cast itself — which is 0 potency anyway).
        if ability_id == NO_MERCY:
            state.no_mercy_end = t + gd.NO_MERCY_DURATION_S
            state.ready_to_break = True
            state.ready_to_break_end = t + gd.READY_TO_BREAK_DURATION_S

        # Combo / proc transitions.
        self._advance(state, ability_id)

        # Generic cooldown / charges.
        apply_cooldown(state, self.cooldowns, ability_id)

    def _advance(self, state: SimState, ability_id: int) -> None:
        if ability_id == KEEN_EDGE:
            state.basic_combo_step = 1
            state.aoe_combo_step = 0
        elif ability_id == BRUTAL_SHELL:
            state.basic_combo_step = 2 if state.basic_combo_step == 1 else 0
        elif ability_id == SOLID_BARREL:
            state.basic_combo_step = 0
        elif ability_id == DEMON_SLICE:
            state.aoe_combo_step = 1
            state.basic_combo_step = 0
        elif ability_id == DEMON_SLAUGHTER:
            state.aoe_combo_step = 0
        elif ability_id == GNASHING_FANG:
            state.gnashing_step = 1
            state.ready_to_rip = True
        elif ability_id == SAVAGE_CLAW:
            state.gnashing_step = 2
            state.ready_to_tear = True
        elif ability_id == WICKED_TALON:
            state.gnashing_step = 0
            state.ready_to_gouge = True
        elif ability_id == BURST_STRIKE:
            state.ready_to_blast = True
        elif ability_id == FATED_CIRCLE:
            state.ready_to_raze = True
        elif ability_id == SONIC_BREAK:
            state.ready_to_break = False
        elif ability_id == REIGN_OF_BEASTS:
            # Ready to Reign is consumed HERE (the in-game buff drops on the first
            # cast); Noble Blood / Lion Heart continue via the combo chain alone.
            state.reign_step = 1
            state.ready_to_reign = False
        elif ability_id == NOBLE_BLOOD:
            state.reign_step = 2
        elif ability_id == LION_HEART:
            state.reign_step = 0
        elif ability_id == HYPERVELOCITY:
            state.ready_to_blast = False
        elif ability_id == JUGULAR_RIP:
            state.ready_to_rip = False
        elif ability_id == ABDOMEN_TEAR:
            state.ready_to_tear = False
        elif ability_id == EYE_GOUGE:
            state.ready_to_gouge = False
        elif ability_id == FATED_BRAND:
            state.ready_to_raze = False

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        # The in-game combo timer: a mid-step basic / Gnashing / Reign chain does
        # not survive a downtime that puts the next step more than 30s after the
        # last GCD — the ceiling must not resume a combo a real player would have
        # lost. (Ready to Break expires via its own end-time; the continuation
        # procs can never be pending here — each fires in its enabling GCD's own
        # weave slot.)
        if win_end - state.last_gcd_t > gd.COMBO_TIMEOUT_S:
            state.basic_combo_step = 0
            state.aoe_combo_step = 0
            state.gnashing_step = 0
            state.reign_step = 0

    # --- Beam search seam --------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental No-Mercy-aware running score (no
        re-scan). Equals `score_delivered_potency(... raid buffs/tincture off)` plus an
        admissible trailing-DoT credit (each open DoT credited its full duration, so a
        DoT line isn't pruned before it pays off) and a banked-cartridge credit. The
        engine re-scores survivors under `buff_intervals`, so ranking on raw potency only
        steers survival."""
        base = state._score_flat + state._score_fin_dot
        if state._score_last_sonic is not None:
            base += _SONIC_FULL_DOT_P * state._score_last_sonic[1]
        if state._score_last_bow is not None:
            base += _BOW_FULL_DOT_P * state._score_last_bow[1]
        return base + state.cartridges * _CARTRIDGE_PRUNE_VALUE

    def beam_signature(self, state: SimState):
        """Diversity-dedup key (engine.beam_search): keep ONE beam per distinct
        *decision* state so the width holds genuinely different lines, not
        near-duplicates that differ only by sub-GCD timing noise.

        The cartridge count (exact int), every combo step and every proc flag ARE the
        decision state — bucketed nowhere. The burst / cooldown timers are bucketed to a
        GCD (`_SIG_BUCKET_S`): two states in the same GCD-slot of every timer make the
        same next move, so collapsing them (keeping the higher-scoring one) only frees
        width. `state.t` is in the key EXACTLY (rounded to 0.01s): beams advance in
        lockstep at max_weaves=2, but the mw=3 sweep's triple-weave clip desyncs them by
        0.5s steps — collapsing two states at different t keeps the higher accumulated
        score and can drop the line with more remaining time. Same-t cohorts still dedup
        fully, which is where the sub-GCD-timer near-duplicate swarm lived.

        Why this shape and not the timers rounded to 0.01s: that lossless key fragmented
        the beam into swarms of near-identical states (same carts / combo / procs, timers
        off by hundredths), starving the *effective* width. It under-converged on the one
        fight with a mid-fight downtime — the M12S-P1 (enc 104) re-burst — where a real
        median parse beat the width-256 ceiling (>100.5%). Bucketing recovers, at width
        256, the ceiling the old key only reached at width 1024 (verified on the breach
        pull) at no wall-clock cost. Denser search can only RAISE the ceiling, so the
        >100% guard is strengthened, never weakened."""
        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)
        return (
            round(state.t, 2),
            state.cartridges,
            state.basic_combo_step, state.aoe_combo_step,
            state.gnashing_step, state.reign_step,
            state.ready_to_break, state.ready_to_reign,
            state.ready_to_blast, state.ready_to_rip, state.ready_to_tear,
            state.ready_to_gouge, state.ready_to_raze,
            slot(state.no_mercy_end - state.t),
            slot(state.ready_to_break_end - state.t),
            slot(state.bloodfest_cap_end - state.t),
            slot(state.ready_to_reign_end - state.t),
            slot(state.sonic_dot_end - state.t),
            slot(state.bow_dot_end - state.t),
            round(state.charges.get(GNASHING_FANG, 0.0), 1),
            slot(state.cd_ready.get(NO_MERCY, 0.0) - state.t),
            slot(state.cd_ready.get(BLOODFEST, 0.0) - state.t),
            slot(state.cd_ready.get(DOUBLE_DOWN, 0.0) - state.t),
            slot(state.cd_ready.get(BLASTING_ZONE, 0.0) - state.t),
            slot(state.cd_ready.get(BOW_SHOCK, 0.0) - state.t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding -----------------------------------

def _model_for(duration_s: float, sim_context) -> GunbreakerRotationModel:
    """Build a model bound to this run's per-pull context: a per-player effective GCD
    (CeilingContext), the free-splash N(t) schedule (MultiTargetContext), and/or the
    phase-continuation entry state (carried cartridges)."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    return GunbreakerRotationModel(entry=entry, gcd_base_s=gcd, mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    free-splash N(t) `schedule`. Buff-aware when given. Empty schedule -> single target,
    byte-identical. Lazy import to avoid a scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.gunbreaker.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(duration_key, sim_context)
    score = _make_score(model.mt_schedule)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, score, duration_key, list(downtime_tuple), buff_intervals,
        width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The GNB ceiling: the diverse beam over the cartridge fork on top of the burst-
    timing refinement. (Beam-only — the exact DP seam is deferred, like RPR/DRG.)"""
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
    """Run the idealized rotation once (greedy baseline). Returns (timeline, 0) — GNB
    has no pet/payload scalar, so aux is always 0."""
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
    """The GCD-perfect ceiling (buff-aware when given)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """Alias for the optimal ceiling — the beam over the cartridge forks on top of the
    refined burst timing (the real upper bound; no greedy floor)."""
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
    model = _model_for(fight_duration_s, sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
