"""Dancer data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for DNC numbers: potencies, cooldowns, the opener, and the
buff/self-buff machinery. DNC is the analyzer's first **physical-ranged proc job**
— it combines:

  * the MCH archetype (all-instant GCDs, `PHYSICAL_RANGED` downtime policy), and
  * the RDM proc economy (RNG-gated GCDs the player can't summon on demand), and
  * three self-buff patterns at once — Standard Finish (a maintained ~5% self-buff,
    the WAR Surging Tempest analog), Technical Finish (~5%, a 20s window) and
    Devilment (a crit/DH burst window, the PLD Fight-or-Flight analog).

**Resource model — everything RNG/external is a budget, not a gauge.** DNC's two
gauges are not deterministic from the player's own cast stream:
  * **Esprit** is fed partly by the *party* (invisible to the player's casts), and
  * **Feathers** are granted at ~50% off the proc spells (RNG).
Modeling either as a `GaugeModel` would over-count generation and emit false
overcap. So — exactly like RDM's proc budget — the ceiling spends *the same count*
the player did: the Scoring aspect measures the player's Reverse Cascade/Fountainfall
(procs), Fan Dance (feathers) and Saber Dance/Dance of the Dawn (esprit spenders)
and threads them in as `sim_context`. Below-average proc/feather/esprit luck never
costs efficiency; only *misuse* does (surfaced by the ProcsAspect). `gauges=()`.

⚠️ ACTION IDS + POTENCIES — these are DT 7.x level-100 values to the best of
current knowledge and are mapped onto the FFLogs cast stream, so a wrong id
silently drops an ability from scoring/findings. The synthetic test-suite is
id-agnostic (fixtures are built from whatever this file declares), so tests pass
regardless of accuracy; the live calibration pass (scripts/validate_job_ceiling,
a real-log id/potency probe, ffxiv.consolegameswiki.com/wiki/Dancer) is the
authority. Verify before trusting the headline.
"""
from __future__ import annotations

from jobs._core.job import PHYSICAL_RANGED, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100; ⚠️ verify live) ------------------------

# Single-target combo
CASCADE          = 15989   # 220; 50% Silken Symmetry
FOUNTAIN         = 15990   # 280 combo'd; 50% Silken Flow
REVERSE_CASCADE  = 15991   # 280; needs Silken Symmetry; feather + esprit
FOUNTAINFALL     = 15992   # 340; needs Silken Flow; feather + esprit

# AoE line (multi-target; gauge-equivalent to the ST combo/procs — same esprit /
# feathers / Silken procs, just cleaving). IDs are sequential after Fountainfall.
WINDMILL         = 15993   # AoE Cascade (combo starter); 50% Silken Symmetry
BLADESHOWER      = 15994   # AoE Fountain (combo finisher); 50% Silken Flow
RISING_WINDMILL  = 15995   # AoE Reverse Cascade (Silken Symmetry proc)
BLOODSHOWER      = 15996   # AoE Fountainfall (Silken Flow proc)

# Esprit spenders (50 esprit each)
SABER_DANCE      = 16005   # 520
DANCE_OF_THE_DAWN = 36985  # 1000; needs Dance of the Dawn Ready (from Devilment, lv100)

# Step dances (the step actions are forced, ~0 potency, ~1.0s step-GCD)
STANDARD_STEP    = 15997
TECHNICAL_STEP   = 15998
EMBOITE          = 15999   # generic step action (Emboite/Entrechat/Jete/Pirouette)
ENTRECHAT        = 16000
JETE             = 16001
PIROUETTE        = 16002
STANDARD_FINISH  = 16192   # Double Standard Finish (2 steps); grants Standard Finish (self/partner ~5%) + Last Dance Ready
TECHNICAL_FINISH = 33218   # Quadruple Technical Finish (4 steps); DT level-100 in-log id (16196 is the stale/base id players never cast) — grants Technical Finish (party ~5%) + Flourishing Finish + esprit
FINISHING_MOVE   = 36984   # GCD that REPLACES Standard Step when Finishing Move Ready (from Flourish); 850, no steps, same Standard Finish buff + Last Dance Ready

# Proc / Flourish-granted GCDs + oGCDs
TILLANA          = 25790   # GCD, from Flourishing Finish (Technical Finish); esprit
LAST_DANCE       = 36983   # GCD, from Last Dance Ready (Standard/Technical Finish)
STARFALL_DANCE   = 25792   # GCD, from Flourishing Starfall (Devilment); rear positional
FAN_DANCE        = 16007   # oGCD, spends 1 feather; 50% Threefold Fan Dance
FAN_DANCE_II     = 16008   # oGCD, AoE feather spender (out of scope for ST v1)
FAN_DANCE_III    = 16009   # oGCD, from Threefold Fan Dance
FAN_DANCE_IV     = 25791   # oGCD, from Fourfold Fan Dance (Flourish)

# Burst / utility oGCDs
FLOURISH         = 16013   # 60s; grants Threefold + Fourfold + Silken Symmetry + Silken Flow
DEVILMENT        = 16011   # 120s; +crit/+DH self+partner 20s; grants Starfall + Dance of the Dawn Ready
CLOSED_POSITION  = 16006   # Dance Partner assignment (no DPS)
EN_AVANT         = 16010   # mobility (no DPS)
CURING_WALTZ     = 16015   # heal (no DPS)
IMPROVISATION    = 16014   # party mit/heal (no DPS)
IMPROVISED_FINISH = 25789  # finisher of Improvisation (no DPS)
SHIELD_SAMBA     = 16012   # party mit (no DPS)

# --- Status ids (ProcsAspect; ⚠️ verify live) -------------------------------
SILKEN_SYMMETRY_STATUS_ID = 2693   # -> Reverse Cascade
SILKEN_FLOW_STATUS_ID     = 2694   # -> Fountainfall
THREEFOLD_FAN_DANCE_STATUS_ID = 1820   # -> Fan Dance III
FOURFOLD_FAN_DANCE_STATUS_ID  = 2699   # -> Fan Dance IV
STANDARD_FINISH_STATUS_ID = 1821
TECHNICAL_FINISH_STATUS_ID = 1822
DEVILMENT_STATUS_ID       = 1825
PROC_DURATION_S: float = 30.0

# --- Self-buff timing + multipliers (⚠️ calibrate) --------------------------
# Standard Finish: maintained ~5% (refreshed every Standard Step ~30s, 60s buff)
# -> a WAR-style coverage overlay (full on the ceiling, measured on delivered).
STANDARD_FINISH_MULT = 1.05
STANDARD_FINISH_DURATION_S = 60.0
# Technical Finish: a ~5% party window (20s) -> PLD-style derived window.
TECHNICAL_FINISH_MULT = 1.05
TECHNICAL_FINISH_DURATION_S = 20.5
# Devilment: +20% crit rate + +20% direct-hit rate for 20s. Effective self-damage
# multiplier from the codebase's crit model (crit_mult≈1.638 per calibrate_crit_dh,
# the same value raid_buffs.py uses): (1 + 0.20×(1.638-1)) × (1 + 0.20×0.25) ≈ 1.18.
# Applied ONLY to the Dancer's OWN damage — derived from its DEVILMENT casts in
# scoring. Devilment (like Standard Finish + Dance Partner) lands only on the Dancer
# and their chosen partner, never the whole party, and the buffed player has no
# control over receiving it — so it is intentionally NOT modeled as a party buff
# other jobs' ceilings get (raid_buffs.py exposes only the party-wide Technical
# Finish for Dancer; see its single-target-exclusion note).
DEVILMENT_MULT = 1.18
DEVILMENT_DURATION_S = 20.0

# --- Reduced-recast step actions (the ~1.0s step-GCD) -----------------------
# The dance steps run faster than the 2.5s global (like RPR's Enshroud Reaping):
# keyed in the simulator's `gcd_duration` override + excluded from clip detection.
STEP_RECAST_S: float = 1.0
STEP_IDS: frozenset[int] = frozenset({EMBOITE, ENTRECHAT, JETE, PIROUETTE})

# --- Potencies (verified vs ffxiv.consolegameswiki.com/wiki/Dancer, DT 7.x) -
POTENCIES: dict[int, int] = {
    CASCADE:          220,
    FOUNTAIN:         280,
    REVERSE_CASCADE:  280,
    FOUNTAINFALL:     340,
    SABER_DANCE:      540,
    DANCE_OF_THE_DAWN: 1000,
    STANDARD_FINISH:  850,    # Double Standard Finish (2 steps)
    FINISHING_MOVE:   850,    # same as a Double Standard Finish, no steps
    TECHNICAL_FINISH: 1300,   # Quadruple Technical Finish (4 steps)
    TILLANA:          600,
    LAST_DANCE:       540,
    STARFALL_DANCE:   600,
    FAN_DANCE:        180,
    FAN_DANCE_III:    220,
    FAN_DANCE_IV:     460,
    # AoE line (per-target primary; full-to-all via AOE_POTENCIES)
    WINDMILL:         120,
    BLADESHOWER:      160,   # combo'd
    RISING_WINDMILL:  160,
    BLOODSHOWER:      200,
    FAN_DANCE_II:     100,
    # Step initiators / steps deal ~0 rotational damage and are forced.
    STANDARD_STEP:    0,
    TECHNICAL_STEP:   0,
    EMBOITE:          0,
    ENTRECHAT:        0,
    JETE:             0,
    PIROUETTE:        0,
    # Buttons with no direct potency.
    FLOURISH:         0,
    DEVILMENT:        0,
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    FAN_DANCE, FAN_DANCE_II, FAN_DANCE_III, FAN_DANCE_IV, FLOURISH, DEVILMENT,
    CLOSED_POSITION, EN_AVANT, CURING_WALTZ, IMPROVISATION, SHIELD_SAMBA,
})

# --- Cooldowns + charges ----------------------------------------------------
# Only genuinely RECAST-gated DPS actions. Standard/Technical Step are GCD
# initiators on their own recast (drift-tracked). Fan Dance / Saber Dance /
# Reverse Cascade / Fountainfall / Fan Dance III-IV / Tillana / Last Dance /
# Starfall / Dance of the Dawn are FEATHER/PROC/ESPRIT-gated -> simulator state
# flags, NOT cooldowns (listing them would read as false drift).
COOLDOWNS: dict[int, tuple[float, int]] = {
    STANDARD_STEP:  (30.0, 1),
    TECHNICAL_STEP: (120.0, 1),
    FLOURISH:       (60.0, 1),
    DEVILMENT:      (120.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    STANDARD_STEP:  720,    # the Standard Finish it leads to + buff upkeep
    TECHNICAL_STEP: 2200,   # Technical Finish + Tillana + the whole 2-min burst
    FLOURISH:       900,    # Fan Dance III + IV + two free proc GCDs
    DEVILMENT:      1500,   # the crit/DH burst window + Starfall + Dawn
}

# --- Canonical opener (⚠️ placeholder -> measured consensus in calibration) -
# OpenerAspect is a zero-priced diagnostic, so an approximate sequence is fine
# until the live pass replaces it with the measured-consensus opener.
CANONICAL_OPENER: tuple[int, ...] = (
    STANDARD_STEP, EMBOITE, ENTRECHAT, STANDARD_FINISH,   # pre-pull standard dance
    TECHNICAL_STEP, EMBOITE, ENTRECHAT, JETE, PIROUETTE, TECHNICAL_FINISH,
    DANCE_OF_THE_DAWN, TILLANA,
)

# --- Clip-detection exclusions ----------------------------------------------
# The whole dance — the Standard/Technical Step initiator + the step actions —
# runs at the ~1.0s dance GCD, intentionally faster than the 2.5s global, so those
# consecutive casts are NOT clips (the RPR Enshroud-Reaping analog).
CLIP_EXCLUSIONS: frozenset[int] = frozenset(STEP_IDS | {STANDARD_STEP, TECHNICAL_STEP})

# --- Burst-alignment abilities ----------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these): the
# 2-minute burst + its enablers.
BURST_ABILITIES: frozenset[int] = frozenset({
    TECHNICAL_STEP, TECHNICAL_FINISH, DEVILMENT, FLOURISH,
    DANCE_OF_THE_DAWN, TILLANA, STARFALL_DANCE, LAST_DANCE,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (TECHNICAL_STEP, DEVILMENT, FLOURISH)

# RNG / proc-gated GCDs+oGCDs — the player can't cast these on demand, so a
# sim/player mismatch on them is NOT a missed cast. Excluded from the diff.
RNG_PROC_IDS: frozenset[int] = frozenset({
    REVERSE_CASCADE, FOUNTAINFALL, FAN_DANCE_III, FAN_DANCE_IV,
    TILLANA, LAST_DANCE, STARFALL_DANCE, DANCE_OF_THE_DAWN,
})

# Defensive / utility actions the simulator never fires (excluded from the DPS
# timeline + cast-diff).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    CLOSED_POSITION, EN_AVANT, CURING_WALTZ, IMPROVISATION, IMPROVISED_FINISH,
    SHIELD_SAMBA,
})

# Finishing Move shares the Standard Step button + its 30s cooldown (Standard
# Step transforms into Finishing Move while Finishing Move Ready), so a Finishing
# Move cast resets Standard Step's cooldown — without this the drift detector
# false-flags Standard Step every time the player used Finishing Move instead.
CHARGE_SHARING: dict[int, int] = {FINISHING_MOVE: STANDARD_STEP}


# --- JOB_DATA bundle --------------------------------------------------------

# --- AoE potencies (dedicated AoE buttons + the cleaving burst) -------------
# ability_id -> per-extra-target potency. The AoE-line buttons (Windmill etc.)
# are full-to-all; the burst the ST rotation already casts cleaves with wiki-
# verified falloff (Saber Dance / Tillana / Dawn / Fan Dance IV / Technical
# Finish all 60% -> x0.4; Starfall 75% -> x0.25). Both scale on delivered +
# ceiling via `aoe_potency.potency_for`.
AOE_POTENCIES: dict[int, int] = {
    WINDMILL:          120,
    BLADESHOWER:       160,
    RISING_WINDMILL:   160,
    BLOODSHOWER:       200,
    FAN_DANCE_II:      100,
    SABER_DANCE:       216,   # 540 x 0.4
    DANCE_OF_THE_DAWN: 400,   # 1000 x 0.4
    TILLANA:           240,   # 600 x 0.4
    TECHNICAL_FINISH:  520,   # 1300 x 0.4
    STARFALL_DANCE:    150,   # 600 x 0.25 (75% falloff)
    FAN_DANCE_IV:      184,   # 460 x 0.4
}


JOB_DATA: JobData = JobData(
    job_name="Dancer",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(),                          # esprit/feathers are budgets, not gauges (see module docstring)
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=frozenset(),       # all recast oGCDs are pure DPS cooldowns
    rng_proc_ids=RNG_PROC_IDS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),
    charge_sharing=CHARGE_SHARING,      # Finishing Move shares Standard Step's cooldown
    raid_buffs={},                      # Technical Finish modeled via buff_windows (job-agnostic) / scoring
    role_policy=PHYSICAL_RANGED,
    aoe_potencies=AOE_POTENCIES,

    # A dropped GCD backfills with a Cascade/Fountain filler (~220-280); price a
    # miss at opportunity cost above that.
    filler_gcd_potency=250,
    # Tincture: effective BiS Dexterity (incl. party-comp bonus + food). ⚠️
    # placeholder = a typical DT physical-ranged value; calibrate per tier via
    # scripts/calibrate_tincture.py (self-correcting — same M on both sides).
    tincture_main_stat=6838,
)
