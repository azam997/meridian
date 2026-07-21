"""Dragoon data tables (Dawntrail 7.2, level 100) + the `JOB_DATA` JobData instance.

Single source of truth for DRG numbers: potencies, the recast-gated cooldowns,
the Firstminds' Focus gauge, the branching combo, the Chaotic Spring DoT, and the
damage self-buffs (Power Surge / Lance Charge / Life of the Dragon).

What makes DRG structurally distinct from the melee sims already shipped:

  * **A branching combo** (the reason this job needs the beam, like SAM). After the
    starter (True Thrust, or Raiden Thrust once Draconian Fire is up) the 2nd GCD
    forks: **Lance Barrage** -> the raw-damage *Heavens' Thrust* combo, or **Spiral
    Blow** -> the *Chaotic Spring* DoT combo. Both converge on Drakesbane. The branch
    choice trades raw GCD potency (raw combo) against Power Surge upkeep + the DoT
    (DoT combo), so the optimal cadence is a search axis -> `gcd_candidates` exposes
    the fork and the beam picks it (see simulator.py).
  * **Three damage self-buffs** that are potency multipliers and are modeled INSIDE
    the sim (derived from the timeline casts, symmetric on delivered + idealized; see
    scoring.py): **Power Surge** (+10%, from Spiral Blow, ~maintained via the DoT
    combo), **Lance Charge** (+10%, 60s burst), **Life of the Dragon** (+15%, 60s
    burst, from Geirskogul). Because the branch fork directly controls Power Surge
    uptime, these are NOT a flat full-coverage overlay (that would over-credit a
    raw-heavy line) — they ride the timeline so the search sees their true cost.
  * **Life Surge** — a 2-charge/40s buff that makes the next weaponskill a guaranteed
    CRITICAL hit (crit only, no direct hit). Priced with the crit-only multiplier (the
    MCH-Reassemble / SAM-always-crit pattern), placed by the sim on the highest-potency
    finisher (Heavens' Thrust / Drakesbane). See scoring.py + lifesurge.py.
  * **Battle Litany** (+10% CRIT RATE) is a party buff and is crit -> NEUTRAL in the
    crit-neutral potency model (not a damage multiplier). The sim casts it for timeline
    realism but it adds no potency.
  * **Chaotic Spring DoT** — a 24s DoT scored by time-to-next-refresh (the SAM
    Higanbana pattern). Refreshed by *running the DoT combo*, so the refresh decision
    IS the branch fork.

⚠️ ACTION + STATUS IDS are best-effort from XIVAPI v2 (the level-100 / DRG-tagged
rows); POTENCIES are DT 7.2 level-100 values cross-checked between the official Job
Guide and ffxiv.consolegameswiki.com. They drive the FFLogs cast-stream mapping, so a
wrong id silently drops an ability from scoring. The synthetic test suite is
ID-agnostic (fixtures are built from whatever this file declares), so tests pass
regardless; the FIRST LIVE RUN is the authority — verify the full set against a real
DRG log's `masterData.abilities` (probe via scripts/probe_dragoon_ids.py), and refresh
the crit multiplier + tincture per gear tier (calibrate_crit_dh.py / calibrate_tincture.py).
"""
from __future__ import annotations

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.2, level 100; XIVAPI v2) -----------------------------

# Main combo — starters
TRUE_THRUST        = 75       # combo starter (opener / when Draconian Fire is down)
RAIDEN_THRUST      = 16479    # replaces True Thrust under Draconian Fire; +1 Firstminds' Focus
# Branch A — Heavens' Thrust (raw-damage) combo
LANCE_BARRAGE      = 36954    # 2nd GCD (raw branch); combo'd
HEAVENS_THRUST     = 25771    # 3rd GCD (raw branch); combo'd, high potency
FANG_AND_CLAW      = 3554     # 4th GCD (raw branch); FLANK positional; grants Draconian Fire
# Branch B — Chaotic Spring (DoT) combo
SPIRAL_BLOW        = 36955    # 2nd GCD (DoT branch); combo'd; grants Power Surge (+10%, 30s)
CHAOTIC_SPRING     = 25772    # 3rd GCD (DoT branch); REAR positional; applies the DoT
WHEELING_THRUST    = 3556     # 4th GCD (DoT branch); REAR positional; grants Draconian Fire
# Shared finisher
DRAKESBANE         = 36952    # 5th GCD (both branches); grants Draconian Fire

# AoE GCD line (cast only in multi-target windows)
DOOM_SPIKE         = 86       # AoE starter; becomes Draconian Fury under Draconian Fire
DRACONIAN_FURY     = 25770    # AoE Raiden Thrust (proc); +1 Firstminds' Focus
SONIC_THRUST       = 7397     # AoE 2nd; grants Power Surge
COERTHAN_TORMENT   = 16477    # AoE 3rd; grants Draconian Fire

# oGCDs — burst + gauge
LANCE_CHARGE       = 85       # 60s; self-buff +10% damage, 20s
BATTLE_LITANY      = 3557     # 120s; party +10% crit rate, 20s (CRIT -> not a potency mult)
LIFE_SURGE         = 83       # 40s / 2 charges; next weaponskill is a guaranteed crit (5s)
GEIRSKOGUL         = 3555     # 60s; activates Life of the Dragon (+15%, 20s) + 1 Nastrond Ready
NASTROND           = 7400     # during LotD; 1 use/window (DT 7.x — VERIFIED live: Nastrond==Geirskogul)
STARDIVER          = 16480    # during LotD; grants Starcross Ready
STARCROSS          = 36956    # follow-up to Stardiver
DRAGONFIRE_DIVE    = 96       # 120s; grants Dragon's Flight
RISE_OF_THE_DRAGON = 36953    # follow-up to Dragonfire Dive (needs Dragon's Flight)
HIGH_JUMP          = 16478    # 30s; grants Dive Ready
MIRAGE_DIVE        = 7399     # follow-up to High Jump (needs Dive Ready)
WYRMWIND_THRUST    = 25773    # 10s; consumes 2 Firstminds' Focus

# Defensive / movement (NO damage value; excluded from the DPS diff via isDefensive)
ELUSIVE_JUMP       = 94       # 30s; backward hop (movement)
WINGED_GLIDE       = 36951    # 60s / 2 charges; gap-closer (movement)
PIERCING_TALON     = 90       # ranged filler weaponskill (not part of the uptime rotation)

DEFENSIVE_IDS: frozenset[int] = frozenset({ELUSIVE_JUMP, WINGED_GLIDE})

# --- Status (buff) IDs ------------------------------------------------------
# Match by NAME on the wire if a different row is emitted (XIVAPI returns legacy
# duplicates for several of these). ⚠️ verify live.
POWER_SURGE_STATUS_ID    = 2720    # +10% damage (from Spiral Blow / Sonic Thrust)
LANCE_CHARGE_STATUS_ID   = 1864    # +10% damage
LOTD_STATUS_ID           = 3177    # Life of the Dragon: +15% damage; enables Nastrond/Stardiver/Starcross
LIFE_SURGE_STATUS_ID     = 116     # next weaponskill guaranteed crit
BATTLE_LITANY_STATUS_ID  = 786     # +10% crit rate (party) — crit, not a potency mult
DRACONIAN_FIRE_STATUS_ID = 1863    # enables Raiden Thrust / Draconian Fury
DIVE_READY_STATUS_ID     = 1243    # enables Mirage Dive
NASTROND_READY_STATUS_ID = 3844    # 3 stacks from Geirskogul
STARCROSS_READY_STATUS_ID = 3846
DRAGONS_FLIGHT_STATUS_ID = 3845    # enables Rise of the Dragon

# Self-buff damage multipliers + durations (the in-sim self-buff windows).
POWER_SURGE_MULT: float        = 1.10
POWER_SURGE_DURATION_S: float  = 30.0
LANCE_CHARGE_MULT: float       = 1.10
LANCE_CHARGE_DURATION_S: float = 20.0
LOTD_MULT: float               = 1.15
LOTD_DURATION_S: float         = 20.0
# Life Surge converts the next weaponskill to a guaranteed crit; the 5s window is
# how long the buff lingers before the next GCD consumes it (always the very next
# weaponskill in practice).
LIFE_SURGE_WINDOW_S: float     = 5.0

# Nastrond casts granted per Geirskogul / Life of the Dragon window. DT 7.x grants
# ONE (the old 3-per-window was Endwalker) — VERIFIED live (a top M11S pull cast
# Nastrond == Geirskogul == 11, i.e. 1:1).
NASTROND_PER_LOTD: int = 1

# Crit-only multiplier at current gear (the SAM value; crit_mult ~ 1.62. WAR's
# guaranteed crit-DH 2.03 / the fixed DH 1.25 = 1.62). Life Surge is crit ONLY (no
# direct hit), so this is the crit-only factor. ⚠️ recompute per gear tier via
# scripts/calibrate_crit_dh.py (it prints crit_mult); crit scales slowly.
GUARANTEED_CRIT_MULT: float = 1.62

# --- Potencies --------------------------------------------------------------
# action_id -> base potency (no buffs / crit modeling). Combo abilities carry
# their COMBO'd value; the positional GCDs (Chaotic Spring / Wheeling Thrust /
# Fang and Claw) carry the positional-HIT value here (assume-always-hit; the
# non-positional value lives in POSITIONAL_MISS_POTENCY for miss pricing). The DoT
# is scored separately (scoring._chaotic_spring_dot_potency).

POTENCIES: dict[int, int] = {
    # Starters
    TRUE_THRUST:        230,
    RAIDEN_THRUST:      320,
    # Raw branch
    LANCE_BARRAGE:      340,   # combo'd
    HEAVENS_THRUST:     460,   # combo'd
    FANG_AND_CLAW:      340,   # combo'd + flank positional (300 on miss)
    # DoT branch
    SPIRAL_BLOW:        300,   # combo'd; grants Power Surge
    CHAOTIC_SPRING:     340,   # combo'd + rear positional (300 on miss); applies DoT
    WHEELING_THRUST:    340,   # combo'd + rear positional (300 on miss)
    # Shared finisher
    DRAKESBANE:         460,
    # oGCDs
    GEIRSKOGUL:         280,
    NASTROND:           720,
    STARDIVER:          840,
    STARCROSS:         1000,
    DRAGONFIRE_DIVE:    500,
    RISE_OF_THE_DRAGON: 550,
    HIGH_JUMP:          400,
    MIRAGE_DIVE:        380,
    WYRMWIND_THRUST:    440,
    # AoE line — PRIMARY potency (multi-target windows only). ⚠️ verify live.
    DOOM_SPIKE:         110,
    DRACONIAN_FURY:     130,
    SONIC_THRUST:       120,   # combo'd
    COERTHAN_TORMENT:   150,   # combo'd
    # Ranged filler (not in the uptime rotation; primary only).
    PIERCING_TALON:     200,
}

# --- Splash (free-splash secondary potency) --------------------------------
# ability_id -> potency dealt to EACH additional target beyond the primary. DRG's
# burst oGCDs (Geirskogul / Nastrond / Stardiver / Starcross / Dragonfire Dive /
# Rise of the Dragon / Wyrmwind Thrust) are line/circle AoE that the single-target
# rotation ALREADY casts -> free-splash on nearby adds, credited symmetrically on
# delivered + ceiling (RPR Communio / MCH Chain Saw pattern; the >100% guard holds).
# Conservative (lower) secondaries — under-crediting splash stays <= 100% (the safe
# direction). ⚠️ verify the falloffs live (Phase: multi-target). The dedicated AoE
# combo (Doom Spike line) lives in AOE_POTENCIES below: the AoE-aware sim forks into
# it under a MultiTargetContext schedule (simulator._AOE_MIN_TARGETS), so its
# delivered + ceiling credit is symmetric — the old "the ST sim never casts it"
# exclusion no longer applies.
SPLASH_POTENCIES: dict[int, int] = {
    GEIRSKOGUL:          140,   # 280 x 0.50
    NASTROND:            360,   # 720 x 0.50
    STARDIVER:           420,   # 840 x 0.50 (conservative; ~40% falloff -> 504)
    STARCROSS:           500,   # 1000 x 0.50 (conservative; ~40% falloff -> 600)
    DRAGONFIRE_DIVE:     250,   # 500 x 0.50
    RISE_OF_THE_DRAGON:  275,   # 550 x 0.50
    WYRMWIND_THRUST:     220,   # 440 x 0.50
}

# --- AoE potencies (the dedicated Doom Spike combo the AoE-aware sim forks into) ---
# ability_id -> per-extra-target potency. The whole line is FULL-TO-ALL (no falloff:
# each enemy takes the listed potency), so secondary == primary (== POTENCIES[id],
# combo'd values). Ids live-verified via get_metadata (86 / 25770 / 7397 / 16477
# resolve to Doom Spike / Draconian Fury / Sonic Thrust / Coerthan Torment).
AOE_POTENCIES: dict[int, int] = {
    DOOM_SPIKE:       110,
    DRACONIAN_FURY:   130,
    SONIC_THRUST:     120,   # combo'd; grants Power Surge (same as Spiral Blow)
    COERTHAN_TORMENT: 150,   # combo'd; grants Draconian Fire
}

# --- Positionals ------------------------------------------------------------
# Non-positional ("missed") potency for the positional GCDs. The delta
# (POTENCIES[id] - this = 40 each) is what a missed positional costs — priced by
# the bonus-byte detector (positionals.py) when FFLogs exposes the byte; idealized
# always uses the hit value above.
POSITIONAL_MISS_POTENCY: dict[int, int] = {
    CHAOTIC_SPRING:  300,
    WHEELING_THRUST: 300,
    FANG_AND_CLAW:   300,
}
POSITIONAL_IDS: frozenset[int] = frozenset(POSITIONAL_MISS_POTENCY)

# oGCD set — kept job-local (not read from XIVAPI) so the sim's weave logic and
# scoring's GCD/oGCD split stay hermetic under the test stub. Everything else in
# POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    LANCE_CHARGE, BATTLE_LITANY, LIFE_SURGE, GEIRSKOGUL, NASTROND, STARDIVER,
    STARCROSS, DRAGONFIRE_DIVE, RISE_OF_THE_DRAGON, HIGH_JUMP, MIRAGE_DIVE,
    WYRMWIND_THRUST, ELUSIVE_JUMP, WINGED_GLIDE,
})

# Weaponskills the sim treats as GCDs that can carry / consume the Life Surge crit
# and the combo state. (Piercing Talon is a ranged filler the uptime sim never casts.)
GCD_WEAPONSKILLS: frozenset[int] = frozenset({
    TRUE_THRUST, RAIDEN_THRUST, LANCE_BARRAGE, HEAVENS_THRUST, FANG_AND_CLAW,
    SPIRAL_BLOW, CHAOTIC_SPRING, WHEELING_THRUST, DRAKESBANE,
    DOOM_SPIKE, DRACONIAN_FURY, SONIC_THRUST, COERTHAN_TORMENT,
})

# --- Firstminds' Focus gauge (0-2) -----------------------------------------
# Generated by Raiden Thrust / Draconian Fury (the Draconian-Fire-proc GCDs, +1
# each); spent by Wyrmwind Thrust (2). At 2 stacks a 3rd is wasted (overcap). The
# LotD "gauge" of older patches is GONE in 7.x — Geirskogul activates Life of the
# Dragon directly, so Firstminds' Focus is DRG's only gauge.
FOCUS_GENERATORS: dict[int, int] = {RAIDEN_THRUST: 1, DRACONIAN_FURY: 1}
FOCUS_SPENDERS: dict[int, int] = {WYRMWIND_THRUST: 2}
FOCUS_CAP = 2
# One Focus is worth ~half a Wyrmwind Thrust (440 / 2).
FOCUS_VALUE_P_PER_UNIT: float = 220.0

# --- Cooldowns (recast_s, max_charges) -------------------------------------
# Only RECAST-gated actions live here (drift-detector-watched + sim recast). The
# state-gated burst buttons (Nastrond / Stardiver / Starcross / Mirage Dive / Rise
# of the Dragon are LotD/proc-gated; Wyrmwind Thrust is Focus-gated) are modeled as
# state flags in the simulator, so listing them would read as false drift.
COOLDOWNS: dict[int, tuple[float, int]] = {
    LANCE_CHARGE:    (60.0, 1),
    BATTLE_LITANY:  (120.0, 1),
    LIFE_SURGE:      (40.0, 2),
    GEIRSKOGUL:      (60.0, 1),
    DRAGONFIRE_DIVE:(120.0, 1),
    HIGH_JUMP:       (30.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    LANCE_CHARGE:    900,   # +10% over a 20s burst window (priced via enablers)
    BATTLE_LITANY:     0,   # crit only -> no own-potency value (party buff)
    LIFE_SURGE:      285,   # ~the crit uplift on a 460p finisher (priced via enablers)
    GEIRSKOGUL:     3200,   # LotD (+15%) + 3 Nastrond + Stardiver + Starcross (via enablers)
    DRAGONFIRE_DIVE: 500,   # + Rise of the Dragon follow-up
    HIGH_JUMP:       400,   # + Mirage Dive follow-up
}

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) --------
# DT 7.2 standard opener — the Chaotic Spring combo first (Power Surge up early),
# then the Heavens' Thrust combo. ⚠️ refine against a current guide / live probe.
CANONICAL_OPENER: tuple[int, ...] = (
    TRUE_THRUST, SPIRAL_BLOW, CHAOTIC_SPRING, WHEELING_THRUST, DRAKESBANE,
    RAIDEN_THRUST, LANCE_BARRAGE, HEAVENS_THRUST, FANG_AND_CLAW, DRAKESBANE,
    RAIDEN_THRUST, SPIRAL_BLOW,
)

# --- Detection exclusions ---------------------------------------------------
CLIP_EXCLUSIONS: frozenset[int] = frozenset()   # no reduced-GCD window (no haste)
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()  # COOLDOWNS lists only recast-gated

# --- Chaotic Spring DoT -----------------------------------------------------
# Snapshots buffs at cast; scored by time-to-next-cast, capped at the 24s duration,
# so an early refresh credits less (symmetric / overcap-safe — the SAM Higanbana model).
CHAOTIC_SPRING_DOT_TICK_P: int = 45
CHAOTIC_SPRING_DOT_TICK_S: float = 3.0
CHAOTIC_SPRING_DOT_DURATION_S: float = 24.0
# At a lone refreshable DoT the beam forks [refresh now via the DoT combo, keep the
# raw combo] once the DoT has <= this left — the search picks the cadence itself.
CHAOTIC_SPRING_REFRESH_AT_S: float = 9.0

# --- Burst-alignment abilities (AlignmentAspect watches these) -------------
BURST_ABILITIES: frozenset[int] = frozenset({
    LANCE_CHARGE, BATTLE_LITANY, GEIRSKOGUL, DRAGONFIRE_DIVE, LIFE_SURGE,
})

# Enablers whose value is throughput/burst, not standalone potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values). Geirskogul is the big
# one (LotD + the Nastrond/Stardiver/Starcross chain); Lance Charge + Life Surge are
# the self-buff windows the sim places.
ENABLER_IDS: tuple[int, ...] = (GEIRSKOGUL, LANCE_CHARGE, LIFE_SURGE)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Dragoon",
    # The PositionalAspect reads the player's DamageDone bonus byte every pull — bundle it.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="focus",
            generators=FOCUS_GENERATORS,
            spenders=FOCUS_SPENDERS,
            cap=FOCUS_CAP,
            value_p_per_unit=FOCUS_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),          # no cross-cooldown reductions
    charge_sharing={},     # no shared-charge pools
    raid_buffs={},         # Battle Litany modeled via buff_windows (job-agnostic; crit-neutral here)
    role_policy=MELEE_DPS,
    # A dropped GCD backfills with a mid combo hit (~300-460).
    filler_gcd_potency=340,
    # Tincture: melee-DPS Strength (party-comp-inclusive, from xivgear); same tier
    # stat/slope as Reaper/Samurai. ⚠️ refine per tier via scripts/calibrate_tincture.py.
    tincture_main_stat=6841,
    tincture_role_coeff=237,
    # Pure melee with gap-closer JUMPS (no ranged GCD bridge) -> forced disconnects are
    # disengages (like Viper/Paladin), NOT an RPR-Harpe ranged filler.
    ranged_filler_id=None,
)
