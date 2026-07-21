"""Idealized SAM rotation — the Samurai `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the SAM-specific rotation: the Kenki / Sen / Meditation
state, the priority pickers, the per-cast transitions, and the Tengentsu
bonus-Kenki schedule. The four `simulate_*` shims at the bottom bind this model to
the engine (names kept so the sidecar / scorer / tests call them unchanged).

SAM-specific rotation encoded:
- **Sen build via three combos** (cycled for three distinct Sen): Gyofu->Jinpu->
  Gekko (Getsu), Gyofu->Shifu->Kasha (Ka), Gyofu->Yukikaze (Setsu). At 3 Sen ->
  Iaijutsu (Tendo Setsugekka if a Meikyo-granted Tendo is up, else Midare
  Setsugekka), each followed by its Tsubame-Gaeshi (Kaeshi) replay.
- **Higanbana** is the 1-Sen Iaijutsu, cast when the DoT is about to fall and Sen
  happens to be 1 (so the 3-Sen Setsugekka is never sacrificed for it).
- **Meikyo Shisui** grants 3 instant enders (build 3 Sen with no starters) + Tendo
  (the next Iaijutsu becomes the 1100-potency Tendo Setsugekka).
- **Ikishoten** grants Ogi Namikiri Ready (-> Ogi Namikiri -> Kaeshi: Namikiri) +
  Zanshin Ready (50-Kenki oGCD) + 50 Kenki.
- **Kenki** is dumped on Hissatsu: Senei (60s) / Zanshin (proc) when available,
  else Hissatsu: Shinten to stay under the 100 cap.
- **Tengentsu Kenki** (the defensive's +10/proc) is injected as scheduled income
  from the per-pull `sim_context` proc count — the ceiling spends the *same* Kenki
  the player measured (symmetric; see scoring.py and data.py).

Out of scope for v1 (documented, intentionally not modeled):
- AoE (Tenka/Tendo Goken, Fuko, Mangetsu/Oka, Kyuten, Guren) — single-target ceiling.
- Exact Fugetsu/Fuka buff timers (Fugetsu is a full-coverage overlay in scoring;
  Fuka is baked into the GCD base). Positional hit/miss (idealized always hits).
- Meditate (downtime-only; a live probe showed top parses never use it in-kill).
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

from jobs._core.sim import engine, optimal
from jobs._core.sim.aoe_potency import n_at, potency_for, schedule_target_fn
from jobs._core.sim.engine import SimParamsBase, SimStateBase, apply_cooldown, is_forbidden
from jobs._core.sim.timing import InstantGCD
from jobs._core.tincture import spec_for_job
from jobs.samurai import data as sd


# --- Ability IDs (aliased for readability) --------------------------------
GYOFU                   = sd.GYOFU
JINPU                   = sd.JINPU
GEKKO                   = sd.GEKKO
SHIFU                   = sd.SHIFU
KASHA                   = sd.KASHA
YUKIKAZE                = sd.YUKIKAZE
HIGANBANA               = sd.HIGANBANA
MIDARE_SETSUGEKKA       = sd.MIDARE_SETSUGEKKA
TENDO_SETSUGEKKA        = sd.TENDO_SETSUGEKKA
KAESHI_SETSUGEKKA       = sd.KAESHI_SETSUGEKKA
TENDO_KAESHI_SETSUGEKKA = sd.TENDO_KAESHI_SETSUGEKKA
IKISHOTEN               = sd.IKISHOTEN
OGI_NAMIKIRI            = sd.OGI_NAMIKIRI
KAESHI_NAMIKIRI         = sd.KAESHI_NAMIKIRI
ZANSHIN                 = sd.ZANSHIN
MEIKYO_SHISUI           = sd.MEIKYO_SHISUI
HISSATSU_SHINTEN        = sd.HISSATSU_SHINTEN
HISSATSU_SENEI          = sd.HISSATSU_SENEI
SHOHA                   = sd.SHOHA
ENPI                    = sd.ENPI
# AoE line (cast only in multi-target windows; gated on N >= _AOE_MIN_TARGETS).
FUKO                    = sd.FUKO
MANGETSU                = sd.MANGETSU
OKA                     = sd.OKA
TENKA_GOKEN             = sd.TENKA_GOKEN
TENDO_GOKEN             = sd.TENDO_GOKEN
KAESHI_GOKEN            = sd.KAESHI_GOKEN
TENDO_KAESHI_GOKEN      = sd.TENDO_KAESHI_GOKEN


# --- Rotation tuning ------------------------------------------------------
# Maintained-Fuka GCD (2.50 base x0.87 haste, with typical SkS gear). ⚠️ tune in
# calibration against observed top-SAM GCD counts — too slow lifts the ceiling
# under top parses (>100%).
SAM_GCD_S = 2.14
# Higanbana refresh threshold: at a lone Sen the beam forks [refresh now, keep
# building toward a Midare] once the DoT has <= this left, so the search picks the
# 8-vs-10 cadence itself (the Sen-credit prune keeps the build/skip line alive).
HIGANBANA_REFRESH_AT_S = 15.0
# Dump Kenki on Shinten at/above this to stay under the 100 cap.
KENKI_DUMP_AT = 50
# SAM's AoE crossover is 3 targets, not 2. The AoE combo (Fuko -> Mangetsu/Oka)
# builds a Sen one GCD quicker than the ST combo and dumps it as the 2-Sen Tenka/
# Tendo Goken, but at N=2 that faster-but-AoE throughput still loses to banking the
# 2 Sen toward a 3-Sen Midare Setsugekka (680p). Verified by the 2026-06-23 AoE
# audit: a forced AoE line scores ~0.5% UNDER full single target at N=2 (and the
# diverse beam, offered the losing AoE candidates, was pruning the correct ST line
# in its myopic top-K) but wins from N=3 (+2960). So the AoE forks in
# `gcd_candidates` are offered only at N >= this. See [[aoe-multitarget-modeling]].
_AOE_MIN_TARGETS = 3
# One full 60s Higanbana DoT in potency (the trailing application is credited this
# much in the incremental beam-prune key, matching score_delivered_potency).
_HIGANBANA_FULL_DOT_P = (sd.HIGANBANA_DOT_DURATION_S / sd.HIGANBANA_DOT_TICK_S
                         * sd.HIGANBANA_DOT_TICK_P)


def _dot_segment(gap_s: float) -> float:
    """Finalized Higanbana DoT for an application whose next refresh (or fight end)
    is `gap_s` later, capped at the 60s duration — the exact-solver counterpart of
    the trailing-DoT term in `score_delivered_potency` / `_higanbana_dot_potency`."""
    covered = min(sd.HIGANBANA_DOT_DURATION_S, max(0.0, gap_s))
    return covered / sd.HIGANBANA_DOT_TICK_S * sd.HIGANBANA_DOT_TICK_P

# --- Admissible-bound inputs (derived from the data tables, never hardcoded, so a
# potency / crit-mult edit can't silently make `admissible_remaining` inadmissible —
# which would be a silently-wrong "provable" optimum). Each is >= the true scored
# value (or income/cost) of the cast it caps.
_SEN_BUILDER_IDS = (GYOFU, JINPU, SHIFU, GEKKO, KASHA, YUKIKAZE)
_UB_TENDO_P: float = sd.POTENCIES[TENDO_SETSUGEKKA] * sd.GUARANTEED_CRIT_MULT
_UB_OGI_P: float = sd.POTENCIES[OGI_NAMIKIRI] * sd.GUARANTEED_CRIT_MULT
_UB_MIDARE_P: float = sd.POTENCIES[MIDARE_SETSUGEKKA] * sd.GUARANTEED_CRIT_MULT
_UB_FILLER_P: float = float(max(sd.POTENCIES[a] for a in _SEN_BUILDER_IDS))
_UB_KENKI_PER_GCD: int = max(sd.KENKI_GENERATORS[a] for a in _SEN_BUILDER_IDS)

# Fallback proc count when no measured Tengentsu count is supplied (warm-cache /
# Theorizer). ~1 block per this many seconds (the live-probe cadence, ~16.8 in a
# 620s fight). The real value is the player's measured count, threaded as sim_context.
_DEFAULT_TENGENTSU_PERIOD_S = 33.0
# How long before the pull the opener Meikyo is pressed (during the countdown). Its
# recast starts then, so a charge has already partly regenerated at t=0 — this is
# load-bearing: it's what lets the LAST in-fight Meikyo->Tendo land before a ~390s
# kill ends (without the lead the 9th Meikyo fires ~10s too late to convert).
_PREPULL_MEIKYO_LEAD_S = 7.0


class SamContext(NamedTuple):
    """The per-pull scalars threaded into the SAM ceiling via `sim_context`
    (hashable -> joins the perfect-sim cache key). All measured from the player /
    refs and applied symmetrically:
      * bonus_kenki       — the player's measured Tengentsu Kenki (Tengentsu's
                            Foresight applications x10).
      * entry_kenki/_meditation — gauge the player carried INTO this pull (a phased
                            fight's P1->P2 leftover; measured from their opening, so
                            the ceiling opens just as loaded).
      * meditate_cap_s    — the most downtime Meditate any top-10 ref achieved on
                            this encounter (Meditate needs you stationary, so not
                            all downtime is usable); None until refs are known
                            (pre-ref the ceiling assumes the full window). 0 means
                            no ref Meditated -> the ceiling assumes none either."""
    bonus_kenki: int = 0
    entry_kenki: int = 0
    entry_meditation: int = 0
    meditate_cap_s: float | None = None


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """SAM picker tunables — no axis beyond the shared knobs in v1 (max_weaves /
    forbidden_windows). The per-pull scalars (Tengentsu Kenki, entry gauge,
    Meditate cap) are model parameters (sim_context), not sweep axes."""
    pass


@dataclass
class SimState(SimStateBase):
    kenki: int = 0
    sen_mask: int = 0            # bitmask of held Sen types (_GETSU/_KA/_SETSU)
    meditation: int = 0          # 0-3 -> Shoha
    meikyo_stacks: int = 0       # 0-3 free combo enders (no starter needed)
    combo_step: int = 0          # 0 fresh (Gyofu), 1 after starter, 2 after 2nd
    combo_target: int = 0        # the Sen bit the in-progress 3-step combo builds
    combo_is_aoe: bool = False   # the in-progress combo was started by Fuko (AoE):
                                 #   its 2nd step is Mangetsu/Oka, not Jinpu/Shifu
    kaeshi_goken_ready: bool = False        # Tenka Goken -> Kaeshi: Goken replay
    tendo_kaeshi_goken_ready: bool = False  # Tendo Goken -> Tendo Kaeshi: Goken
    tendo: bool = False          # Meikyo-granted: next Iaijutsu -> Tendo Setsugekka
    ogi_ready: bool = False      # from Ikishoten
    zanshin_ready: bool = False  # from Ikishoten
    kaeshi_setsugekka_ready: bool = False
    tendo_kaeshi_ready: bool = False
    kaeshi_namikiri_ready: bool = False
    higanbana_dot_end: float = 0.0
    # Tengentsu +10-Kenki blocks scheduled across the fight (set in prepull).
    tengentsu_procs: list[float] = field(default_factory=list)
    # Incremental (buff-agnostic) running score, maintained in apply_cast so the
    # beam's `beam_prune` ranking is O(1) instead of re-scanning the timeline each
    # node (what makes a wide diversity beam fast). `_score_flat` = sum of cast
    # potencies x guaranteed-crit; `_score_fin_dot` = finalized Higanbana DoT of
    # already-superseded applications; `_score_last_higan` = the trailing Higanbana's
    # cast time (credited the full 60s DoT in the prune key, matching the scorer).
    _score_flat: float = 0.0
    _score_fin_dot: float = 0.0
    _score_last_higan: float | None = None
    # Pot-aware EXACT counterparts for the DP's `exact_g`/`terminal_g`: the
    # canonical scorer folds the in-sim pot marker into a multiplier window even
    # on the buff-agnostic path, so the flat sums above diverge the moment the
    # sim pots (the opener) — the old fallback was a full re-scan per DP state,
    # the measured bottleneck (68% of a 240s solve). Each contribution is scaled
    # by the pot multiplier at its snapshot time; the trailing Higanbana's
    # multiplier is snapshotted at its cast (the DoT credits at the application
    # instant, `_higanbana_dot_potency`).
    _g_main: float = 0.0
    _g_fin_dot: float = 0.0
    _g_last_higan_m: float = 1.0


# --- Refinement / canonical anchors ---------------------------------------
# Hold the burst ENABLERS (Ikishoten / Meikyo) into raid-buff windows; the payoff
# GCDs (Ogi, Tendo Setsugekka, Senei, Zanshin) follow them, so aligning the
# enablers aligns the whole burst without force-holding Sen-bound GCDs.
_PERFECT_ANCHORS: tuple[int, ...] = (IKISHOTEN, MEIKYO_SHISUI)
_CANONICAL_ALIGN_ANCHORS: tuple[int, ...] = (IKISHOTEN, MEIKYO_SHISUI)

# Sweep axes (kept job-local).
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)


# Sen TYPES — Midare/Tendo Setsugekka need all three distinct (Getsu+Ka+Setsu);
# Higanbana spends any one. Tracked as a bitmask so the beam can explore *which*
# Sen feeds a 1-Sen Higanbana (the cheap 2-GCD Setsu, or a free Meikyo ender) vs.
# which it banks toward the 3-Sen Midare — without ever forming an illegal
# duplicate-Sen Midare (the bug a count-based model would allow).
_GETSU, _KA, _SETSU = 1, 2, 4
_ALL_SEN = _GETSU | _KA | _SETSU
_SEN_ORDER = (_GETSU, _KA, _SETSU)
_SEN_ENDER = {_GETSU: GEKKO, _KA: KASHA, _SETSU: YUKIKAZE}   # instant / 3rd-step ender
_SEN_SECOND = {_GETSU: JINPU, _KA: SHIFU}                    # the 2nd combo step
# (Setsu is a 2-step combo: Gyofu -> Yukikaze, no distinct 2nd step.)


def _sen_count(mask: int) -> int:
    return bin(mask).count("1")


def _missing_sen(mask: int) -> list[int]:
    return [b for b in _SEN_ORDER if not (mask & b)]


# --- The SAM rotation model -----------------------------------------------

# The tincture the sim places in-rotation (placed by the shared engine `_maybe_pot`,
# scored at cast time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    sd.JOB_DATA.tincture_main_stat, sd.JOB_DATA.tincture_role_coeff)


class SamuraiRotationModel(engine.BaseRotationModel):
    cooldowns = sd.COOLDOWNS
    timing = InstantGCD(base_s=SAM_GCD_S)
    agnostic_anchors = _PERFECT_ANCHORS
    buff_anchors = _PERFECT_ANCHORS
    canonical_anchors = _CANONICAL_ALIGN_ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, bonus_kenki: int = 0, entry_kenki: int = 0,
                 entry_meditation: int = 0, meditate_cap_s: float | None = None,
                 gcd_base_s: float | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        self.bonus_kenki = max(0, int(bonus_kenki))
        self.entry_kenki = max(0, min(sd.KENKI_CAP, int(entry_kenki)))
        self.entry_meditation = max(0, min(3, int(entry_meditation)))
        self.meditate_cap_s = meditate_cap_s
        # Multi-target N(t) schedule (the AoE-aware ceiling): where N>=2 the
        # candidates fork in the 2-step AoE combo (Fuko -> Mangetsu/Oka, 2 Sen ->
        # Tenka/Tendo Goken). Empty () -> single target, byte-identical. The DP is
        # skipped under an AoE schedule (its admissible bound is single-target), so
        # the diverse beam alone holds the multi-target ceiling.
        self.mt_schedule = mt_schedule
        # Per-player Skill Speed: `gcd_base_s` is the player's measured Fuka GCD when
        # faster than the constant (the cadence inference's band centers on the Fuka
        # 2.14s cluster). None keeps the constant, byte-identical.
        if gcd_base_s is not None:
            self.timing = InstantGCD(base_s=gcd_base_s)

    def _n(self, t: float) -> int:
        """Live target count at `t` from the multi-target schedule (1 if none)."""
        return n_at(t, self.mt_schedule)

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {MEIKYO_SHISUI: 2.0}
        state.cd_ready = {IKISHOTEN: 0.0, HISSATSU_SENEI: 0.0}
        # Gauge carried into the pull (a phased fight's P1->P2 leftover).
        state.kenki = self.entry_kenki
        state.meditation = self.entry_meditation
        return state

    def prepull(self, state: SimState, params) -> None:
        engage = sd.JOB_DATA.role_policy.engage_delay_s
        state.t = engage
        # Pre-pull Meikyo Shisui — the standard SAM opener presses it during the
        # countdown, so the 3 instant enders + Tendo are live at the pull and the
        # first GCDs are a Tendo Setsugekka burst. It spends ONE of the two charges
        # pre-fight, so the in-fight rotation starts with 1 charge (regenerating).
        # This is load-bearing for the ceiling: with a 55s/2-charge recast the 9th
        # in-fight Meikyo can't press before ~t=385, so a real opener's 9th
        # Meikyo->Tendo in a ~390s fight is ONLY reachable via this pre-pull press.
        state.meikyo_stacks = 3
        state.tendo = True
        # 1 charge remaining, plus the countdown recast lead already banked toward
        # the spent charge (so the in-fight Meikyo cadence — and the last Tendo —
        # land as early as a real pre-pulled opener).
        recast_s = sd.COOLDOWNS[MEIKYO_SHISUI][0]
        state.charges[MEIKYO_SHISUI] = min(2.0, 1.0 + _PREPULL_MEIKYO_LEAD_S / recast_s)
        # Schedule the Tengentsu +10-Kenki blocks evenly across the fight.
        dur = state.fight_duration_s
        n = self.bonus_kenki // sd.TENGENTSU_KENKI_PER_PROC
        if n <= 0 and self.bonus_kenki == 0:
            n = int(dur / _DEFAULT_TENGENTSU_PERIOD_S)
        state.tengentsu_procs = [dur * (i + 0.5) / n for i in range(n)] if n > 0 else []

    def _release_tengentsu_kenki(self, state: SimState) -> None:
        """Add +10 Kenki for each scheduled Tengentsu block now due (capped)."""
        while state.tengentsu_procs and state.tengentsu_procs[0] <= state.t:
            state.tengentsu_procs.pop(0)
            state.kenki = min(sd.KENKI_CAP, state.kenki + sd.TENGENTSU_KENKI_PER_PROC)

    def _needs_higanbana(self, state: SimState) -> bool:
        # Deliberately buff-agnostic. Refreshing Higanbana early to snapshot the
        # 60s DoT inside a raid window was measured net-NEGATIVE: the clipped live
        # DoT (~16.7p/s) plus the Sen-economy distortion outweigh the ~18% buff,
        # so the natural expiry cadence already maximises Higanbana potency. (MCH
        # Queen banking pays only because battery is near-waste-free to hold.)
        return state.higanbana_dot_end - state.t <= HIGANBANA_REFRESH_AT_S

    def pick_gcd(self, state: SimState, params) -> int:
        self._release_tengentsu_kenki(state)

        # All non-combo GCDs happen at a combo boundary (combo_step == 0) so a
        # combo is never broken (which would drop the combo bonus).
        if state.combo_step == 0:
            # Tsubame-Gaeshi / Kaeshi follow-ups — immediate next GCD.
            if state.kaeshi_namikiri_ready:
                return KAESHI_NAMIKIRI
            if state.tendo_kaeshi_ready:
                return TENDO_KAESHI_SETSUGEKKA
            if state.kaeshi_setsugekka_ready:
                return KAESHI_SETSUGEKKA
            # Ogi Namikiri (from Ikishoten).
            if state.ogi_ready and not is_forbidden(OGI_NAMIKIRI, state.t,
                                                    params.forbidden_windows):
                return OGI_NAMIKIRI
            # Iaijutsu dumps.
            if state.sen_mask == _ALL_SEN:
                return TENDO_SETSUGEKKA if state.tendo else MIDARE_SETSUGEKKA
            if _sen_count(state.sen_mask) == 1 and self._needs_higanbana(state):
                return HIGANBANA

        return self._build_sen_gcd(state)

    def _build_sen_gcd(self, state: SimState) -> int:
        """The next GCD building the greedy-default missing Sen (canonical order
        Getsu -> Ka -> Setsu). The beam explores other build orders + feeds via
        `gcd_candidates`."""
        missing = _missing_sen(state.sen_mask)
        return self._build_target_gcd(state, missing[0] if missing else _SETSU)

    def _build_target_gcd(self, state: SimState, target: int) -> int:
        """The next GCD to build a *specific* Sen type. With Meikyo the ender is
        instant; otherwise the 3-step (Getsu/Ka) or 2-step (Setsu) combo. Mid-combo
        (`combo_step == 2`) the 3rd step is locked to `combo_target`."""
        if state.meikyo_stacks > 0 and state.combo_step == 0:
            return _SEN_ENDER[target]
        if state.combo_step == 0:
            return GYOFU
        if state.combo_step == 1:
            return _SEN_SECOND.get(target, YUKIKAZE)   # Setsu ends here (Yukikaze)
        return _SEN_ENDER.get(state.combo_target, GEKKO)   # combo_step == 2 (locked)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """**Every legal GCD** at this slot — the dense move set (a superset of the
        old targeted one). Forced single move when mid-combo, a Kaeshi follow-up is
        pending, or 3 Sen must dump; otherwise the full fork: Ogi (if ready),
        Higanbana (lone Sen + refreshable DoT), and a Sen builder toward *each*
        missing Sen (a Meikyo instant ender, or a combo start whose 2nd step then
        forks on which Sen). Dense candidates used to *dilute* a fixed-width beam
        (measured worse), but paired with `beam_signature` dedup they no longer do —
        the dedup keeps the width on distinct lines, so the search reaches the
        Sen-feeding / Higanbana-cadence optimum a real top parse plays (see the
        [sam-exact-solver-diagnostic] memory: the over-100% pull was a search gap)."""
        self._release_tengentsu_kenki(state)
        n = self._n(state.t)
        cs = state.combo_step
        if cs == 1:                                        # which Sen this combo builds
            if state.combo_is_aoe:                         # after Fuko: AoE 2nd step
                opts = []
                if not (state.sen_mask & _GETSU):
                    opts.append(MANGETSU)
                if not (state.sen_mask & _KA):
                    opts.append(OKA)
                return opts or [MANGETSU]
            opts = [_SEN_SECOND.get(b, YUKIKAZE) for b in _missing_sen(state.sen_mask)]
            return opts or [GYOFU]
        if cs == 2:                                        # locked 3rd step
            return [_SEN_ENDER.get(state.combo_target, GEKKO)]
        # combo_step == 0 — forced Kaeshi / Ogi follow-ups first (AoE Goken replays
        # included; their flags are only ever set on an N>=2 line).
        if state.kaeshi_namikiri_ready:
            return [KAESHI_NAMIKIRI]
        if state.tendo_kaeshi_ready:
            return [TENDO_KAESHI_SETSUGEKKA]
        if state.tendo_kaeshi_goken_ready:
            return [TENDO_KAESHI_GOKEN]
        if state.kaeshi_setsugekka_ready:
            return [KAESHI_SETSUGEKKA]
        if state.kaeshi_goken_ready:
            return [KAESHI_GOKEN]
        moves: list[int] = []
        if state.ogi_ready and not is_forbidden(
                OGI_NAMIKIRI, state.t, params.forbidden_windows):
            moves.append(OGI_NAMIKIRI)
        if state.sen_mask == _ALL_SEN:
            moves.append(TENDO_SETSUGEKKA if state.tendo else MIDARE_SETSUGEKKA)
        else:
            missing = _missing_sen(state.sen_mask)
            sen_n = _sen_count(state.sen_mask)
            if sen_n == 1 and self._needs_higanbana(state):
                moves.append(HIGANBANA)
            # AoE Iaijutsu dump (only at N>=3): 2 Sen -> the 2-Sen Tenka/Tendo Goken.
            # At 1-2 targets the ST line always banks the 2 Sen toward the 3-Sen Midare.
            if n >= _AOE_MIN_TARGETS and sen_n == 2:
                moves.append(TENDO_GOKEN if state.tendo else TENKA_GOKEN)
            if state.meikyo_stacks > 0:                    # instant ender per missing Sen
                moves.extend(_SEN_ENDER[b] for b in missing)
                if n >= _AOE_MIN_TARGETS:                  # + the instant AoE enders
                    if not (state.sen_mask & _GETSU):
                        moves.append(MANGETSU)
                    if not (state.sen_mask & _KA):
                        moves.append(OKA)
            else:
                moves.append(GYOFU)                        # ST combo start (2nd-step forks Sen)
                if n >= _AOE_MIN_TARGETS:
                    moves.append(FUKO)                     # AoE combo start (-> Mangetsu/Oka)
        seen: set[int] = set()
        out = [m for m in moves if not (m in seen or seen.add(m))]
        return out or [self.pick_gcd(state, params)]

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """Top-K ranking key, computed O(1) from the incremental running score (NOT a
        re-scan of the timeline — that is what lets the wide diversity beam stay
        fast). Equals `score_delivered_potency(..., optimistic_dot=False)` (trailing
        Higanbana full 60s, a clipped one penalized) + an admissible banked-Sen
        credit (each Sen -> ~1/3 of a Midare + Kaeshi), which keeps a
        build-toward-Midare line that is momentarily behind alive until its delayed
        Midare lands. Buff-agnostic by design — the engine's final selection
        re-scores the surviving beams under `buff_intervals`, so ranking on raw
        potency only steers survival, never the result. (A fully-optimistic DoT here
        was tried and is WORSE: crediting EVERY Higanbana the full 60s over-rewards
        Higanbana-spam and reintroduces the over-refresh collapse.)"""
        base = state._score_flat + state._score_fin_dot
        if state._score_last_higan is not None:
            base += _HIGANBANA_FULL_DOT_P
        return base + _sen_count(state.sen_mask) * _SEN_PRUNE_VALUE

    def beam_signature(self, state: SimState):
        """Lossless diversity-dedup key (see engine.beam_search): the full
        future-relevant state, so two beams that reached an identical state (e.g. the
        same Sen via a different build order) collapse to the higher-scoring one,
        freeing the fixed width for genuinely distinct lines."""
        return (
            round(state.t, 2), state.kenki, state.sen_mask, state.meditation,
            state.meikyo_stacks, state.combo_step, state.combo_target, state.tendo,
            state.combo_is_aoe, state.kaeshi_goken_ready, state.tendo_kaeshi_goken_ready,
            state.ogi_ready, state.zanshin_ready, state.kaeshi_setsugekka_ready,
            state.tendo_kaeshi_ready, state.kaeshi_namikiri_ready,
            round(state.higanbana_dot_end - state.t, 2),
            round(state.charges.get(MEIKYO_SHISUI, 0.0), 3),
            round(max(0.0, state.cd_ready.get(IKISHOTEN, 0.0) - state.t), 2),
            round(max(0.0, state.cd_ready.get(HISSATSU_SENEI, 0.0) - state.t), 2),
        )

    # --- Exact-solver seam (optimal.solve_optimal) --------------------------
    # `legal_gcds` defaults to the dense `gcd_candidates` above. These overrides
    # promote the proven SAM prototype (scripts/solve_samurai_optimal.py) onto the
    # model so the engine's job-agnostic DP converges on a full pull.

    def clone(self, state: SimState) -> SimState:
        """Field-aware shallow clone for the hot DP loop (~10x copy.deepcopy): copy
        only the mutated containers; the read-only downtime / buff intervals are
        shared, the scalar gauge/flag/running-score fields ride the shallow copy."""
        new = copy.copy(state)
        new.charges = dict(state.charges)
        new.cd_ready = dict(state.cd_ready)
        new.timeline = list(state.timeline)
        new.tengentsu_procs = list(state.tengentsu_procs)
        return new

    def dominance_key(self, state: SimState):
        """The CATEGORICAL bucket: everything that changes which moves are legal /
        forced (so two states with different keys are genuinely incomparable). The
        continuous resources that only *accumulate* go in `dominance_vector`. Sen
        mask, combo, the armed Kaeshi/Ogi/Zanshin/Tendo flags, the Meikyo stack count
        (it swaps instant enders for a Gyofu starter), and the quantized Higanbana
        remaining (it gates the refresh fork at <=15s) are all categorical."""
        return (
            round(state.t, 2), state.sen_mask, state.combo_step, state.combo_target,
            state.tendo, state.ogi_ready, state.zanshin_ready,
            state.kaeshi_setsugekka_ready, state.tendo_kaeshi_ready,
            state.kaeshi_namikiri_ready, state.meikyo_stacks,
            round(state.higanbana_dot_end - state.t, 2),
        )

    def dominance_vector(self, state: SimState) -> tuple:
        """The MONOTONE-GOOD resources (more is always weakly better, so a state
        ahead on all of them and on score dominates): Kenki, Meditation, the
        regenerating Meikyo charge, and the Ikishoten / Senei cooldowns negated so
        readier = larger. None of these change the legal-move *set* (their spenders
        are oGCDs), so a dominated line can always be replayed for >= score."""
        return (
            state.kenki, state.meditation,
            round(state.charges.get(MEIKYO_SHISUI, 0.0), 3),
            -round(max(0.0, state.cd_ready.get(IKISHOTEN, 0.0) - state.t), 2),
            -round(max(0.0, state.cd_ready.get(HISSATSU_SENEI, 0.0) - state.t), 2),
        )

    def exact_g(self, state: SimState, score_fn) -> float:
        """Exact score of the committed prefix — the POT-AWARE incremental running
        score (`_g_main` + finalized DoT + the trailing Higanbana credited by
        ELAPSED time, an underestimate the bound's `dot_ub` covers), O(1) for the
        whole buff-agnostic rank ceiling. Each contribution was banked scaled by
        the pot multiplier at its snapshot time (`BaseRotationModel.pot_mult_at`),
        which is what keeps this exact once the opener pot drops — the old
        re-score-when-`tincture_used` fallback was a full timeline re-scan per DP
        state, the measured bottleneck (68% of a 240s solve). A raid-buff-aware
        solve still re-scores (the DP only runs the agnostic axis)."""
        if state.buff_intervals:
            return score_fn(state.timeline, 0, state.buff_intervals)
        g = state._g_main + state._g_fin_dot
        if state._score_last_higan is not None:
            g += (_dot_segment(state.t - state._score_last_higan)
                  * state._g_last_higan_m)
        return g

    def terminal_g(self, state: SimState, score_fn) -> float:
        """Score of a COMPLETE rotation — the trailing Higanbana credited its full
        60s DoT at its snapshot multiplier, matching `score_delivered_potency`'s
        end-of-fight convention (the correctness gate: this must equal the
        canonical scorer on the timeline; pinned by the solver-invariants harness
        and the prefix exactness test)."""
        if state.buff_intervals:
            return score_fn(state.timeline, 0, state.buff_intervals)
        g = state._g_main + state._g_fin_dot
        if state._score_last_higan is not None:
            g += _HIGANBANA_FULL_DOT_P * state._g_last_higan_m
        return g

    def admissible_remaining(self, state: SimState) -> float:
        """Resource-gated admissible upper bound on the remaining reward (never an
        underestimate), ported from the prototype's `_ub_tight`. Each premium cast
        count is capped by its binding resource (Meikyo->Tendo, Ikishoten->Ogi,
        Sen-throughput->Midare); the remaining GCD slots are filled highest-value
        first, the rest at the best builder; oGCDs are Kenki/CD/meditation gated; all
        Higanbana DoT is bounded by continuous uptime + one full DoT. Looser than
        reality, so the B&B prune it drives stays lossless."""
        rem = state.fight_duration_s - state.t
        if rem <= 0:
            return 0.0
        gcd_s = self.timing.base_s
        n = int(math.ceil(rem / gcd_s)) + 1                 # >= remaining GCD slots
        # Tendo Setsugekka pairs (Setsugekka + Tendo Kaeshi), gated by Meikyo presses.
        mk = state.charges.get(MEIKYO_SHISUI, 0.0)
        mk_recast = sd.COOLDOWNS[MEIKYO_SHISUI][0]
        n_tendo = (1 if state.tendo else 0) + int(math.floor(mk + rem / (mk_recast / 2.0))) + 1
        # Ogi pairs (Ogi + Kaeshi Namikiri), gated by Ikishoten.
        iki_in = max(0.0, state.cd_ready.get(IKISHOTEN, 0.0) - state.t)
        n_iki = (1 if iki_in <= rem else 0) + int(rem / sd.COOLDOWNS[IKISHOTEN][0]) + 1
        n_ogi = (1 if state.ogi_ready else 0) + n_iki
        # Midare pairs: remaining 3-Sen sets beyond the Tendo ones (Sen-throughput cap).
        # NB: a builder-aware tightening of this cap (Sen cost >= 1 GCD via Meikyo, >= 2
        # GCDs via combo, vs the 1-Sen-per-GCD assumed here) was implemented and measured
        # — it is admissible (the 240/300s bench dp stayed byte-identical) but moved the
        # wall NOT AT ALL: SAM's solve is bounded by the Pareto FRONTIER, not the B&B
        # bound (`pruned_bound` is ~0 next to `pruned_dominance`; the bound rarely crosses
        # the beam-seed floor even when tightened ~2x). So it was reverted as inert
        # complexity. SAM's gate stays at 240s; raising it would need a smaller frontier,
        # not a tighter bound. See the exact-optimal-rotation-solver memory.
        max_sets = (_sen_count(state.sen_mask) + n) // 3
        n_midare = max(0, max_sets - n_tendo)
        # Fill the n GCD slots highest-value-first (admissible per-cast ceilings).
        hi = ([_UB_TENDO_P] * (2 * n_tendo) + [_UB_OGI_P] * (2 * n_ogi)
              + [_UB_MIDARE_P] * (2 * n_midare))
        hi.sort(reverse=True)
        gcd_ub = sum(hi[:n]) if len(hi) >= n else sum(hi) + _UB_FILLER_P * (n - len(hi))
        # oGCDs (Kenki / CD / meditation gated).
        senei_in = max(0.0, state.cd_ready.get(HISSATSU_SENEI, 0.0) - state.t)
        n_senei = (1 if senei_in <= rem else 0) \
            + int(rem / sd.COOLDOWNS[HISSATSU_SENEI][0]) + 1
        n_iaijutsu = n_tendo + n_midare + n_ogi
        n_shoha = (state.meditation + n_iaijutsu) // 3
        kenki_income = (state.kenki + n * _UB_KENKI_PER_GCD
                        + n_iki * sd.KENKI_GENERATORS[IKISHOTEN]
                        + len(state.tengentsu_procs) * sd.TENGENTSU_KENKI_PER_PROC)
        shinten_cost = sd.KENKI_SPENDERS[HISSATSU_SHINTEN]
        n_shinten = max(0, int((kenki_income
                                - n_senei * sd.KENKI_SPENDERS[HISSATSU_SENEI]
                                - n_ogi * sd.KENKI_SPENDERS[ZANSHIN]) // shinten_cost))
        ogcd_ub = (n_senei * sd.POTENCIES[HISSATSU_SENEI]
                   + n_ogi * sd.POTENCIES[ZANSHIN]
                   + n_shoha * sd.POTENCIES[SHOHA]
                   + n_shinten * sd.POTENCIES[HISSATSU_SHINTEN])
        # All Higanbana DoT (future ticks + the end-of-fight full-credit artifact).
        dot_ub = (rem / sd.HIGANBANA_DOT_TICK_S) * sd.HIGANBANA_DOT_TICK_P + _HIGANBANA_FULL_DOT_P
        # Inflate by the tincture multiplier so the bound stays admissible once the
        # in-sim pot multiplies covered casts (looser, never an underestimate).
        tinct = self.tincture_spec.multiplier if self.tincture_spec else 1.0
        return (gcd_ub + ogcd_ub + dot_ub) * tinct

    def pick_ogcd(self, state: SimState, params):
        self._release_tengentsu_kenki(state)
        t = state.t
        fw = params.forbidden_windows

        # Ikishoten — burst enabler (Ogi + Zanshin + 50 Kenki).
        if state.cd_ready.get(IKISHOTEN, 0) <= t \
                and not is_forbidden(IKISHOTEN, t, fw):
            return IKISHOTEN
        # Zanshin — proc spender (50 Kenki).
        if state.zanshin_ready and state.kenki >= 50:
            return ZANSHIN
        # Hissatsu: Senei — 60s, premium 25-Kenki spender.
        if state.cd_ready.get(HISSATSU_SENEI, 0) <= t and state.kenki >= 25 \
                and not is_forbidden(HISSATSU_SENEI, t, fw):
            return HISSATSU_SENEI
        # Shoha — spend 3 Meditation.
        if state.meditation >= 3:
            return SHOHA
        # Meikyo Shisui — burst tool (instant enders + Tendo). Don't fire while a
        # previous Meikyo's enders/Tendo are still unspent: a second Meikyo would
        # just re-set the Tendo flag, collapsing two charges into ONE Tendo
        # Setsugekka (real play spaces them, banking the charge). So gate on both
        # no leftover stacks AND no pending Tendo.
        if state.charges.get(MEIKYO_SHISUI, 0) >= 1 and state.meikyo_stacks == 0 \
                and not state.tendo and not is_forbidden(MEIKYO_SHISUI, t, fw):
            return MEIKYO_SHISUI
        # Hissatsu: Shinten — Kenki dump to stay under cap.
        if state.kenki >= KENKI_DUMP_AT:
            return HISSATSU_SHINTEN
        return None

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental (buff-agnostic) running score for the O(1) beam-prune key,
        # plus the pot-aware exact accumulators for the DP's `exact_g`. The
        # per-target potency (`potency_for`) scales AoE buttons by the live target
        # count; at N==1 it equals POTENCIES.get, byte-identical.
        base_p = potency_for(ability_id, self._n(t), sd.JOB_DATA)
        if base_p > 0:
            flat = base_p * (sd.GUARANTEED_CRIT_MULT
                             if ability_id in sd.ALWAYS_CRIT_IDS else 1.0)
            state._score_flat += flat
            state._g_main += flat * self.pot_mult_at(state, t)
        if ability_id == HIGANBANA:
            if state._score_last_higan is not None:        # finalize the prior DoT
                gap = min(sd.HIGANBANA_DOT_DURATION_S, max(0.0, t - state._score_last_higan))
                seg = gap / sd.HIGANBANA_DOT_TICK_S * sd.HIGANBANA_DOT_TICK_P
                state._score_fin_dot += seg
                state._g_fin_dot += seg * state._g_last_higan_m
            state._score_last_higan = t
            state._g_last_higan_m = self.pot_mult_at(state, t)

        # Kenki gauge.
        if ability_id in sd.KENKI_GENERATORS:
            state.kenki = min(sd.KENKI_CAP,
                              state.kenki + sd.KENKI_GENERATORS[ability_id])
        if ability_id in sd.KENKI_SPENDERS:
            state.kenki = max(0, state.kenki - sd.KENKI_SPENDERS[ability_id])

        # Cooldown / charges (generic).
        apply_cooldown(state, self.cooldowns, ability_id)

        # Combo + Sen progression (Sen tracked by TYPE so a Midare always needs 3
        # distinct; an out-of-order build/feed can never form an illegal duplicate).
        if ability_id == GYOFU:
            state.combo_step = 1
            state.combo_target = 0
            state.combo_is_aoe = False
        elif ability_id == FUKO:
            state.combo_step = 1
            state.combo_target = 0
            state.combo_is_aoe = True
        elif ability_id == JINPU:
            state.combo_step = 2
            state.combo_target = _GETSU
        elif ability_id == SHIFU:
            state.combo_step = 2
            state.combo_target = _KA
        elif ability_id in (GEKKO, KASHA, YUKIKAZE):
            # A Meikyo free ender fires at a combo boundary; a normal 3rd/2nd step
            # is at combo_step 2/1 — only the former spends a Meikyo stack.
            was_meikyo = state.meikyo_stacks > 0 and state.combo_step == 0
            state.sen_mask |= {GEKKO: _GETSU, KASHA: _KA, YUKIKAZE: _SETSU}[ability_id]
            state.combo_step = 0
            state.combo_target = 0
            if was_meikyo:
                state.meikyo_stacks -= 1
        elif ability_id in (MANGETSU, OKA):
            # AoE enders: the Fuko-combo 2nd step (combo_step 1), or a Meikyo instant
            # ender (combo_step 0) — only the latter spends a Meikyo stack. Each grants
            # the same coverage buff as its ST sibling (Mangetsu->Fugetsu, Oka->Fuka),
            # so the Fugetsu coverage overlay stays valid on the AoE line.
            was_meikyo = state.meikyo_stacks > 0 and state.combo_step == 0
            state.sen_mask |= {MANGETSU: _GETSU, OKA: _KA}[ability_id]
            state.combo_step = 0
            state.combo_target = 0
            state.combo_is_aoe = False
            if was_meikyo:
                state.meikyo_stacks -= 1

        # Iaijutsu + Tsubame-Gaeshi follow-ups
        elif ability_id == MIDARE_SETSUGEKKA:
            state.sen_mask = 0
            state.kaeshi_setsugekka_ready = True
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == TENDO_SETSUGEKKA:
            state.sen_mask = 0
            state.tendo = False
            state.tendo_kaeshi_ready = True
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == HIGANBANA:
            state.sen_mask = 0
            state.higanbana_dot_end = t + sd.HIGANBANA_DOT_DURATION_S
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == KAESHI_SETSUGEKKA:
            state.kaeshi_setsugekka_ready = False
        elif ability_id == TENDO_KAESHI_SETSUGEKKA:
            state.tendo_kaeshi_ready = False
        # AoE Iaijutsu (2-Sen Goken) + their Tsubame-Gaeshi replays.
        elif ability_id == TENKA_GOKEN:
            state.sen_mask = 0
            state.kaeshi_goken_ready = True
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == TENDO_GOKEN:
            state.sen_mask = 0
            state.tendo = False
            state.tendo_kaeshi_goken_ready = True
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == KAESHI_GOKEN:
            state.kaeshi_goken_ready = False
        elif ability_id == TENDO_KAESHI_GOKEN:
            state.tendo_kaeshi_goken_ready = False

        # Ikishoten line
        elif ability_id == OGI_NAMIKIRI:
            state.ogi_ready = False
            state.kaeshi_namikiri_ready = True
            state.meditation = min(3, state.meditation + 1)
        elif ability_id == KAESHI_NAMIKIRI:
            state.kaeshi_namikiri_ready = False
        elif ability_id == IKISHOTEN:
            state.ogi_ready = True
            state.zanshin_ready = True
        elif ability_id == ZANSHIN:
            state.zanshin_ready = False

        # Meikyo / Shoha
        elif ability_id == MEIKYO_SHISUI:
            state.meikyo_stacks = 3
            state.tendo = True
        elif ability_id == SHOHA:
            state.meditation = max(0, state.meditation - 3)

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        """Bank resources through a boss-untargetable gap the way a real SAM does:
        channel Meditate (10 Kenki + 1 Meditation per 3s tick) for the window, and
        if a Meikyo charge is up, press it near the end so its 3 enders are ready
        the instant the boss reappears. Both are FREE here (no uptime GCDs lost),
        and they're real resources the player spends on the post-downtime burst —
        so omitting them would leave the ceiling below a top parse that does this.
        Resources only; the picker converts them to Shinten/Shoha/Tendo on resume."""
        window_s = win_end - win_start
        if window_s < sd.MEDITATE_TICK_S:
            return
        # Meditate (leave a ~1s tail for a pre-reappear Meikyo press), capped at
        # the most a top-10 ref actually Meditated here — Meditate needs you
        # stationary, so a movement-heavy gap yields little/none (cap 0 -> none).
        usable = max(0.0, window_s - 1.0)
        if self.meditate_cap_s is not None:
            usable = min(usable, self.meditate_cap_s)
        ticks = min(sd.MEDITATE_MAX_TICKS, int(usable / sd.MEDITATE_TICK_S))
        if ticks > 0:
            state.kenki = min(sd.KENKI_CAP,
                              state.kenki + ticks * sd.MEDITATE_KENKI_PER_TICK)
            state.meditation = min(3, state.meditation
                                   + ticks * sd.MEDITATE_MEDITATION_PER_TICK)
            state.timeline.append((win_start, sd.MEDITATE))
        # Pre-reappear Meikyo: stacks ready at the targetable edge (only when one
        # isn't already pending, mirroring the uptime gate).
        if state.charges.get(MEIKYO_SHISUI, 0) >= 1 and state.meikyo_stacks == 0 \
                and not state.tendo:
            press_t = max(win_start, win_end - 1.0)
            state.timeline.append((press_t, MEIKYO_SHISUI))
            apply_cooldown(state, self.cooldowns, MEIKYO_SHISUI)
            state.meikyo_stacks = 3
            state.tendo = True

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


def _default_bonus_kenki(duration_s: float) -> int:
    return int(duration_s / _DEFAULT_TENGENTSU_PERIOD_S) * sd.TENGENTSU_KENKI_PER_PROC


def _model_for(duration_s: float, sim_context) -> SamuraiRotationModel:
    """Build a model bound to this run's per-pull context. After unwrapping any
    per-player effective GCD (CeilingContext, faster-than-constant Skill Speed) then
    any `MultiTargetContext` (the AoE N(t) schedule), the payload is a `SamContext`
    (measured Tengentsu Kenki / entry gauge / Meditate cap); None for the warm-cache
    / Theorizer default; or a bare int (legacy / direct test calls) = bonus Kenki
    only."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    if payload is None:
        return SamuraiRotationModel(
            bonus_kenki=_default_bonus_kenki(duration_s), gcd_base_s=gcd,
            mt_schedule=mt_schedule)
    if isinstance(payload, SamContext):
        return SamuraiRotationModel(
            bonus_kenki=payload.bonus_kenki,
            entry_kenki=payload.entry_kenki,
            entry_meditation=payload.entry_meditation,
            meditate_cap_s=payload.meditate_cap_s,
            gcd_base_s=gcd, mt_schedule=mt_schedule)
    return SamuraiRotationModel(bonus_kenki=int(payload), gcd_base_s=gcd,
                                mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn `(timeline, aux, buff_intervals)` bound to a
    multi-target N(t) `schedule` (each cast valued per-target via
    `aoe_potency.potency_for`). Buff-aware when given. Empty schedule -> single
    target, byte-identical to the pre-AoE scorer. Lazy import to avoid a
    scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.samurai.scoring import score_delivered_potency
        return score_delivered_potency(
            timeline, buff_intervals=buff_intervals, target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests / canonical helpers call `_score`).
_score = _make_score()


# Admissible per-Sen credit used only in the beam PRUNE key: a held Sen will become
# ~1/3 of a Midare + its Kaeshi replay (both guaranteed crits). Crediting banked Sen
# keeps a line that SKIPS a Higanbana to build a Midare alive in the top-K until the
# Midare actually lands — without it the exact time-to-next DoT score (which rewards
# tighter refreshes) prunes the skip-line before its delayed Midare pays off.
_SEN_PRUNE_VALUE = (sd.POTENCIES[sd.MIDARE_SETSUGEKKA]
                    + sd.POTENCIES[sd.KAESHI_SETSUGEKKA]) * sd.GUARANTEED_CRIT_MULT / 3.0


# Beam width for the GCD-perfect search. With the DENSE `gcd_candidates` +
# `beam_signature` diversity-dedup, the width holds that many *distinct* lines, so a
# wider beam actually reaches the Sen-feeding / Higanbana-cadence optimum (it used to
# collapse into near-duplicates). 256 converges the long-fight ceiling above every
# real top parse — incl. the elite M12S-P2 pull that the old width-24 ceiling sat
# ~0.55% UNDER (see [sam-exact-solver-diagnostic]); the greedy guard in
# `beam_perfect` still means it never regresses below the refined ceiling.
_BEAM_WIDTH = 256


def _beam_best(model, score, fight_duration_s, downtime, buff_intervals):
    """The beam ceiling: the engine's burst-timing refinement + a beam search over
    the GCD forks (Higanbana-vs-Midare, and the AoE combo at N>=2) on top (guarded
    never to fall below the refined greedy ceiling). Kept as the exact solver's
    incumbent seed + the buff-alignment / fallback half of `_optimal_best`.
    `engine.beam_perfect` width 1 == `engine.perfect`."""
    return engine.beam_perfect(model, score, fight_duration_s, downtime,
                               buff_intervals, width=_BEAM_WIDTH)


# Wall-clock cap for the exact solve (guards a slow short-fight solve; the duration
# gate below keeps long fights off the DP entirely, so this rarely binds).
_DP_TIME_BOX_S = 30.0

# SAM's state space is large enough that the exact DP is interactive only on shorter
# fights. With the pot-aware incremental `exact_g`/`terminal_g` (the old fallback
# re-scanned the whole timeline per DP state once the opener pot dropped — 68% of a
# 240s solve), the bench (`scripts/bench_sam_dp.py`) is: 180s ~3.6s/budget,
# 240s ~14s, 300s ~49s (now PROVES, +609 over the beam, but past the 30s box). The
# diverse beam already holds the SAM ceiling at 0-over-100.5%, so the DP runs LIVE
# only up to this duration (short fights get the provable optimum); longer fights
# use the beam alone. The next raise needs a smaller frontier (tighter
# `admissible_remaining`, best-first ordering), not cheaper per-state work. The
# hermetic harness still exercises the DP for exactness at 50s.
_DP_MAX_DURATION_S = 240.0


@lru_cache(maxsize=64)
def _dp_throughput_cached(duration_key: float,
                          downtime_tuple: tuple[tuple[float, float], ...],
                          sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    """The provable buff-agnostic throughput optimum — the rank/strict ceiling. Cached
    (buff-independent), so the strict / observed / master scenarios share one solve and
    only their beam half differs. Seeds the B&B incumbent from the beam (a strong lower
    bound → tight pruning), solves each weave budget keeping the best, and guards >=
    the seed so a time-out can never fall below the beam."""
    downtime = list(downtime_tuple)
    model = _model_for(duration_key, sim_context)
    # The DP only runs on single-target pulls (`_optimal_best` routes N>=2 to the
    # beam, whose ceiling the DP's single-target admissible bound can't dominate), so
    # `model.mt_schedule` is empty here and `score` == the module `_score`.
    score = _make_score(model.mt_schedule)
    seed_tl, seed_aux = engine.beam_perfect(
        model, score, duration_key, downtime, None, width=_BEAM_WIDTH)
    best = (score(seed_tl, seed_aux, None), tuple(seed_tl), seed_aux)
    for mw in _SWEEP_MAX_WEAVES:
        params = SimParams(max_weaves_per_gcd=mw)
        tl, aux, _proven = optimal.solve_optimal(
            model, score, duration_key, downtime, params,
            buff_intervals=None, incumbent=best[0], time_box=_DP_TIME_BOX_S)
        s = score(tl, aux, None)
        if s > best[0]:
            best = (s, tuple(tl), aux)
    return best[1], best[2]


def _dp_throughput(fight_duration_s, downtime, sim_context):
    """Cached buff-agnostic exact optimum for this pull -> (timeline, aux)."""
    tl, aux = _dp_throughput_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        sim_context)
    return list(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The SAM ceiling. The diverse beam (buff-aware, raid-burst aligned) is the base —
    it already holds the ceiling at 0-over-100.5%. On shorter single-target fights
    (<= _DP_MAX_DURATION_S) the exact buff-agnostic DP also runs and, being a provable
    upper bound, max's against the beam; on long fights the DP is too slow to run
    interactively (gated) so the beam stands alone. Neither axis ever regresses.

    Under an AoE schedule (N>=2) the DP is skipped entirely: its `admissible_remaining`
    bound is single-target (per-cast ceilings from the ST potencies), so it would
    UNDER-estimate the remaining reward once AoE buttons scale with the target count —
    inadmissible, which would make the "provable" optimum silently wrong. The diverse
    beam is target-aware via `score` (the AoE forks live in `gcd_candidates`) and holds
    the multi-target ceiling on its own."""
    model = _model_for(fight_duration_s, sim_context)
    score = _make_score(model.mt_schedule)
    aoe = any(n >= 2 for _s, _e, n in model.mt_schedule)
    if aoe or fight_duration_s > _DP_MAX_DURATION_S or buff_intervals:
        beam_tl, beam_aux = engine.beam_perfect(
            model, score, fight_duration_s, downtime, buff_intervals, width=_BEAM_WIDTH)
        if aoe or fight_duration_s > _DP_MAX_DURATION_S:
            return beam_tl, beam_aux
        dp_tl, dp_aux = _dp_throughput(fight_duration_s, downtime, sim_context)
        if score(dp_tl, dp_aux, buff_intervals) >= score(beam_tl, beam_aux, buff_intervals):
            return dp_tl, dp_aux
        return beam_tl, beam_aux
    return _dp_throughput(fight_duration_s, downtime, sim_context)


# --- Module-level entrypoints (bind the model to the shared engine) --------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once. Returns (timeline, 0) — SAM has no pet/
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
    """The provably-optimal rotation (buff-aware when given)."""
    return _optimal_best(fight_duration_s, downtime_windows or [], buff_intervals,
                         sim_context)


def simulate_idealized_perfect(
        fight_duration_s: float,
        downtime_windows: list[tuple[float, float]] | None = None,
        buff_intervals: list[tuple[float, float, float]] | None = None,
        sim_context=None,
        ) -> tuple[list[tuple[float, int]], int]:
    """The GCD-perfect ceiling: the exact DP+B&B optimum guarded against the beam
    (the real upper bound — no greedy floor). Buff-aware when `buff_intervals` is
    given."""
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
