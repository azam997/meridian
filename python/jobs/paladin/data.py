"""Paladin data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

The analyzer's first TANK. Single source of truth for PLD numbers: potencies,
the recast-gated cooldowns, the combo / proc chains, and the defensive set the
new Timeline Defensives lane renders.

Two things make PLD structurally different from the DPS sims already shipped:

  * **No offensive gauge.** PLD's only gauge (Oath) is spent purely on DEFENSIVE
    actions (Holy Sheltron / Intervention), which the idealized sim never casts —
    so `gauges=()`. MP never binds the optimized rotation (Requiescat's magical
    phase is MP-free, the Divine Might Holy Spirit is free, Riot Blade refunds
    MP), so it's not modeled either. All of PLD's complexity is the combo / proc
    STATE MACHINE in simulator.py, not resources.
  * **Ranged + melee.** Holy Spirit / Shield Lob (25y) plus the magical Confiteor
    combo are ranged; the rest is melee. The sim models the ranged side via a
    pre-pull Holy Spirit channel (like RPR's pre-Harpe) and the in-burst magical
    combo; hardcast Holy Spirit as steady-state filler is out of scope for v1.

⚠️ ACTION IDS / POTENCIES — best-effort DT 7.x level-100 values (standard PLD
game/FFLogs action ids; potencies cross-checked against The Balance / the console
wiki). They drive the mapping from the FFLogs cast stream onto this table, so a
wrong id silently drops an ability from scoring. VERIFY the full set against a
real PLD log's `masterData.abilities` on the first live run — in particular the
"replaced button" ids (Requiescat→Imperator, Sentinel→Guardian, Sheltron→Holy
Sheltron) and the Blade combo potencies. The synthetic test-suite is id-agnostic
(it builds fixtures from whatever this file declares), so tests pass regardless;
the live run is the authority. Same discipline as reaper/data.py.
"""
from __future__ import annotations

from jobs._core.job import MELEE_TANK, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) ---------------------------------------

# Main combo (melee, physical)
FAST_BLADE        = 9
RIOT_BLADE        = 15      # +MP
ROYAL_AUTHORITY   = 3539    # combo finisher -> Atonement Ready + Divine Might
# AoE line (cast in multi-target windows). The AoE combo grants Divine Might but
# NOT the Atonement chain (structurally different from Royal Authority), so the
# ST-vs-AoE combo choice is a BEAM fork (the search weighs the lost chain value).
# ⚠️ ids + potencies best-effort DT 7.x, verify-live (Phase 5).
TOTAL_ECLIPSE     = 7381    # AoE combo starter (replaces Fast Blade)
PROMINENCE        = 16457   # AoE combo finisher -> Divine Might (no Atonement chain)
HOLY_CIRCLE       = 16458   # AoE Holy Spirit (the Divine Might free instant)
# Atonement chain (granted by Royal Authority; each step arms the next)
ATONEMENT         = 16460
SUPPLICATION      = 36918
SEPULCHRE         = 36919
# Goring Blade — burst GCD. ⚠️ gating (Royal-Authority-granted vs recast) unclear;
# modeled here as a 60s-recast GCD (~one per Fight or Flight). Verify live.
GORING_BLADE      = 3538
# Ranged magical
HOLY_SPIRIT       = 7384    # instant under Divine Might / Requiescat (only form modeled)
HOLY_SPIRIT_CAST_S: float = 1.5   # hardcast time for the pre-pull channel
SHIELD_LOB        = 24      # ranged physical (pull filler)
# Magical combo (unlocked by Imperator/Requiescat; all instant, ranged)
CONFITEOR         = 16459
BLADE_OF_FAITH    = 25748
BLADE_OF_TRUTH    = 25749
BLADE_OF_VALOR    = 25750
BLADE_OF_HONOR    = 36922   # oGCD, granted after Blade of Valor
# Burst / damage oGCDs
FIGHT_OR_FLIGHT   = 20      # 60s, 20s self damage-up window (the burst anchor)
IMPERATOR         = 36921   # 60s, deals damage + opens the magical combo (was Requiescat)
REQUIESCAT        = 7383    # pre-96 name; kept so an older log still maps
CIRCLE_OF_SCORN   = 23      # 30s, direct + short DoT (folded into potency)
EXPIACION         = 25747   # 30s (was Spirits Within)
SPIRITS_WITHIN    = 29      # pre-rework name; kept for older logs
INTERVENE         = 16461   # 30s / 2 charges gap-closer (used as a damage oGCD)

# --- Defensives (NO damage value; rendered on the Timeline Defensives lane and
# excluded from the DPS diff/scoring via the isDefensive flag). ⚠️ verify ids.
SENTINEL          = 17
GUARDIAN          = 36920   # Sentinel upgrade (Lv92+)
HALLOWED_GROUND   = 30
BULWARK           = 22
SHELTRON          = 3542
HOLY_SHELTRON     = 25746   # Sheltron upgrade
INTERVENTION      = 7382
DIVINE_VEIL       = 3540
PASSAGE_OF_ARMS   = 7385
COVER             = 27
CLEMENCY          = 3541    # GCD heal — no enemy potency

DEFENSIVE_IDS: frozenset[int] = frozenset({
    SENTINEL, GUARDIAN, HALLOWED_GROUND, BULWARK, SHELTRON, HOLY_SHELTRON,
    INTERVENTION, DIVINE_VEIL, PASSAGE_OF_ARMS, COVER, CLEMENCY,
})

# --- Fight or Flight (the self-buff burst window) ---------------------------
# A timed personal damage-up the player places — PLD's analog of RPR's Death's
# Design, but a burst window rather than a maintained debuff. scoring.py derives
# the window from the FoF casts in the timeline itself (symmetric on delivered +
# idealized), so a late/dropped FoF or GCDs lost under it cost efficiency.
# ⚠️ verify the exact multiplier + scope (all damage vs physical) live.
FIGHT_OR_FLIGHT_DURATION_S: float = 20.0
FIGHT_OR_FLIGHT_MULT: float = 1.25

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / crit modeling). ⚠️ best-effort, verify.

POTENCIES: dict[int, int] = {
    # Main combo
    FAST_BLADE:       220,
    RIOT_BLADE:       330,   # combo'd, +MP
    ROYAL_AUTHORITY:  460,   # combo'd; grants Atonement Ready + Divine Might
    # Atonement chain
    ATONEMENT:        460,
    SUPPLICATION:     500,
    SEPULCHRE:        540,
    # Burst GCD (granted by Fight or Flight -> Goring Blade Ready)
    GORING_BLADE:     700,
    # Ranged magical
    HOLY_SPIRIT:      500,   # Divine Might value (instant; the in-rotation form)
    SHIELD_LOB:       100,
    # Magical combo — REQUIESCAT-enhanced potencies (always cast under Requiescat
    # from Imperator; the base 500/260/380/500 values are never used in rotation).
    CONFITEOR:       1000,
    BLADE_OF_FAITH:   760,
    BLADE_OF_TRUTH:   880,
    BLADE_OF_VALOR:  1000,
    BLADE_OF_HONOR:  1000,   # oGCD
    # Damage oGCDs
    FIGHT_OR_FLIGHT:    0,   # buff only
    IMPERATOR:        580,   # AoE damage on cast + grants 4 Requiescat stacks
    CIRCLE_OF_SCORN:  290,   # 140 direct + 30/tick DoT x 5 ticks (15s) folded in
    EXPIACION:        450,   # ⚠️ level-86 wiki value; verify lv100 bump
    INTERVENE:        150,
    # AoE line (per-target PRIMARY potency; falloff applied via AOE_POTENCIES).
    TOTAL_ECLIPSE:    120,
    PROMINENCE:       100,
    HOLY_CIRCLE:      250,   # Divine Might value (the in-rotation form; like Holy Spirit 500)
}

# --- AoE potencies (dedicated AoE buttons + the cleaving rotation abilities) -
# ability_id -> per-EXTRA-target potency (falloff baked in). Wiki-verified: PLD's
# whole AoE line is "60% less for all remaining enemies" -> secondary = primary x
# 0.4 (Circle of Scorn is the lone full-to-all). The AoE-line buttons (Total
# Eclipse / Prominence / Holy Circle) the sim swaps in, PLUS the cleaving abilities
# the ST rotation already casts (the magical Confiteor combo, Imperator, Circle of
# Scorn, Expiacion) — those scale on both delivered + ceiling.
AOE_POTENCIES: dict[int, int] = {
    TOTAL_ECLIPSE:    48,    # 120 x 0.4
    PROMINENCE:       40,    # 100 x 0.4
    HOLY_CIRCLE:     100,    # 250 x 0.4
    CONFITEOR:       400,    # 1000 x 0.4
    BLADE_OF_FAITH:  304,    # 760 x 0.4
    BLADE_OF_TRUTH:  352,    # 880 x 0.4
    BLADE_OF_VALOR:  400,    # 1000 x 0.4
    IMPERATOR:       232,    # 580 x 0.4
    CIRCLE_OF_SCORN: 290,    # full-to-all (no falloff)
    EXPIACION:       180,    # 450 x 0.4
}

# oGCD set — kept job-local (not read from XIVAPI) so scoring's GCD/oGCD split
# stays hermetic under the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    FIGHT_OR_FLIGHT, IMPERATOR, BLADE_OF_HONOR, CIRCLE_OF_SCORN, EXPIACION,
    INTERVENE,
})

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only RECAST-gated actions live
# here; proc/combo-gated buttons (the Atonement chain, the magical combo) are
# state-gated, so listing them would read as false drift.

# Goring Blade is NOT here: it's a proc (Goring Blade Ready, granted by Fight or
# Flight for 30s), not a recast-gated button, so listing it would read as false
# drift. The simulator models it as a FoF-granted proc.
COOLDOWNS: dict[int, tuple[float, int]] = {
    FIGHT_OR_FLIGHT: (60.0, 1),
    IMPERATOR:       (60.0, 1),
    CIRCLE_OF_SCORN: (30.0, 1),
    EXPIACION:       (30.0, 1),
    INTERVENE:       (30.0, 2),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    FIGHT_OR_FLIGHT: 1500,   # the 25% window over ~8 GCDs (rough; sim prices it)
    IMPERATOR:       1000,   # direct + opens the magical combo (priced via enablers)
    CIRCLE_OF_SCORN:  290,
    EXPIACION:        450,
    INTERVENE:        150,
}

# --- Canonical opener -------------------------------------------------------
# First ~12 in-fight GCDs in expected order. Holy Spirit is precast during the
# countdown (the ranged channel), so the first in-fight GCD is Fast Blade.
# ⚠️ Placeholder ordering — refine against a current PLD opener guide. OpenerAspect
# is a zero-priced diagnostic, so approximate is acceptable until the live pass.
CANONICAL_OPENER: tuple[int, ...] = (
    FAST_BLADE,         # FoF weaved here
    RIOT_BLADE,
    ROYAL_AUTHORITY,
    GORING_BLADE,       # Imperator weaved
    CONFITEOR,
    BLADE_OF_FAITH,
    BLADE_OF_TRUTH,
    BLADE_OF_VALOR,     # Blade of Honor weaved
    ATONEMENT,
    SUPPLICATION,
    SEPULCHRE,
    HOLY_SPIRIT,        # Divine Might
)

# --- Clip-detection exclusions ---------------------------------------------
# PLD has no reduced-GCD window (the magical combo runs at the normal 2.5s recast),
# so nothing to exclude.
CLIP_EXCLUSIONS: frozenset[int] = frozenset()

# --- Drift-detection exclusions --------------------------------------------
# COOLDOWNS lists only recast-gated actions, so no proc-gated button reaches the
# drift detector. Nothing to exclude.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these).
BURST_ABILITIES: frozenset[int] = frozenset({
    FIGHT_OR_FLIGHT, IMPERATOR, GORING_BLADE, CONFITEOR, BLADE_OF_HONOR,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (FIGHT_OR_FLIGHT, IMPERATOR)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Paladin",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(),                 # no offensive gauge (Oath is defensive-only)
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),              # no cross-cooldown reductions
    charge_sharing={},         # no shared-charge pools
    raid_buffs={},             # party buffs modeled via buff_windows (job-agnostic)
    role_policy=MELEE_TANK,
    aoe_potencies=AOE_POTENCIES,
    # A dropped high-potency GCD backfills with a Royal Authority combo hit.
    filler_gcd_potency=460,
    # Tincture: tank BiS Strength (incl. party-comp bonus + food) from xivgear;
    # tank attribute slope ~190. ⚠️ refine per tier via scripts/calibrate_tincture.py.
    tincture_main_stat=6386,
    tincture_role_coeff=190,
)
