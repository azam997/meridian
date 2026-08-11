"""Reaper data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

Single source of truth for RPR numbers: potencies, cooldowns, gauge rules
(Soul + Shroud), the Enshroud sub-rotation, Death's Design upkeep, and the
Soulsow -> Harvest Moon downtime re-arm. Values cross-checked against
ffxiv.consolegameswiki.com/wiki/Reaper (the PvE actions page the planning
brief pointed at) for level-100 potencies.

Pinned to FFXIV patch series 7.x. If the analyzer is ever run against a log
from a different patch, `PATCH_VERSION` is the place to gate.

⚠️ ACTION IDS — these are the game/FFLogs action IDs to the best of current
knowledge (Endwalker base 243xx + Dawntrail additions 369xx). They drive the
mapping from the FFLogs cast stream onto this table, so a wrong ID silently
drops an ability from scoring. Verify the full set against a real RPR log's
`masterData.abilities` on the first live run (Phase 10), and the handful of
"replaced button" ids (Unveiled*, Executioner's*, Sacrificium, Perfectio) in
particular. The synthetic test-suite is ID-agnostic (it builds fixtures from
whatever this file declares), so tests pass regardless; the live run is the
authority.
"""
from __future__ import annotations

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) ---------------------------------------

# Main combo
SLICE              = 24373
WAXING_SLICE       = 24374
INFERNAL_SLICE     = 24375
# Ranged opener channel — pre-cast during the countdown (1.3s cast / 2.5s GCD /
# 25y). The sim may pre-channel it (swept) to fill the melee run-in with damage.
HARPE              = 24386
HARPE_CAST_S: float = 1.3
# Death's Design upkeep
SHADOW_OF_DEATH    = 24378
# Soul gauge builders
SOUL_SLICE         = 24380
SOUL_SCYTHE        = 24381   # AoE soul builder
# Soul spenders -> Soul Reaver / Executioner
BLOOD_STALK        = 24389
UNVEILED_GIBBET    = 24390   # replaces Blood Stalk while Enhanced Gibbet up
UNVEILED_GALLOWS   = 24391   # replaces Blood Stalk while Enhanced Gallows up
GLUTTONY           = 24393
# Soul Reaver GCDs (positional)
GIBBET             = 24382   # flank
GALLOWS            = 24383   # rear
# Executioner GCDs (positional; Gluttony-gated, Lv96+)
EXEC_GIBBET        = 36970   # flank
EXEC_GALLOWS       = 36971   # rear
# Enshroud set
ENSHROUD           = 24394
VOID_REAPING       = 24395   # flank, 1.5s recast in Enshroud
CROSS_REAPING      = 24396   # rear, 1.5s recast in Enshroud
LEMURES_SLICE      = 24399
SACRIFICIUM        = 36969   # replaces Gluttony during Enshroud (Oblatio)
COMMUNIO           = 24398
PERFECTIO          = 36973   # replaces Communio after Plentiful Harvest's Occulta
# Raid burst
ARCANE_CIRCLE      = 24405
PLENTIFUL_HARVEST  = 24385
# Downtime re-arm
SOULSOW            = 24387
HARVEST_MOON       = 24388

# --- AoE line (multi-target) -----------------------------------------------
# The dedicated AoE buttons that replace the single-target rotation when 3+
# enemies are clustered. Absent from the single-target sim (it never casts
# them), so they were scored 0 until now. ⚠️ IDs + potencies are best-effort
# (DT 7.x level 100, cross-checked vs the wiki) and MUST be verified against a
# real multi-target RPR log's masterData.abilities on the first live run
# (Phase 5) — same discipline as the single-target set above.
SPINNING_SCYTHE    = 24376   # AoE combo starter (replaces Slice)
NIGHTMARE_SCYTHE   = 24377   # AoE combo finisher (replaces Waxing/Infernal)
WHORL_OF_DEATH     = 24379   # AoE Shadow of Death — applies Death's Design to all
GRIM_SWATHE        = 24392   # AoE Blood Stalk (oGCD soul spender)
GUILLOTINE         = 24384   # AoE Soul Reaver GCD (replaces Gibbet/Gallows)
GRIM_REAPING       = 24397   # AoE Void/Cross Reaping (inside Enshroud)
LEMURES_SCYTHE     = 24400   # AoE Lemure's Slice (oGCD, drains Void Shroud)
EXEC_GUILLOTINE    = 36972   # AoE Executioner GCD (Gluttony-gated)

# Death's Design debuff (10% personal damage amp on the target).
DEATHS_DESIGN_STATUS_ID = 2586
DEATHS_DESIGN_MULT: float = 1.10

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / crit modeling). POSITIONAL abilities
# carry the positional-HIT value here (assume-always-hit per the plan); the
# non-positional value lives in POSITIONAL_MISS_POTENCY for miss pricing.

POTENCIES: dict[int, int] = {
    # Main combo
    SLICE:           420,
    WAXING_SLICE:    500,   # combo'd
    INFERNAL_SLICE:  600,   # combo'd
    HARPE:           300,   # ranged opener channel (pre-pull) — VERIFIED live
                            # 2026-06-10 (probe_rpr_potency.py: implied ~300)
    # Death's Design upkeep (also +10 soul)
    SHADOW_OF_DEATH: 300,
    # Soul builders
    SOUL_SLICE:      520,
    SOUL_SCYTHE:     180,   # AoE
    # Soul spenders (oGCD direct hits)
    BLOOD_STALK:     340,
    UNVEILED_GIBBET: 440,
    UNVEILED_GALLOWS:440,
    GLUTTONY:        560,
    # Soul Reaver GCDs (positional-hit value)
    GIBBET:          560,
    GALLOWS:         560,
    # Executioner GCDs (positional-hit value)
    EXEC_GIBBET:     760,
    EXEC_GALLOWS:    760,
    # Enshroud set
    ENSHROUD:          0,   # oGCD, no damage
    VOID_REAPING:    640,   # enhanced/positional-hit value
    CROSS_REAPING:   640,
    LEMURES_SLICE:   280,
    SACRIFICIUM:     700,
    COMMUNIO:       1100,
    PERFECTIO:      1300,
    # Raid burst
    ARCANE_CIRCLE:     0,   # oGCD, no damage (party buff)
    PLENTIFUL_HARVEST:1000,  # full-party (8 Immortal Sacrifice) value — VERIFIED
                             # live 2026-06-10 (probe_rpr_potency.py: q25 damage
                             # ratio vs the Communio anchor ⇒ implied ~1005)
    # Downtime re-arm
    SOULSOW:           0,   # no damage; arms Harvest Moon
    HARVEST_MOON:    800,
    # AoE line — PRIMARY potency (full-to-all unless a falloff is noted in
    # SPLASH_POTENCIES). ⚠️ best-effort, verify live (Phase 5).
    SPINNING_SCYTHE:  140,
    NIGHTMARE_SCYTHE: 140,
    WHORL_OF_DEATH:   100,  # AoE DD applicator (low direct potency; value is the amp)
    GRIM_SWATHE:      140,
    GUILLOTINE:       200,
    GRIM_REAPING:     220,
    LEMURES_SCYTHE:   100,
    EXEC_GUILLOTINE:  260,
}

# --- Splash (multi-target secondary potency) -------------------------------
# ability_id -> potency dealt to EACH additional target beyond the primary.
#
# CALIBRATED from a live M10S top-parse probe (2026-05-30): top Reapers run the
# SINGLE-TARGET rotation and never cast the AoE combo at the current 2-target
# tier — their entire multi-target output is FREE-SPLASH on rotation abilities
# the idealized ST sim already casts, so crediting their splash on both
# delivered and the ceiling is symmetric (the >100% guard holds). maxN observed
# = 2, so the credit is exactly one extra target's worth. See
# [[multitarget-live-findings]].
#
# Secondary potency = primary × measured secondary/primary unmitigated ratio
# (~0.7). The method takes the within-packet max hit as the primary, which
# biases the ratio slightly LOW — the safe direction (under-credits splash →
# efficiency stays <= 100%), and the ratio largely cancels in delivered/ceiling.
#
# The dedicated AoE-combo buttons (Spinning Scythe, Guillotine, Grim Swathe, …)
# are deliberately NOT here: the ST sim doesn't cast them, so crediting their
# delivered splash would break symmetry. They stay in POTENCIES (primary scored
# if ever cast) but earn no secondary credit until the sim models the AoE
# rotation — unnecessary at the current tier (nobody uses it).
SPLASH_POTENCIES: dict[int, int] = {
    COMMUNIO:          770,   # 1100 × ~0.70
    PERFECTIO:         910,   # 1300 × ~0.70
    PLENTIFUL_HARVEST: 800,   # 1000 × ~0.80
    SACRIFICIUM:       490,   #  700 × ~0.70
    GLUTTONY:          390,   #  560 × ~0.70
    HARVEST_MOON:      480,   #  800 × ~0.60
    WHORL_OF_DEATH:     80,   #  100 × ~0.80 (AoE Death's Design applicator)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) ----------
# ability_id -> per-extra-target potency. RPR's AoE line is full-to-all (no
# falloff): secondary == primary (== POTENCIES[id]). Whorl of Death stays in
# SPLASH_POTENCIES (it has a falloff secondary). `jobs._core.sim.aoe_potency`
# reads both maps. ⚠️ potencies best-effort, verify-live (Phase 5).
AOE_POTENCIES: dict[int, int] = {
    SPINNING_SCYTHE:  140,
    NIGHTMARE_SCYTHE: 140,
    SOUL_SCYTHE:      180,
    GUILLOTINE:       200,
    EXEC_GUILLOTINE:  260,
    GRIM_SWATHE:      140,
    GRIM_REAPING:     220,
    LEMURES_SCYTHE:   100,
}

# Non-positional ("missed") potency for the positional abilities. The delta
# (POTENCIES[id] - this) is what a missed positional costs — priced only when
# the bonus-byte detector (positionals.py) is wired in. Idealized always uses
# the hit value above.
POSITIONAL_MISS_POTENCY: dict[int, int] = {
    GIBBET:        500,
    GALLOWS:       500,
    EXEC_GIBBET:   700,
    EXEC_GALLOWS:  700,
    VOID_REAPING:  580,
    CROSS_REAPING: 580,
}
POSITIONAL_IDS: frozenset[int] = frozenset(POSITIONAL_MISS_POTENCY)

# oGCD set — kept job-local (not read from XIVAPI) so scoring's GCD/oGCD split
# stays hermetic under the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    BLOOD_STALK, UNVEILED_GIBBET, UNVEILED_GALLOWS, GLUTTONY,
    SACRIFICIUM, LEMURES_SLICE, ENSHROUD, ARCANE_CIRCLE,
})

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only the genuinely RECAST-gated
# actions live here; RPR's signature buttons (Enshroud, Blood Stalk, Gibbet,
# Communio, …) are GAUGE-gated, so listing them would read as false drift.

COOLDOWNS: dict[int, tuple[float, int]] = {
    SOUL_SLICE:    (30.0, 2),
    SOUL_SCYTHE:   (30.0, 2),
    GLUTTONY:      (60.0, 1),
    ARCANE_CIRCLE: (120.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    SOUL_SLICE:    520,    # direct potency
    SOUL_SCYTHE:   180,
    GLUTTONY:      560,    # direct + enables 2 Executioner GCDs (priced via enablers)
    ARCANE_CIRCLE: 1000,   # raid buff + Plentiful Harvest enable (rough)
}

# --- Soul gauge -------------------------------------------------------------
# 0–100. Built by the main combo / Shadow of Death (+10 each), Soul Slice /
# Soul Scythe (+50), Harvest Moon (+10). Spent in 50s by Blood Stalk / Unveiled
# (-> Soul Reaver -> a Gibbet/Gallows) and Gluttony (-> 2 Executioner GCDs).

SOUL_GENERATORS: dict[int, int] = {
    SLICE:           10,
    WAXING_SLICE:    10,
    INFERNAL_SLICE:  10,
    SHADOW_OF_DEATH: 10,
    SOUL_SLICE:      50,
    SOUL_SCYTHE:     50,
    HARVEST_MOON:    10,
    # AoE line (gauge-equivalent to the ST counterparts; +10/GCD per combo step,
    # Whorl mirrors Shadow of Death). ⚠️ verify-live (Phase 5).
    SPINNING_SCYTHE:  10,
    NIGHTMARE_SCYTHE: 10,
    WHORL_OF_DEATH:   10,
}
SOUL_SPENDERS: dict[int, int] = {
    BLOOD_STALK:      50,
    UNVEILED_GIBBET:  50,
    UNVEILED_GALLOWS: 50,
    GLUTTONY:         50,
    GRIM_SWATHE:      50,   # AoE Blood Stalk (oGCD) — same 50-soul -> Soul Reaver
}
SOUL_CAP = 100
# 50 soul -> Blood Stalk (340, ~free weave) + a Gibbet/Gallows (560 vs ~500
# filler) + 10 shroud toward an Enshroud. The marginal value of one soul when
# it would otherwise overcap is roughly (premium / 50). Estimate; refine
# alongside the shroud rate once the sim is validated against top parses.
SOUL_VALUE_P_PER_UNIT: float = 8.0

# --- Shroud gauge -----------------------------------------------------------
# 0–100. Built by the Soul Reaver / Executioner GCDs (+10 each). Spent (50) by
# Enshroud to enter the Lemure sub-rotation. (Plentiful Harvest's "Ideal Host"
# grants a FREE Enshroud rather than shroud, modeled in the simulator.)

SHROUD_GENERATORS: dict[int, int] = {
    GIBBET:        10,
    GALLOWS:       10,
    EXEC_GIBBET:   10,
    EXEC_GALLOWS:  10,
    # AoE Soul Reaver / Executioner GCDs feed shroud identically.
    GUILLOTINE:      10,
    EXEC_GUILLOTINE: 10,
}
SHROUD_SPENDERS: dict[int, int] = {ENSHROUD: 50}
SHROUD_CAP = 100
# 50 shroud -> Enshroud window: ~5×640 Reaping + 1100 Communio + 2×280 Lemure's
# Slice, mostly replacing ~500 filler GCDs. The window's premium over filler is
# large; per-unit estimate below. Refine post-validation.
SHROUD_VALUE_P_PER_UNIT: float = 30.0

# --- Enshroud sub-rotation constants ---------------------------------------
LEMURE_SHROUD_ON_ENSHROUD = 5     # stacks granted by Enshroud
ENSHROUD_WINDOW_S: float = 9.0    # ~5×1.5s Reaping + Communio resolution buffer

# --- Death's Design upkeep --------------------------------------------------
# Shadow of Death applies a 30s debuff, extendable to a 60s cap by refreshing.
# The simulator refreshes on cadence to hold 100% uptime.
DEATHS_DESIGN_DURATION_S: float = 30.0
DEATHS_DESIGN_MAX_S: float = 60.0

# --- Canonical opener -------------------------------------------------------
# First ~12 in-fight GCDs in expected order. MEASURED from the live M9S top-10
# (2026-06-10, scripts/probe_rpr_potency.py): all four sampled top parses open
# pre-channeled Harpe > Shadow of Death > Soul Slice > the Executioner pair
# (Gluttony weaved early) > Soul Slice > Plentiful Harvest (Arcane Circle
# weaved) > a double Reaping cycle > Communio. Minor per-player variance:
# Executioner order flips, the 2nd Soul Slice can slide one slot, and the
# Reaping pair may start on Cross. OpenerAspect is a zero-priced diagnostic,
# so the modal line is what we pin.
CANONICAL_OPENER: tuple[int, ...] = (
    HARPE,             # pre-channeled during the countdown, lands ~t=0
    SHADOW_OF_DEATH,   # apply Death's Design
    SOUL_SLICE,
    EXEC_GALLOWS,      # Gluttony weaved early -> 2 Executioner GCDs
    EXEC_GIBBET,
    SOUL_SLICE,
    PLENTIFUL_HARVEST, # Arcane Circle weaved
    VOID_REAPING,      # Ideal Host free Enshroud
    CROSS_REAPING,
    VOID_REAPING,
    CROSS_REAPING,
    COMMUNIO,
)

# --- Clip-detection exclusions ---------------------------------------------
# Void / Cross Reaping run at a 1.5s recast inside Enshroud — not a clip.
CLIP_EXCLUSIONS: frozenset[int] = frozenset({VOID_REAPING, CROSS_REAPING})

# --- Drift-detection exclusions --------------------------------------------
# COOLDOWNS already lists only the recast-gated actions, so no gauge-gated
# button reaches the drift detector. Nothing to exclude.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these).
BURST_ABILITIES: frozenset[int] = frozenset({
    GLUTTONY, ENSHROUD, PLENTIFUL_HARVEST, ARCANE_CIRCLE,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (ARCANE_CIRCLE, ENSHROUD, GLUTTONY, PLENTIFUL_HARVEST)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Reaper",
    # Death's Design coverage reads the player's DamageDone every pull — bundle it.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="soul",
            generators=SOUL_GENERATORS,
            spenders=SOUL_SPENDERS,
            cap=SOUL_CAP,
            value_p_per_unit=SOUL_VALUE_P_PER_UNIT,
        ),
        GaugeModel(
            name="shroud",
            generators=SHROUD_GENERATORS,
            spenders=SHROUD_SPENDERS,
            cap=SHROUD_CAP,
            value_p_per_unit=SHROUD_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    clip_exclusions=CLIP_EXCLUSIONS,
    # Enshroud spawns a ~9s window of 1.5s-recast Void/Cross Reaping; without
    # this skip, ClippingAspect reads those pairs as clipping (MCH Hypercharge
    # analog). Keyed on the Enshroud oGCD that opens the window.
    clip_skip_windows={ENSHROUD: ENSHROUD_WINDOW_S},
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),          # no cross-cooldown reductions
    charge_sharing={},     # no shared-charge pools
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    raid_buffs={},         # Arcane Circle modeled via buff_windows (job-agnostic)
    role_policy=MELEE_DPS,
    # A dropped high-potency GCD backfills with a main-combo hit (~420-600);
    # use the mid value so a missed tool is priced at opportunity cost.
    filler_gcd_potency=500,
    # Tincture: effective BiS Strength from xivgear (incl. party-comp bonus +
    # food — the in-raid value). Base for f(base+Δ)/f(base), Δ541 ⇒ ≈ +8.23%.
    tincture_main_stat=6841,
    # Harpe = the disconnect bridge: enables consensus ranged-filler windows on
    # the LENIENT ceiling (top M10S refs cast 11-14 mid-fight Harpes at hard
    # consensus times — see scripts/probe_rpr_harpe.py / NEXT_STEPS).
    ranged_filler_id=HARPE,
)
