"""Idealized Ninja rotation — the NIN `RotationModel` for the shared engine.

The time loop, downtime/weave/charge handling, parameter sweep, local-search
refinement and canonical buff alignment all live in `jobs/_core/sim/engine.py`.
This module supplies only the NIN-specific rotation: the mudra->ninjutsu system,
the Ninki/Kazematoi gauges, the Kunai's Bane self-buff window, the Ten Chi Jin
ladder, Bunshin's shadow mirrors, and the Raiju chain. The four `simulate_*`
shims at the bottom bind this model to the engine (names kept so the sidecar /
scorer / tests call them unchanged).

NIN-specific structure encoded:

- **Mixed GCD speeds** (the Viper per-ability pattern, but with FIXED recasts):
  the Huton trait's permanent 15% haste puts the normal weaponskills at ~2.12s
  (`NIN_GCD_S`, scaled by per-player Skill Speed), while mudras (0.5s), ninjutsu
  (1.5s) and the Ten Chi Jin steps (~1.0s) are fixed-rate and do NOT scale.
- **The mudra system**: a ninjutsu is a forced GCD chain (mudras -> finisher)
  toward a goal (`_ninjutsu_goal`), consuming ONE shared charge (2 / 20s, keyed
  on TEN so the engine's generic multi-charge regen runs — including through
  downtime, which is the point). The charged-vs-free mudra id families from the
  live logs are reproduced (the first mudra of a paid sequence logs the charged
  id; Kassatsu sequences are free).
- **Downtime is NIN's signature axis**: charges regenerate while the boss is
  gone, the picker dumps Raitons *before* a window that would overcap the pool,
  and `on_downtime_window` pre-casts the mudras at the window edge so the
  ninjutsu itself is the first uptime GCD (what skilled Ninjas do with the
  charge-and-a-half a long downtime hands back).
- **Kunai's Bane** (+10% ninja-only, 15s/60s, needs Shadow Walker from Suiton):
  a windowed self-buff folded into the INCREMENTAL beam score (the DRG/GNB
  pattern) — the sim pays the real Suiton tax (580 < Raiton 740) to unlock it.
- **The 120s cycle**: Dokumori (+40 Ninki, Higi -> Zesho Meppo), Ten Chi Jin
  (the charge-free 500/740/580 ladder + Tenri Jindo 1100), TCJ's Suiton feeding
  Meisui (+50 Ninki, next spender +150), Kassatsu's free x1.30 Hyosho Ranryu
  held into the Kunai's Bane window.
- **Bunshin** (90s, 50 Ninki): 5 shadow mirrors (+160p and +5 Ninki per mirrored
  weaponskill) + Phantom Kamaitachi Ready (700p GCD, held into the KB window).

Multi-target (the `MultiTargetContext` N(t) schedule): the dedicated AoE line
swaps in DETERMINISTICALLY at audited VALUE crossovers (never raw per-target
potency — see the `_*_MIN_TARGETS` constants): Katon replaces the Raiton charge
dump at N>=3 (Raiton's value includes the banked Raiju), Goka Mekkyaku replaces
the Kassatsu Hyosho at N>=3, Death Blossom -> Hakke Mujinsatsu replaces a fresh
ST combo at N>=4, and the Ninki frogs (Hellfrog / Deathfrog) replace
Bhavacakra / Zesho at N>=2 (N>=3 against a Meisui-armed spender). Empty
schedule -> byte-identical single target.

Out of scope (documented, intentionally not modeled):
- Doton / Hollow Nozuchi (ground-target puddle uptime), the Huton
  Suiton-alternative, AoE TCJ steps, and Phantom Kamaitachi's pet-dealt splash
  (unmeasurable on the delivered side) — see data.AOE_POTENCIES' exclusions.
- Positional hit/miss (idealized always hits — the RPR convention); Raiju-Ready
  expiry (30s; both the greedy and every surviving beam line consume within a
  few GCDs).
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
from jobs.ninja import data as nd


# --- Ability IDs (aliased from data for readability) ------------------------
SPINNING_EDGE   = nd.SPINNING_EDGE
GUST_SLASH      = nd.GUST_SLASH
AEOLIAN_EDGE    = nd.AEOLIAN_EDGE
ARMOR_CRUSH     = nd.ARMOR_CRUSH
FLEETING_RAIJU  = nd.FLEETING_RAIJU
PHANTOM_KAMAITACHI = nd.PHANTOM_KAMAITACHI
TEN             = nd.TEN
TEN_FREE        = nd.TEN_FREE
CHI_FREE        = nd.CHI_FREE
JIN_FREE        = nd.JIN_FREE
RAITON          = nd.RAITON
SUITON          = nd.SUITON
HYOSHO_RANRYU   = nd.HYOSHO_RANRYU
TCJ_FUMA        = nd.TCJ_FUMA
TCJ_RAITON      = nd.TCJ_RAITON
TCJ_SUITON      = nd.TCJ_SUITON
KUNAIS_BANE     = nd.KUNAIS_BANE
DOKUMORI        = nd.DOKUMORI
KASSATSU        = nd.KASSATSU
TEN_CHI_JIN     = nd.TEN_CHI_JIN
TENRI_JINDO     = nd.TENRI_JINDO
MEISUI          = nd.MEISUI
BHAVACAKRA      = nd.BHAVACAKRA
ZESHO_MEPPO     = nd.ZESHO_MEPPO
BUNSHIN         = nd.BUNSHIN
DREAM           = nd.DREAM_WITHIN_A_DREAM
# AoE line (cast only in multi-target windows; gated on the audited crossovers).
KATON           = nd.KATON
GOKA_MEKKYAKU   = nd.GOKA_MEKKYAKU
DEATH_BLOSSOM   = nd.DEATH_BLOSSOM
HAKKE_MUJINSATSU = nd.HAKKE_MUJINSATSU
HELLFROG_MEDIUM = nd.HELLFROG_MEDIUM
DEATHFROG_MEDIUM = nd.DEATHFROG_MEDIUM


# --- Rotation tuning ---------------------------------------------------------
# The Huton-hasted standard GCD (2.5 x 0.85) at tier-typical Skill Speed — every
# weaponskill page's infobox reads 2.12s, and the live cast-gap histogram peaks
# at 2.10-2.15. Per-player Skill Speed threads in via `gcd_base_s` (only ever
# faster -> monotone-safe). The fixed-rate mudra / ninjutsu / TCJ recasts below
# do NOT scale with it.
NIN_GCD_S = 2.125

MUDRA_GCD_S    = 0.5      # fixed mudra recast
NINJUTSU_GCD_S = 1.5      # fixed ninjutsu recast
TCJ_STEP_GCD_S = 1.0      # measured inter-step cadence (the closer runs 1.5s)

# The measured pre-pull line: mudras during the countdown, Suiton landing at
# ~0.5s (the pull), the first melee GCD one ninjutsu-recast later (~2.0s).
OPENER_SUITON_T_S = 0.5
# Mudra charges at the pull: 2 - 1 (the pre-pull Suiton's charge, spent ~6s
# before the pull) + ~6s/20s regen.
OPENER_CHARGES = 1.3

# Picker thresholds.
SUITON_LEAD_S      = 6.0    # start Suiton when Kunai's Bane is ready within this
SUITON_HOLD_S      = 20.0   # bank the last charge when KB needs Suiton within this
CHARGE_DUMP_AT     = 1.9    # spend a Raiton before the pool caps
NINKI_POOL_MAX     = 85     # dump Bhavacakra outside KB before the next GCD caps
HYOSHO_KB_WAIT_S   = 25.0   # hold the Kassatsu Hyosho for KB unless it's this far
DOWNTIME_PRIME_MIN_S = 2.0  # min window length to pre-cast mudras at its edge
DOWNTIME_DUMP_LEAD_S = 8.0  # dump charges this close to a pool-capping window

# The forced GCD chain for each ninjutsu goal. The FIRST id's family encodes the
# charge: TEN (charged) opens a paid sequence, TEN_FREE opens Kassatsu's free one.
# (The specific mudra ids per finisher are cosmetic in this model — 0 potency,
# fixed 0.5s cadence — only the count and the charged/free family matter.)
_SEQ: dict[int, tuple[int, ...]] = {
    RAITON:        (TEN, CHI_FREE),
    SUITON:        (TEN, CHI_FREE, JIN_FREE),
    HYOSHO_RANRYU: (TEN_FREE, JIN_FREE),
    KATON:         (TEN, CHI_FREE),        # paid, 2 mudras (like Raiton)
    GOKA_MEKKYAKU: (TEN_FREE, JIN_FREE),   # Kassatsu-only (free, like Hyosho)
}
_TCJ_STEPS: tuple[int, ...] = (TCJ_FUMA, TCJ_RAITON, TCJ_SUITON)

# --- AoE crossovers (VALUE crossovers, audited — never raw per-target potency) --
# Katon vs Raiton: a Raiton line is worth ~740 + the Raiju it banks (a later
#   700p Fleeting Raiju displacing a ~450p combo GCD ≈ +250) ≈ ~990; Katon is
#   350×n -> flips at n>=3 (1050 > 990). (Bunshin mirrors ride the displaced
#   filler on both lines — they cancel.)
_KATON_MIN_TARGETS = 3
# Goka vs Hyosho (both consume the same Kassatsu x1.30): 850×n vs 1300 flips
#   arithmetically at n=2 by a hairline (+13p x1.3); gated at 3 with the rest of
#   the line so the N=2 ceiling stays the proven ST rotation.
_GOKA_MIN_TARGETS = 3
# Death Blossom -> Hakke Mujinsatsu vs the ST combo: AoE ~110×n per GCD at +5
#   Ninki; ST ~410 per GCD + the richer finisher Ninki (+3.3/GCD ≈ +27p at
#   8p/unit) + Kazematoi economy ≈ ~437 -> flips at n>=4 (440). (Bunshin
#   mirrors fire on both combos — they cancel.)
_AOE_COMBO_MIN_TARGETS = 4
# Ninki spenders (each 50 Ninki buys ONE spender): Deathfrog 400×n beats Zesho
#   700 at n>=2 (800); Hellfrog 250×n beats Bhavacakra 400 at n>=2 (500) but a
#   Meisui-armed Bhavacakra 550 only at n>=3 (750). Encoded in pick_ogcd.
_FROG_MIN_TARGETS = 2

# Per-ability fixed GCD slot durations (everything else runs at gcd_base_s).
_FIXED_DUR: dict[int, float] = {
    **{m: MUDRA_GCD_S for m in nd.MUDRA_IDS},
    **{j: NINJUTSU_GCD_S for j in nd.NINJUTSU_IDS},
    TCJ_FUMA: TCJ_STEP_GCD_S, TCJ_RAITON: TCJ_STEP_GCD_S,
    TCJ_SUITON: NINJUTSU_GCD_S,
}
# Slots too short (or too fragile — mid-TCJ) to weave into. Ninjutsu (1.5s) and
# the TCJ closer take ONE weave (measured: Kassatsu after the opener Suiton,
# Meisui after TCJ's Suiton); mudras and the mid-TCJ steps take none.
_WEAVE_BUDGET: dict[int, int] = {
    **{m: 0 for m in nd.MUDRA_IDS},
    **{j: 1 for j in nd.NINJUTSU_IDS},
    TCJ_FUMA: 0, TCJ_RAITON: 0, TCJ_SUITON: 1,
}

# Hold the burst enablers into raid-buff windows (refine / canonical alignment).
_ANCHORS: tuple[int, ...] = (KUNAIS_BANE, DOKUMORI, TEN_CHI_JIN, KASSATSU)
_SWEEP_MAX_WEAVES: tuple[int, ...] = (2, 3)
_BEAM_WIDTH = 256
_SIG_BUCKET_S = NIN_GCD_S   # decision-state timer bucket (~one GCD)

# The tincture the sim places in-rotation (engine `_maybe_pot`, scored at cast
# time). Derived from JobData — same value the scorer's spec uses.
_TINCTURE_SPEC = spec_for_job(
    nd.JOB_DATA.tincture_main_stat, nd.JOB_DATA.tincture_role_coeff)

# Prune-credit rates for banked resources (admissible-ish steering only — the
# final beam selection always re-scores with the exact score_fn).
_PRUNE_NINKI_P   = nd.NINKI_VALUE_P_PER_UNIT     # ~a Bhavacakra per 50
_PRUNE_KAZ_P     = nd.KAZEMATOI_VALUE_P_PER_UNIT
_PRUNE_RAIJU_P   = 400.0    # a pending 700p Raiju over the ~300p filler it displaces
_PRUNE_CHARGE_P  = 400.0    # a banked charge -> a Raiton line's net value
_PRUNE_MIRROR_P  = float(nd.BUNSHIN_MIRROR_P)


@dataclass(frozen=True)
class SimParams(SimParamsBase):
    """NIN picker tunables — only the shared knobs (max_weaves / forbidden_windows)."""
    pass


@dataclass
class SimState(SimStateBase):
    # Gauges (GaugeModel.name == field convention, for the shared entry-gauge seed).
    ninki: int = 0
    kazematoi: int = 0
    # Melee combo: 0 starter, 1 -> Gust Slash, 2 -> finisher.
    combo_step: int = 0
    # The in-progress combo was started by Death Blossom (AoE): its 2nd step is
    # Hakke Mujinsatsu (a 2-step combo), not Gust Slash.
    combo_is_aoe: bool = False
    # Mudra sequence in progress: the goal ninjutsu + how many mudras are down.
    mudra_goal: int = 0          # 0 = idle; else RAITON / SUITON / HYOSHO_RANRYU
    mudra_done: int = 0
    # Ten Chi Jin ladder: -1 idle, 0-2 -> next _TCJ_STEPS index.
    tcj_step: int = -1
    # Procs / windows (absolute end-times; a flag where the window never binds).
    raiju: int = 0               # Raiju Ready stacks (0-3)
    sw_end: float = float("-inf")        # Shadow Walker
    kb_end: float = float("-inf")        # Kunai's Bane +10% window
    higi_end: float = float("-inf")      # Higi (Dokumori) -> Zesho Meppo
    kassatsu_armed: bool = False
    meisui_armed: bool = False           # next Bhavacakra / Zesho +150
    tenri_ready: bool = False
    bunshin: int = 0             # shadow mirror stacks (0-5)
    pk_until: float = float("-inf")      # Phantom Kamaitachi Ready expiry
    # Incremental (raid-buff-agnostic, self-buff-AWARE) running score for the O(1)
    # beam-prune key — the exact per-cast math `score_delivered_potency` runs with
    # raid buffs / tincture off.
    _score_flat: float = 0.0


class NinjaRotationModel(engine.BaseRotationModel):
    cooldowns = nd.COOLDOWNS
    timing = InstantGCD(base_s=NIN_GCD_S)
    agnostic_anchors = _ANCHORS
    buff_anchors = _ANCHORS
    canonical_anchors = _ANCHORS
    tincture_spec = _TINCTURE_SPEC

    def __init__(self, gcd_base_s: float | None = None,
                 entry: EntryState | None = None,
                 mt_schedule: tuple[tuple[float, float, int], ...] = ()):
        # Per-player Skill Speed: scales the normal weaponskills only (the fixed
        # mudra/ninjutsu/TCJ recasts are speed-immune). None keeps the constant,
        # byte-identical.
        self.gcd_base_s = NIN_GCD_S if gcd_base_s is None else gcd_base_s
        if self.gcd_base_s != NIN_GCD_S:
            self.timing = InstantGCD(base_s=self.gcd_base_s)
        # Phase-continuation entry state (M12S-P2): carried Ninki / Kazematoi +
        # an earlier opener start. None -> cold start, byte-identical.
        self.entry = entry
        # The multi-target N(t) schedule (MultiTargetContext) — drives the
        # deterministic AoE swaps at the audited crossovers above. Empty () ->
        # single target, byte-identical.
        self.mt_schedule = mt_schedule

    def _n(self, t: float) -> int:
        """Live target count at `t` from the multi-target schedule (1 if none)."""
        return n_at(t, self.mt_schedule)

    # --- Lifecycle -----------------------------------------------------------

    def init_state(self) -> SimState:
        state = SimState()
        state.charges = {TEN: 2.0}
        state.cd_ready = {KUNAIS_BANE: 0.0, KASSATSU: 0.0, DREAM: 0.0,
                          BUNSHIN: 0.0, DOKUMORI: 0.0, TEN_CHI_JIN: 0.0,
                          MEISUI: 0.0}
        return state

    def prepull(self, state: SimState, params) -> None:
        # The measured opener: mudras during the countdown (cosmetic pre-zone
        # casts), Suiton landing at ~0.5s as the pull (ranged, so no melee run-in
        # is exposed — the 1.5s ninjutsu recast covers the walk to the boss).
        if self.entry is not None:
            seed_entry_gauge(state, self.entry.gauge_map, nd.JOB_DATA.gauges)
        start = OPENER_SUITON_T_S
        if self.entry is not None and self.entry.opener_start_s is not None:
            start = min(start, self.entry.opener_start_s)
        state.timeline.append((start - 1.5, TEN))
        state.timeline.append((start - 1.0, CHI_FREE))
        state.timeline.append((start - 0.5, JIN_FREE))
        state.timeline.append((start, SUITON))
        state._score_flat += nd.POTENCIES[SUITON]
        state.sw_end = start + nd.SHADOW_WALKER_DURATION_S
        state.charges[TEN] = OPENER_CHARGES
        state.last_gcd_t = start
        state.t = start + NINJUTSU_GCD_S

    # --- GCD timing ----------------------------------------------------------

    def gcd_duration(self, state: SimState, gcd_id: int, params) -> float:
        # Fixed-rate mudra/ninjutsu/TCJ slots; everything else at the (possibly
        # per-player) hasted weaponskill GCD.
        return _FIXED_DUR.get(gcd_id, self.gcd_base_s)

    def weave_budget(self, state: SimState, gcd_id: int, params) -> int:
        cap = _WEAVE_BUDGET.get(gcd_id)
        if cap is not None:
            return cap
        return params.max_weaves_per_gcd

    # --- GCD selection -------------------------------------------------------

    def _kb_ready_in(self, state: SimState) -> float:
        return state.cd_ready.get(KUNAIS_BANE, 0.0) - state.t

    def _next_downtime(self, state: SimState) -> tuple[float, float] | None:
        t = state.t
        best = None
        for s, e in state.downtime_windows:
            if e > t and (best is None or s < best[0]):
                best = (max(s, t), e)
        return best

    def _charged_aoe_or_raiton(self, state: SimState) -> int:
        """The paid 2-mudra finisher for this state's target count: Katon at the
        audited crossover, else Raiton. One rule, so the picker, `apply_cast`'s
        first-mudra default, and the downtime priming always agree."""
        return (KATON if self._n(state.t) >= _KATON_MIN_TARGETS else RAITON)

    def _ninjutsu_goal(self, state: SimState) -> int:
        """The ninjutsu this state should be building toward, or 0 for none.
        Deterministic in the state (no params) so `pick_gcd` (pure) and
        `apply_cast` (the first-mudra transition) derive the same goal."""
        t = state.t
        remaining = state.fight_duration_s - t
        # Kassatsu's free ninjutsu — held into the Kunai's Bane window (the x1.30
        # on 1300 is the single biggest hit), released early only when KB is far
        # off (drifted) or the fight is ending. At the AoE crossover the free
        # cast is Goka Mekkyaku (850×n) instead of Hyosho Ranryu (1300).
        if state.kassatsu_armed:
            if (state.kb_end > t or self._kb_ready_in(state) > HYOSHO_KB_WAIT_S
                    or remaining < 15.0):
                return (GOKA_MEKKYAKU if self._n(t) >= _GOKA_MIN_TARGETS
                        else HYOSHO_RANRYU)
            return 0
        charges = state.charges.get(TEN, 0.0)
        if charges < 1.0:
            return 0
        # Suiton — the Shadow Walker feed for an imminent Kunai's Bane.
        if (state.sw_end <= t and self._kb_ready_in(state) <= SUITON_LEAD_S
                and remaining > 5.0):
            return SUITON
        dump = self._charged_aoe_or_raiton(state)
        # Raiton/Katon — dump before the pool caps.
        if charges >= CHARGE_DUMP_AT:
            return dump
        # Raiton/Katon — dump ahead of a downtime window whose regen would cap
        # the pool.
        nxt = self._next_downtime(state)
        if nxt is not None and nxt[0] - t <= DOWNTIME_DUMP_LEAD_S:
            at_end = charges + (nxt[1] - t) / nd.COOLDOWNS[TEN][0]
            if at_end > 2.0:
                return dump
        # Raiton/Katon — spend freely while the last charge isn't banked for a
        # Suiton.
        kb_in = self._kb_ready_in(state)
        needs_bank = (state.sw_end <= t and kb_in <= SUITON_HOLD_S
                      and remaining > kb_in + 2.0)
        if not needs_bank:
            return dump
        return 0

    def _non_ninjutsu_pick(self, state: SimState) -> int:
        """The best non-mudra GCD: Phantom Kamaitachi / the Raiju chain / combo.
        At the AoE-combo crossover the fresh combo swaps to Death Blossom ->
        Hakke Mujinsatsu (a combo mid-flight is finished on its own line —
        breaking it forfeits the finisher progress)."""
        t = state.t
        if state.pk_until > t and (state.kb_end > t
                                   or self._kb_ready_in(state) > state.pk_until - t):
            return PHANTOM_KAMAITACHI
        if state.raiju > 0:
            return FLEETING_RAIJU
        if state.combo_step == 1 and state.combo_is_aoe:
            return HAKKE_MUJINSATSU
        if state.combo_step == 0:
            return (DEATH_BLOSSOM
                    if self._n(t) >= _AOE_COMBO_MIN_TARGETS else SPINNING_EDGE)
        if state.combo_step == 1:
            return GUST_SLASH
        return AEOLIAN_EDGE if state.kazematoi >= 1 else ARMOR_CRUSH

    def pick_gcd(self, state: SimState, params) -> int:
        # Forced chains first: the TCJ ladder, then an in-progress mudra sequence.
        if state.tcj_step >= 0:
            return _TCJ_STEPS[state.tcj_step]
        if state.mudra_goal:
            seq = _SEQ[state.mudra_goal]
            if state.mudra_done < len(seq):
                return seq[state.mudra_done]
            return state.mudra_goal
        goal = self._ninjutsu_goal(state)
        if goal:
            return _SEQ[goal][0]
        return self._non_ninjutsu_pick(state)

    def gcd_candidates(self, state: SimState, params) -> list[int]:
        """The beam's fork set. Forced chains are single moves; elsewhere the
        forks are (a) the finisher choice (Aeolian's Kazematoi spend vs Armor
        Crush's build — the burst-banking axis) and (b) Raiton timing (dump a
        charge now vs keep the melee GCD rolling). A Suiton goal is NOT forked
        away — Kunai's Bane depends on it."""
        if state.tcj_step >= 0 or state.mudra_goal:
            return [self.pick_gcd(state, params)]
        base = self.pick_gcd(state, params)
        out = [base]
        goal = self._ninjutsu_goal(state)
        if goal in (SUITON, HYOSHO_RANRYU, GOKA_MEKKYAKU):
            return out   # KB's Shadow Walker feed / the held Kassatsu cast
        # Finisher fork.
        if state.combo_step == 2 and base in (AEOLIAN_EDGE, ARMOR_CRUSH):
            other = ARMOR_CRUSH if base == AEOLIAN_EDGE else AEOLIAN_EDGE
            out.append(other)
        # Raiton/Katon-timing fork (dump a charge now vs keep the GCD rolling).
        if goal in (RAITON, KATON) and base == TEN:
            alt = self._non_ninjutsu_pick(state)
            if alt not in out:
                out.append(alt)
        elif (not goal and state.charges.get(TEN, 0.0) >= 1.0
                and not state.kassatsu_armed
                and state.fight_duration_s - state.t > 5.0):
            out.append(TEN)
        return out

    # --- oGCD selection ------------------------------------------------------

    def pick_ogcd(self, state: SimState, params):
        t = state.t
        fw = params.forbidden_windows
        # Kunai's Bane — the +10% window everything keys off; fired FIRST so the
        # payload (TCJ / Hyosho / the Ninki dumps) lands inside it (the DRG
        # Lance-Charge lesson: in the STRICT scenario pick order alone sets
        # self-buff alignment).
        if (state.cd_ready.get(KUNAIS_BANE, 0.0) <= t and state.sw_end > t
                and not is_forbidden(KUNAIS_BANE, t, fw)):
            return KUNAIS_BANE
        # Dokumori — the party buff + Higi + 40 Ninki, on cooldown at burst open.
        if state.cd_ready.get(DOKUMORI, 0.0) <= t \
                and not is_forbidden(DOKUMORI, t, fw):
            return DOKUMORI
        # Tenri Jindo — 1100p follow-up once the TCJ ladder is done.
        if state.tenri_ready and state.tcj_step < 0:
            return TENRI_JINDO
        # Ten Chi Jin — inside the KB window (120s vs KB's 60s: always alignable,
        # so waiting costs no drift), with a fight-end escape (an unbuffed ladder
        # + Tenri Jindo still beats never casting them); never while a mudra
        # sequence is mid-flight.
        if (state.cd_ready.get(TEN_CHI_JIN, 0.0) <= t and state.tcj_step < 0
                and not state.mudra_goal
                and (state.kb_end > t or state.fight_duration_s - t < 20.0)
                and not is_forbidden(TEN_CHI_JIN, t, fw)):
            return TEN_CHI_JIN
        # Kassatsu — arms the free x1.30 Hyosho (the goal rule holds it for KB).
        if state.cd_ready.get(KASSATSU, 0.0) <= t and not state.kassatsu_armed \
                and not is_forbidden(KASSATSU, t, fw):
            return KASSATSU
        # Bunshin — 5 shadow mirrors + Phantom Kamaitachi Ready.
        if (state.cd_ready.get(BUNSHIN, 0.0) <= t and state.ninki >= 50
                and not is_forbidden(BUNSHIN, t, fw)):
            return BUNSHIN
        # Dream Within a Dream — 60s, rides the KB cadence.
        if state.cd_ready.get(DREAM, 0.0) <= t \
                and not is_forbidden(DREAM, t, fw):
            return DREAM
        # Meisui — spends a Shadow Walker KB doesn't need (KB comfortably far).
        if (state.cd_ready.get(MEISUI, 0.0) <= t and state.sw_end > t
                and self._kb_ready_in(state) > 15.0
                and not is_forbidden(MEISUI, t, fw)):
            return MEISUI
        # Ninki spenders: Zesho Meppo under Higi, else Bhavacakra — dumped inside
        # the KB window, pooled (to the overcap edge) outside it. At the audited
        # frog crossovers the AoE spender wins the same 50 Ninki: Deathfrog
        # 400×n > Zesho 700 at n>=2; Hellfrog 250×n > Bhavacakra 400 at n>=2 but
        # only > a Meisui-armed 550 at n>=3 (the +150 rides Bhavacakra/Zesho
        # only, and the frogs don't consume it).
        if state.ninki >= 50:
            dump = (state.kb_end > t or state.ninki >= NINKI_POOL_MAX
                    or state.fight_duration_s - t < 10.0)
            if dump:
                n = self._n(t)
                if state.higi_end > t:
                    return DEATHFROG_MEDIUM if n >= _FROG_MIN_TARGETS else ZESHO_MEPPO
                if n >= (_FROG_MIN_TARGETS + 1 if state.meisui_armed
                         else _FROG_MIN_TARGETS):
                    return HELLFROG_MEDIUM
                return BHAVACAKRA
        return None

    # --- Cast transitions ----------------------------------------------------

    def apply_cast(self, state: SimState, ability_id: int) -> None:
        t = state.t
        state.timeline.append((t, ability_id))

        # Incremental score (mirrors scoring.score_delivered_potency exactly,
        # raid buffs / tincture off): per-target table potency (`potency_for`
        # scales the AoE line by the live target count; == POTENCIES.get at
        # N==1, byte-identical) + the state-derived bonuses, x Kunai's Bane,
        # x Kassatsu on the consuming ninjutsu.
        base = potency_for(ability_id, self._n(t), nd.JOB_DATA)
        if ability_id == AEOLIAN_EDGE and state.kazematoi >= 1:
            base += nd.AEOLIAN_KAZEMATOI_BONUS_P
        elif ability_id in (BHAVACAKRA, ZESHO_MEPPO) and state.meisui_armed:
            base += nd.MEISUI_BONUS_P
        if base > 0:
            m = nd.KUNAIS_BANE_MULT if state.kb_end > t else 1.0
            if ability_id in nd.NINJUTSU_IDS and state.kassatsu_armed:
                m *= nd.KASSATSU_MULT
            state._score_flat += base * m
        # Bunshin shadow mirror (+160p, +5 Ninki per mirrored weaponskill).
        if state.bunshin > 0 and ability_id in nd.BUNSHIN_MIRRORED_IDS:
            m = nd.KUNAIS_BANE_MULT if state.kb_end > t else 1.0
            state._score_flat += nd.BUNSHIN_MIRROR_P * m
            state.bunshin -= 1
            state.ninki = min(nd.NINKI_CAP, state.ninki + 5)

        # Gauges (generators clamp at the cap; spenders floor at 0).
        gain = nd.NINKI_GENERATORS.get(ability_id)
        if gain:
            state.ninki = min(nd.NINKI_CAP, state.ninki + gain)
        spend = nd.NINKI_SPENDERS.get(ability_id)
        if spend:
            state.ninki = max(0, state.ninki - spend)

        # Mudra sequence bookkeeping. The first mudra of a paid sequence (the
        # charged TEN id) spends the shared charge; Kassatsu's free family
        # doesn't. The goal is derived here with the same pure rule the picker
        # used (identical state), defaulting to the target-count-appropriate
        # charge dump (Raiton / Katon) for a beam-forked start.
        if ability_id in nd.MUDRA_IDS:
            if not state.mudra_goal:
                state.mudra_goal = (self._ninjutsu_goal(state)
                                    or self._charged_aoe_or_raiton(state))
                if ability_id == TEN:
                    state.charges[TEN] = max(0.0, state.charges.get(TEN, 0.0) - 1.0)
            state.mudra_done += 1
            return

        if ability_id in nd.NINJUTSU_IDS:
            state.mudra_goal = 0
            state.mudra_done = 0
            if state.kassatsu_armed:
                state.kassatsu_armed = False       # consumed (after scoring)
            if ability_id == RAITON:
                state.raiju = min(nd.RAIJU_READY_MAX, state.raiju + 1)
            elif ability_id in (SUITON, nd.HUTON_NINJUTSU):
                state.sw_end = t + nd.SHADOW_WALKER_DURATION_S
            return

        # Ten Chi Jin ladder steps.
        if ability_id == TCJ_FUMA:
            state.tcj_step = 1
            return
        if ability_id == TCJ_RAITON:
            state.tcj_step = 2
            state.raiju = min(nd.RAIJU_READY_MAX, state.raiju + 1)
            return
        if ability_id == TCJ_SUITON:
            state.tcj_step = -1
            state.sw_end = t + nd.SHADOW_WALKER_DURATION_S
            return

        # Melee combo progression (ST 3-step; the AoE Death Blossom -> Hakke
        # Mujinsatsu is a 2-step chain sharing the combo state).
        if ability_id == SPINNING_EDGE:
            state.combo_step = 1
            state.combo_is_aoe = False
        elif ability_id == DEATH_BLOSSOM:
            state.combo_step = 1
            state.combo_is_aoe = True
        elif ability_id == GUST_SLASH:
            state.combo_step = 2
        elif ability_id == HAKKE_MUJINSATSU:
            state.combo_step = 0
            state.combo_is_aoe = False
        elif ability_id == AEOLIAN_EDGE:
            state.combo_step = 0
            if state.kazematoi >= 1:
                state.kazematoi -= 1
        elif ability_id == ARMOR_CRUSH:
            state.combo_step = 0
            state.kazematoi = min(nd.KAZEMATOI_CAP, state.kazematoi + 2)
        elif ability_id == FLEETING_RAIJU:
            state.raiju = max(0, state.raiju - 1)
        elif ability_id == PHANTOM_KAMAITACHI:
            state.pk_until = float("-inf")

        # oGCD effects (window opens AFTER scoring the granting cast).
        elif ability_id == KUNAIS_BANE:
            state.sw_end = float("-inf")
            state.kb_end = t + nd.KUNAIS_BANE_DURATION_S
        elif ability_id == DOKUMORI:
            state.higi_end = t + nd.HIGI_DURATION_S
        elif ability_id == KASSATSU:
            state.kassatsu_armed = True
        elif ability_id == TEN_CHI_JIN:
            state.tcj_step = 0
            state.tenri_ready = True
        elif ability_id == TENRI_JINDO:
            state.tenri_ready = False
        elif ability_id == MEISUI:
            state.sw_end = float("-inf")
            state.meisui_armed = True
        elif ability_id in (BHAVACAKRA, ZESHO_MEPPO):
            if state.meisui_armed:
                state.meisui_armed = False
            if ability_id == ZESHO_MEPPO:
                state.higi_end = float("-inf")     # Higi consumed
        elif ability_id == DEATHFROG_MEDIUM:
            state.higi_end = float("-inf")         # Higi consumed (frogs skip Meisui)
        elif ability_id == BUNSHIN:
            state.bunshin = nd.BUNSHIN_STACKS
            state.pk_until = t + 45.0

        apply_cooldown(state, self.cooldowns, ability_id)

    # --- Downtime ------------------------------------------------------------

    def on_downtime_window(self, state: SimState,
                           win_start: float, win_end: float) -> None:
        """NIN's signature downtime move: pre-cast the mudras at the window edge
        (they need no target) so the ninjutsu itself is the first uptime GCD.
        Charges regenerate through the window via the engine's generic regen (the
        pool is keyed on TEN in `cooldowns`); one is pre-deducted here for the
        primed sequence. Suiton is primed when Kunai's Bane wants a Shadow Walker
        at re-engage, else Raiton."""
        t = state.t
        if (win_end - t < DOWNTIME_PRIME_MIN_S or state.mudra_goal
                or state.tcj_step >= 0
                or win_end > state.fight_duration_s - 2.0):
            return
        charges_at_end = min(2.0, state.charges.get(TEN, 0.0)
                             + (win_end - t) / nd.COOLDOWNS[TEN][0])
        if charges_at_end < 1.0:
            return
        kb_in_at_end = state.cd_ready.get(KUNAIS_BANE, 0.0) - win_end
        if state.sw_end <= win_end and kb_in_at_end <= SUITON_LEAD_S:
            goal = SUITON
        elif n_at(win_end, self.mt_schedule) >= _KATON_MIN_TARGETS:
            goal = KATON     # re-engaging into a multi-target window
        else:
            goal = RAITON
        seq = _SEQ[goal]
        for i, mid in enumerate(seq):
            state.timeline.append(
                (win_end - MUDRA_GCD_S * (len(seq) - i), mid))
        state.charges[TEN] = max(0.0, state.charges.get(TEN, 0.0) - 1.0)
        state.mudra_goal = goal
        state.mudra_done = len(seq)

    # --- Beam search seam ----------------------------------------------------

    def beam_prune(self, state: SimState, score_fn, buff_intervals) -> float:
        """O(1) top-K ranking from the incremental self-buff-aware running score
        plus admissible-ish credits for banked resources (Ninki, Kazematoi, Raiju
        stacks, mudra charges, remaining Bunshin mirrors), so an investing line
        isn't pruned before it pays off. The final selection always re-scores
        with the exact score_fn."""
        return (state._score_flat
                + state.ninki * _PRUNE_NINKI_P
                + state.kazematoi * _PRUNE_KAZ_P
                + state.raiju * _PRUNE_RAIJU_P
                + state.charges.get(TEN, 0.0) * _PRUNE_CHARGE_P
                + state.bunshin * _PRUNE_MIRROR_P)

    def beam_signature(self, state: SimState):
        """Bucketed decision-state key (the GNB lesson: a lossless timer key
        fragments the beam into sub-GCD near-duplicates and starves the effective
        width). Gauges, combo/sequence steps and proc flags are exact; window and
        cooldown timers are bucketed to ~one GCD. `state.t` is bucketed too — NIN
        beams do NOT advance in lockstep (a mudra fork advances 0.5s while a
        combo fork advances 2.12s)."""
        t = state.t

        def slot(remaining: float) -> int:
            return int(max(0.0, remaining) / _SIG_BUCKET_S)

        return (
            int(t / _SIG_BUCKET_S),
            state.combo_step, state.combo_is_aoe, state.kazematoi, state.ninki,
            state.mudra_goal, state.mudra_done, state.tcj_step,
            state.raiju, state.kassatsu_armed, state.meisui_armed,
            state.tenri_ready, state.bunshin,
            slot(state.sw_end - t), slot(state.kb_end - t),
            slot(state.higi_end - t), slot(state.pk_until - t),
            int(state.charges.get(TEN, 0.0) * 4),
            slot(state.cd_ready.get(KUNAIS_BANE, 0.0) - t),
            slot(state.cd_ready.get(KASSATSU, 0.0) - t),
            slot(state.cd_ready.get(DREAM, 0.0) - t),
            slot(state.cd_ready.get(BUNSHIN, 0.0) - t),
            slot(state.cd_ready.get(DOKUMORI, 0.0) - t),
            slot(state.cd_ready.get(TEN_CHI_JIN, 0.0) - t),
            slot(state.cd_ready.get(MEISUI, 0.0) - t),
        )

    def sweep_params(self, extra_forbidden):
        for mw in _SWEEP_MAX_WEAVES:
            yield SimParams(max_weaves_per_gcd=mw, forbidden_windows=extra_forbidden)


# --- Model construction + engine binding ------------------------------------

def _model_for(sim_context) -> NinjaRotationModel:
    """Build the model bound to this run's per-pull context: a per-player
    effective GCD (CeilingContext), the multi-target N(t) schedule
    (MultiTargetContext -> the AoE-line swaps), and/or a phase-continuation
    `EntryState` (carried Ninki/Kazematoi + opener start). `None` -> the default
    model, byte-identical."""
    from jobs._core.downtime_sources import MultiTargetContext
    from jobs._core.gcd_speed import unwrap_ceiling_context
    gcd, payload = unwrap_ceiling_context(sim_context)
    mt_schedule: tuple[tuple[float, float, int], ...] = ()
    if isinstance(payload, MultiTargetContext):
        mt_schedule = payload.schedule
        payload = payload.inner
    entry = payload if isinstance(payload, EntryState) else None
    return NinjaRotationModel(gcd_base_s=gcd, entry=entry,
                              mt_schedule=mt_schedule)


def _make_score(schedule: tuple[tuple[float, float, int], ...] = ()):
    """Build the engine-facing score_fn bound to a multi-target N(t) `schedule`
    (each cast valued per-target via `aoe_potency.potency_for`). Empty schedule
    -> single target, byte-identical to the pre-AoE scorer. Lazy import to
    avoid a scoring<->simulator cycle at module load."""
    target_fn = schedule_target_fn(schedule)

    def _score(timeline, aux, buff_intervals):
        from jobs.ninja.scoring import score_delivered_potency
        return score_delivered_potency(timeline, buff_intervals=buff_intervals,
                                       target_fn=target_fn)
    return _score


# Module-level no-schedule scorer (back-compat: tests / canonical helpers).
_score = _make_score()


@lru_cache(maxsize=64)
def _perfect_cached(duration_key: float,
                    downtime_tuple: tuple[tuple[float, float], ...],
                    buff_tuple: tuple[tuple[float, float, float], ...] | None,
                    sim_context) -> tuple[tuple[tuple[float, int], ...], int]:
    model = _model_for(sim_context)
    buff_intervals = list(buff_tuple) if buff_tuple else None
    tl, aux = engine.beam_perfect(
        model, _make_score(model.mt_schedule), duration_key,
        list(downtime_tuple), buff_intervals, width=_BEAM_WIDTH)
    return tuple(tl), aux


def _optimal_best(fight_duration_s, downtime, buff_intervals, sim_context):
    """The NIN ceiling: the diverse beam over the finisher / Raiton-timing forks
    on top of the burst-timing refinement. (Beam-only, like DRG/RPR — the exact
    DP seam is deferred; the mudra chains make the action space deep but the
    genuinely strategic forks are few.)"""
    tl, aux = _perfect_cached(
        round(fight_duration_s, 3),
        tuple((round(s, 3), round(e, 3)) for s, e in (downtime or [])),
        tuple((round(s, 3), round(e, 3), round(m, 4))
              for s, e, m in buff_intervals) if buff_intervals else None,
        sim_context)
    return list(tl), aux


# --- Module-level entrypoints (bind the model to the shared engine) ----------

def simulate_idealized(fight_duration_s: float,
                       downtime_windows: list[tuple[float, float]] | None = None,
                       params: SimParams | None = None,
                       sim_context=None,
                       ) -> tuple[list[tuple[float, int]], int]:
    """Run the idealized rotation once (greedy baseline). Returns (timeline, 0)
    — NIN has no pet/payload scalar, so aux is always 0."""
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
    """Alias for the optimal ceiling — the beam over the GCD forks on top of the
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
    model = _model_for(sim_context)
    return engine.canonical_aligned(model, _make_score(model.mt_schedule),
                                    fight_duration_s, downtime_windows or [],
                                    buff_intervals)
