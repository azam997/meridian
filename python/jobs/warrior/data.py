"""Warrior data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

The analyzer's second TANK (after Paladin) and its fifth full idealized simulator.
Single source of truth for WAR numbers: potencies, the recast-gated cooldowns, the
Beast Gauge economy, the combo / Inner Release proc chain, and the defensive set
the Timeline Defensives lane renders.

Two things make WAR structurally interesting next to the DPS sims already shipped:

  * **Beast Gauge is an OFFENSIVE gauge** (unlike PLD's defensive-only Oath), so —
    like MCH heat / RPR soul — it gets a real `GaugeModel`: the combo finishers
    build it, Fell Cleave / Inner Chaos spend 50, and overcapping it is wasted
    potency. This is the first tank with an offensive gauge overcap pass.
  * **Surging Tempest is a MAINTAINED self-buff** (a 10% personal damage amp the
    player keeps ~100% uptime via Storm's Eye), not a burst window. It is the WAR
    analog of RPR's Death's Design — modeled the same way (scoring.py): the
    idealized side assumes full coverage, the delivered side is scaled by the
    *measured* coverage (jobs/warrior/surging_tempest.py). So a dropped Surging
    Tempest costs efficiency; at 100% uptime the x1.10 cancels in the ratio. This
    is the `coverage_intervals` overlay, NOT the FoF-style derived-window approach
    PLD uses (Fight or Flight is a real burst window; Surging Tempest is upkeep).

The guaranteed-crit-DH weaponskills (Inner Chaos always; Fell Cleave / Primal Rend
under Inner Release) are priced with a flat crit-DH multiplier in scoring.py,
exactly like MCH Reassemble / Full Metal Field.

⚠️ ACTION IDS / POTENCIES — best-effort DT 7.x level-100 values (standard WAR
game/FFLogs action ids; potencies cross-checked against The Balance / the console
wiki). They drive the mapping from the FFLogs cast stream onto this table, so a
wrong id silently drops an ability from scoring. VERIFY the full set against a real
WAR log's `masterData.abilities` on the first live run — in particular the
"replaced button" / Dawntrail-addition ids (Inner Release, Inner Chaos, Primal
Rend / Ruination / Wrath, Damnation) and the combo potencies. The synthetic
test-suite is id-agnostic (it builds fixtures from whatever this file declares), so
tests pass regardless; the live run is the authority. Same discipline as
paladin/data.py.
"""
from __future__ import annotations

from jobs._core.job import MELEE_TANK, CDRRule, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) ---------------------------------------

# Main combo (single target, physical)
HEAVY_SWING       = 31
MAIM              = 37      # +10 gauge (combo'd)
STORMS_PATH       = 42      # +20 gauge (combo'd); self-heal
STORMS_EYE        = 45      # +10 gauge (combo'd); applies / refreshes Surging Tempest
# Ranged filler (used only when forced off the boss; not part of the ST rotation)
TOMAHAWK          = 46
# Gauge spenders (weaponskills)
FELL_CLEAVE       = 3549    # -50 gauge; free + guaranteed crit-DH under Inner Release
INNER_CHAOS       = 16465   # -50 gauge; guaranteed crit-DH (needs Nascent Chaos from Infuriate)
# Burst pipeline
INNER_RELEASE     = 7389    # 60s; 3 free guaranteed-crit-DH weaponskills + Primal Rend; extends Surging Tempest
PRIMAL_REND       = 25753   # GCD jump, granted by Inner Release; guaranteed crit-DH -> Primal Ruination Ready
PRIMAL_RUINATION  = 36925   # GCD, granted after Primal Rend (Dawntrail)
PRIMAL_WRATH      = 36924   # oGCD, granted after the 3 Inner Release weaponskills (Wrathful)
# Damage oGCDs
INFURIATE         = 52      # 60s / 2 charges; +50 gauge + Nascent Chaos (next Fell Cleave -> Inner Chaos)
UPHEAVAL          = 7387    # 30s; direct-damage oGCD
ONSLAUGHT         = 7386    # 30s / 3 charges; gap-closer used as a damage oGCD
# AoE line (cast in multi-target windows; gauge-equivalent to the ST counterparts).
# ⚠️ ids + potencies best-effort DT 7.x, verify-live (Phase 5).
OVERPOWER         = 41      # AoE combo starter (replaces Heavy Swing)
MYTHRIL_TEMPEST   = 16462   # AoE combo finisher: +20 gauge + applies/refreshes Surging Tempest
DECIMATE          = 3550    # AoE Beast spender (replaces Fell Cleave); free crit-DH under Inner Release. XIVAPI-verified (16667 was Landslide, a MNK action)
CHAOTIC_CYCLONE   = 16463   # AoE Inner Chaos (Nascent Chaos; always guaranteed crit-DH)
OROGENY           = 25752   # AoE Upheaval (oGCD)

# Surging Tempest — the maintained 10% personal-damage self-buff (applied by
# Storm's Eye). ⚠️ verify the status id + multiplier live.
SURGING_TEMPEST_STATUS_ID = 2677
SURGING_TEMPEST_MULT: float = 1.10
SURGING_TEMPEST_DURATION_S: float = 30.0   # Storm's Eye duration
SURGING_TEMPEST_MAX_S: float = 60.0        # bankable cap (Storm's Eye + Inner Release extends)
INNER_RELEASE_EXTEND_S: float = 10.0       # Inner Release extends Surging Tempest

# Inner Release grants 3 free guaranteed-crit-DH weaponskills. The window that
# scoring scans for those crit-DH Fell Cleaves is anchored on the IR cast: 3 GCDs
# (~7.5s) plus a small buffer. (Inner Chaos is innately crit-DH regardless.)
INNER_RELEASE_STACKS = 3
INNER_RELEASE_WINDOW_S: float = 8.0

# --- Defensives (NO damage value; rendered on the Timeline Defensives lane and
# excluded from the DPS diff/scoring via the isDefensive flag). ⚠️ verify ids.
THRILL_OF_BATTLE  = 40
VENGEANCE         = 44
DAMNATION         = 36923   # Vengeance upgrade (Lv92+)
HOLMGANG          = 43
SHAKE_IT_OFF      = 7388
BLOODWHETTING     = 25751   # Raw Intuition / Nascent Flash line upgrade
NASCENT_FLASH     = 16464
RAW_INTUITION     = 3551
EQUILIBRIUM       = 3552

DEFENSIVE_IDS: frozenset[int] = frozenset({
    THRILL_OF_BATTLE, VENGEANCE, DAMNATION, HOLMGANG, SHAKE_IT_OFF,
    BLOODWHETTING, NASCENT_FLASH, RAW_INTUITION, EQUILIBRIUM,
})

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / crit modeling). Combo abilities carry
# their COMBO'd value (the sim always combos correctly). ⚠️ best-effort, verify.

POTENCIES: dict[int, int] = {
    # Main combo
    HEAVY_SWING:      300,
    MAIM:             440,   # combo'd; +10 gauge
    STORMS_PATH:      580,   # combo'd; +20 gauge
    STORMS_EYE:       560,   # combo'd; +10 gauge; Surging Tempest
    TOMAHAWK:         150,   # ranged filler (off-boss only)
    # Gauge spenders
    FELL_CLEAVE:      580,   # crit-DH when free under Inner Release (priced in scoring)
    INNER_CHAOS:      940,   # always guaranteed crit-DH (priced in scoring)
    # Burst pipeline
    PRIMAL_REND:      700,   # guaranteed crit-DH (only from Inner Release)
    PRIMAL_RUINATION: 780,
    PRIMAL_WRATH:     700,   # oGCD
    # Damage oGCDs
    INNER_RELEASE:      0,   # buff/enable only
    INFURIATE:          0,   # gauge/proc only
    UPHEAVAL:         420,
    ONSLAUGHT:        150,
    # AoE line (per-target primary; full-to-all via AOE_POTENCIES).
    OVERPOWER:        110,
    MYTHRIL_TEMPEST:  100,
    DECIMATE:         180,
    CHAOTIC_CYCLONE:  200,   # guaranteed crit-DH (priced in scoring)
    OROGENY:          150,
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) -----------
# ability_id -> per-extra-target potency. WAR's AoE line is full-to-all
# (secondary == primary == POTENCIES[id]). ⚠️ verify-live (Phase 5).
AOE_POTENCIES: dict[int, int] = {
    OVERPOWER:       110,
    MYTHRIL_TEMPEST: 100,
    DECIMATE:        180,
    CHAOTIC_CYCLONE: 200,
    OROGENY:         150,
}

# oGCD set — kept job-local (not read from XIVAPI) so scoring's GCD/oGCD split
# stays hermetic under the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    INNER_RELEASE, INFURIATE, UPHEAVAL, ONSLAUGHT, PRIMAL_WRATH,
})

# Weaponskills that land a guaranteed critical direct hit (priced x
# GUARANTEED_CRIT_DH_MULT in scoring). Inner Chaos and Primal Rend always; a
# Fell Cleave only while it's a free Inner Release stack (window-derived in
# scoring, so listing it here would over-credit every Fell Cleave).
ALWAYS_CRIT_DH_IDS: frozenset[int] = frozenset({INNER_CHAOS, PRIMAL_REND,
                                                CHAOTIC_CYCLONE})

# Empirically-derived guaranteed crit-DH multiplier (crit ~1.62 × DH 1.25 ≈ 2.03
# at current gear — same value MCH calibrated). ⚠️ recompute per gear tier via
# scripts/calibrate_crit_dh.py; crit scales slowly, so a re-run per major tier is
# plenty.
GUARANTEED_CRIT_DH_MULT: float = 2.03

# --- Beast Gauge (0-100) ----------------------------------------------------
# The offensive gauge. Combo finishers build it; Fell Cleave / Inner Chaos spend
# 50; Infuriate grants 50 instantly. Overcapping it is wasted potency.

BEAST_GENERATORS: dict[int, int] = {
    MAIM:        10,
    STORMS_EYE:  10,
    STORMS_PATH: 20,
    INFURIATE:   50,
    MYTHRIL_TEMPEST: 20,   # AoE combo finisher (gauge-equivalent to Storm's Path)
}
BEAST_SPENDERS: dict[int, int] = {
    FELL_CLEAVE: 50,
    INNER_CHAOS: 50,
    DECIMATE:        50,   # AoE Fell Cleave
    CHAOTIC_CYCLONE: 50,   # AoE Inner Chaos
}
BEAST_CAP = 100
# 50 gauge -> one Fell Cleave (580) in a GCD slot that would otherwise be a
# ~300 filler Heavy Swing. Marginal value per gauge over filler ≈ (580-300)/50.
# (Inner Chaos is the premium spend but it's gated on Infuriate, not gauge, so
# the filler-fungibility value of a raw gauge unit is the Fell Cleave delta.)
BEAST_VALUE_P_PER_UNIT: float = 5.6

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only RECAST-gated actions live
# here; the gauge-/proc-gated buttons (Fell Cleave, Inner Chaos, Primal Rend /
# Ruination / Wrath) are state-gated, so listing them would read as false drift.

COOLDOWNS: dict[int, tuple[float, int]] = {
    INNER_RELEASE: (60.0, 1),
    INFURIATE:     (60.0, 2),
    UPHEAVAL:      (30.0, 1),
    ONSLAUGHT:     (30.0, 3),
}

# Infuriate cooldown reduction: each Beast-gauge weaponskill (Fell Cleave / Inner
# Chaos, free or paid) cuts Infuriate's recast by 5s. This is the load-bearing WAR
# mechanic — it's what lets a real Warrior fire Infuriate (-> Inner Chaos) far
# more than the bare 60s/2-charge rate would allow (a live top-10 probe showed ~16
# Inner Chaos in a 9-min M12S kill vs ~11 charges of raw cooldown). The simulator
# applies it as fractional charge regen; the DriftAspect reads the CDRRules below.
# ⚠️ verify the 5s value live.
INFURIATE_CDR_S: float = 5.0
CDR_RULES: tuple[CDRRule, ...] = (
    CDRRule(source=FELL_CLEAVE, targets=frozenset({INFURIATE}),
            reduction_s=INFURIATE_CDR_S),
    CDRRule(source=INNER_CHAOS, targets=frozenset({INFURIATE}),
            reduction_s=INFURIATE_CDR_S),
)

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    INNER_RELEASE: 1500,   # 3 crit-DH Fell Cleaves + Primal Rend (priced via enablers)
    INFURIATE:      940,   # the Inner Chaos it enables (priced via enablers)
    UPHEAVAL:       420,
    ONSLAUGHT:      150,
}

# --- Canonical opener -------------------------------------------------------
# First ~12 in-fight GCDs in expected order. Tanks are on the boss at the pull
# (no run-in), so the first in-fight GCD is Heavy Swing; Surging Tempest is
# applied at the third GCD (Storm's Eye), Inner Release / Infuriate weave into
# the burst. ⚠️ Placeholder ordering — refine against a current WAR opener guide.
# OpenerAspect is a zero-priced diagnostic, so approximate is acceptable.
CANONICAL_OPENER: tuple[int, ...] = (
    HEAVY_SWING,
    MAIM,                # Infuriate weaved (pre-burst Nascent Chaos)
    STORMS_EYE,          # apply Surging Tempest; Inner Release weaved
    INNER_CHAOS,         # Nascent Chaos -> guaranteed crit-DH
    FELL_CLEAVE,         # free Inner Release stack
    FELL_CLEAVE,         # free Inner Release stack
    PRIMAL_REND,         # Primal Wrath weaved (Wrathful)
    PRIMAL_RUINATION,
    HEAVY_SWING,
    MAIM,
    STORMS_PATH,
    FELL_CLEAVE,
)

# --- Clip-detection exclusions ---------------------------------------------
# WAR has no reduced-GCD window (everything runs at the normal 2.5s recast), so
# nothing to exclude.
CLIP_EXCLUSIONS: frozenset[int] = frozenset()

# --- Drift-detection exclusions --------------------------------------------
# COOLDOWNS lists only recast-gated actions, so no gauge/proc-gated button
# reaches the drift detector. Nothing to exclude.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these).
BURST_ABILITIES: frozenset[int] = frozenset({
    INNER_RELEASE, INFURIATE, INNER_CHAOS, PRIMAL_REND, PRIMAL_WRATH, UPHEAVAL,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (INNER_RELEASE, INFURIATE)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Warrior",
    # Surging Tempest coverage reads the player's DamageDone every pull — bundle it.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="beast",
            generators=BEAST_GENERATORS,
            spenders=BEAST_SPENDERS,
            cap=BEAST_CAP,
            value_p_per_unit=BEAST_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=CDR_RULES,       # Fell Cleave / Inner Chaos cut Infuriate's recast 5s
    charge_sharing={},         # no shared-charge pools
    raid_buffs={},             # party buffs modeled via buff_windows (job-agnostic)
    role_policy=MELEE_TANK,
    aoe_potencies=AOE_POTENCIES,
    # A dropped high-potency GCD backfills with a Storm's Path combo hit.
    filler_gcd_potency=580,
    # Tincture: tank BiS Strength (incl. party-comp bonus + food) from xivgear;
    # tank attribute slope ~190. ⚠️ refine per tier via scripts/calibrate_tincture.py.
    tincture_main_stat=6386,
    tincture_role_coeff=190,
)
