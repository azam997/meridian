"""Gunbreaker data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

The analyzer's third TANK (after Paladin and Warrior) and its twelfth full idealized
simulator. Single source of truth for GNB numbers: potencies, the recast-gated
cooldowns, the Powder Gauge (cartridge) economy, the combo / Continuation machine,
the No Mercy burst self-buff, and the two snapshotted DoTs (Sonic Break / Bow Shock).

What makes GNB structurally distinct from the tanks already shipped:

  * **The Powder Gauge is an OFFENSIVE cartridge resource** (like WAR's Beast), but it
    feeds a *spend-cadence fork*: at a combo boundary the rotation chooses which
    cartridge spender to play (Burst Strike / Gnashing Fang / Double Down) or whether to
    keep building — and whether to bank a cartridge for the next No Mercy. That fork is
    why GNB needs the beam search (the SAM/DRG pattern); see simulator.py. The cap is
    normally 3 but **Bloodfest temporarily raises it to 6 for 30s** (the Patch 7.4
    rework, 2025-12-16) — modeled with a sim-internal `bloodfest_cap_end` timer, since
    the static `GaugeModel.cap` can't express a dynamic cap.
  * **No Mercy is a recurring BURST self-buff** (+20% for 20s, every 60s), modeled the
    Paladin-FoF / Dragoon-Lance-Charge way: the window is DERIVED from the No Mercy casts
    in the timeline itself and FOLDED INTO the incremental score (scoring.py), symmetric
    on delivered + idealized, so a late/dropped No Mercy or GCDs lost under it cost
    efficiency and a perfectly-used one cancels in the ratio (the >100% guard holds). It
    is NOT a flat full-coverage overlay (No Mercy is only ~1/3 uptime).
  * **Continuations** (Hypervelocity / Jugular Rip / Abdomen Tear / Eye Gouge / Fated
    Brand) are forced oGCDs that proc off the preceding GCD — emitted deterministically in
    `pick_ogcd`, at top priority so they never lose weave budget.
  * **Sonic Break + Bow Shock DoTs** are 15s (NOT SAM's 30s), scored by time-to-next
    capped at 15s, snapshotting the No Mercy multiplier at cast (the DRG Chaotic Spring
    model). Sonic Break is a GCD gated by Ready to Break (from No Mercy); Bow Shock an oGCD.
  * **No guaranteed-crit ability** (the Reign of Beasts chain effect text is plain
    potency) ⇒ no crit-DH calibration, unlike WAR.

ACTION IDS are VERIFIED three ways (2026-07-01 audit): probed from live top-GNB logs
(scripts/probe_gunbreaker_ids.py), cross-checked against the real-pull fixture cast
streams (tests/fixtures/gnb/), and XIVAPI-confirmed (names/icons/categories now bundled
in jobs/_core/ability_metadata.py). POTENCIES + mechanics match live Patch 7.51 exactly
— every value below is the post-7.4-rework state (Bloodfest 60s + the 3->6 cap window,
2-charge Gnashing Fang, 15s/120-tick Sonic Break DoT, the -20 continuation nerfs, Double
Down 1000/2-cart, Reign 800/900/1000), confirmed against the Lodestone job guide and
consolegameswiki patch histories. Still per-tier: the tincture stat (calibrate_tincture.py).
"""
from __future__ import annotations

from jobs._core.job import MELEE_TANK, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100; verify live) --------------------------

# Single-target combo
KEEN_EDGE         = 16137    # combo starter
BRUTAL_SHELL      = 16139    # 2nd combo hit; heal + barrier
SOLID_BARREL      = 16145    # 3rd combo hit; +1 Cartridge
# Cartridge spenders
BURST_STRIKE      = 16162    # -1 cart; grants Ready to Blast -> Hypervelocity
GNASHING_FANG     = 16146    # -1 cart; 30s / 2 charges; starts Gnashing combo; -> Jugular Rip
SAVAGE_CLAW       = 16147    # Gnashing combo 2 (from Gnashing Fang); -> Abdomen Tear
WICKED_TALON      = 16150    # Gnashing combo 3 (from Savage Claw); -> Eye Gouge
DOUBLE_DOWN       = 25760    # -2 carts; 60s; AoE (15% falloff), used in ST burst
SONIC_BREAK       = 16153    # GCD; requires Ready to Break (from No Mercy); 340 + 15s DoT
# Reign of Beasts combo (level 100; from Bloodfest's Ready to Reign)
REIGN_OF_BEASTS   = 36937    # requires Ready to Reign; -> Noble Blood
NOBLE_BLOOD       = 36938    # Reign combo 2; -> Lion Heart
LION_HEART        = 36939    # Reign combo 3 (capstone)
# AoE GCD line (cast only in multi-target windows)
DEMON_SLICE       = 16141    # AoE starter
DEMON_SLAUGHTER   = 16149    # AoE 2nd; +1 Cartridge
FATED_CIRCLE      = 16163    # AoE cartridge spender (-1 cart); grants Ready to Raze -> Fated Brand
# Ranged filler (not part of the uptime rotation)
LIGHTNING_SHOT    = 16143    # ranged filler weaponskill

# oGCDs — burst + damage
NO_MERCY          = 16138    # 60s; +20% damage for 20s; grants Ready to Break
BLOODFEST         = 16164    # 60s (7.4; was 120s); +3 carts; cap -> 6 (30s); grants Ready to Reign
BLASTING_ZONE     = 16165    # 30s; direct-damage oGCD (upgrade of Danger Zone)
DANGER_ZONE       = 16144    # pre-80 Blasting Zone (kept for id mapping on lower-level logs)
BOW_SHOCK         = 16159    # 60s; AoE oGCD; 150 + 15s DoT
# Continuation oGCDs (proc off the preceding GCD; forced)
CONTINUATION      = 16155    # the metamorphic parent (becomes the below by proc)
HYPERVELOCITY     = 25759    # after Burst Strike (Ready to Blast)
JUGULAR_RIP       = 16156    # after Gnashing Fang (Ready to Rip)
ABDOMEN_TEAR      = 16157    # after Savage Claw (Ready to Tear)
EYE_GOUGE         = 16158    # after Wicked Talon (Ready to Gouge)
FATED_BRAND       = 36936    # after Fated Circle (Ready to Raze) — probe-verified (was swapped w/ Great Nebula)

# Defensive / utility (NO damage value; excluded from the DPS diff via isDefensive)
CAMOUFLAGE        = 16140
ROYAL_GUARD       = 16142    # tank stance
RELEASE_ROYAL_GUARD = 32068
NEBULA            = 16148
GREAT_NEBULA      = 36935    # upgrade of Nebula (defensive) — probe-verified (was swapped w/ Fated Brand)
AURORA            = 16151    # regen, 2 charges
SUPERBOLIDE       = 16152    # invuln
HEART_OF_LIGHT    = 16160    # party mitigation
HEART_OF_STONE    = 16161
HEART_OF_CORUNDUM = 25758    # upgrade of Heart of Stone
TRAJECTORY        = 36934    # gap closer (mobility; replaced Rough Divide), 2 charges

DEFENSIVE_IDS: frozenset[int] = frozenset({
    CAMOUFLAGE, ROYAL_GUARD, RELEASE_ROYAL_GUARD, NEBULA, GREAT_NEBULA, AURORA,
    SUPERBOLIDE, HEART_OF_LIGHT, HEART_OF_STONE, HEART_OF_CORUNDUM, TRAJECTORY,
})

# --- Status (buff/debuff) IDs ----------------------------------------------
# Not load-bearing for v1 scoring (No Mercy + the DoTs are derived from CAST ids,
# not statuses) — kept for documentation / future coverage reconstruction. ⚠ verify live.
# Probe-verified (scripts/probe_gunbreaker_ids.py, live M11S log, FFLogs game-status ids).
NO_MERCY_STATUS_ID       = 1831    # +20% damage (verified)
READY_TO_BREAK_STATUS_ID = 3886    # enables Sonic Break (verified)
READY_TO_REIGN_STATUS_ID = 3840    # enables the Reign of Beasts combo (verified)
READY_TO_BLAST_STATUS_ID = 2686    # enables Hypervelocity (verified)
READY_TO_RIP_STATUS_ID   = 1842    # enables Jugular Rip (verified)
READY_TO_TEAR_STATUS_ID  = 1843    # enables Abdomen Tear (verified)
READY_TO_GOUGE_STATUS_ID = 1844    # enables Eye Gouge (verified)
READY_TO_RAZE_STATUS_ID  = 3839    # enables Fated Brand (⚠ unverified — no Fated Circle in ST logs)
SONIC_BREAK_DOT_STATUS_ID = 1837   # ⚠ verify
BOW_SHOCK_DOT_STATUS_ID   = 1838   # ⚠ verify

# --- No Mercy self-buff ----------------------------------------------------
# The +20% burst window the player places. scoring.py derives the window from the
# No Mercy casts in the timeline (symmetric on delivered + idealized), so a late /
# dropped No Mercy or GCDs lost under it cost efficiency. NOT a full-coverage overlay.
NO_MERCY_MULT: float       = 1.20
NO_MERCY_DURATION_S: float = 20.0
# Ready to Break (granted by No Mercy, enables Sonic Break) expires after 30s —
# the sim gates Sonic Break on it so a line can't hold the cast past the window.
READY_TO_BREAK_DURATION_S: float = 30.0
# The standard FFXIV combo timer: a started combo (basic / Gnashing / Reign chain)
# is lost if the next step doesn't land within this window. Only binds across
# downtime in-sim (the rotation never idles 30s mid-combo on its own).
COMBO_TIMEOUT_S: float = 30.0

# --- Potencies --------------------------------------------------------------
# action_id -> base potency (no buffs / crit modeling). Combo abilities carry their
# COMBO'd value. The DoTs are scored separately (scoring._dot_potency). Wiki-verified
# (ffxiv.consolegameswiki.com, level 100).
POTENCIES: dict[int, int] = {
    # Single-target combo
    KEEN_EDGE:        300,
    BRUTAL_SHELL:     380,   # combo'd
    SOLID_BARREL:     460,   # combo'd; +1 cart
    # Cartridge spenders
    BURST_STRIKE:     420,
    GNASHING_FANG:    440,
    SAVAGE_CLAW:      500,   # combo from Gnashing Fang
    WICKED_TALON:     560,   # combo from Savage Claw
    DOUBLE_DOWN:     1000,   # 15% falloff to additional targets
    SONIC_BREAK:      340,   # + DoT (scored separately)
    # Reign of Beasts combo
    REIGN_OF_BEASTS:  800,   # 60% falloff to additional targets
    NOBLE_BLOOD:      900,
    LION_HEART:      1000,
    # oGCDs
    BLASTING_ZONE:    800,
    DANGER_ZONE:      250,
    BOW_SHOCK:        150,   # + DoT (scored separately)
    # Continuations (oGCD)
    HYPERVELOCITY:    180,
    JUGULAR_RIP:      220,
    ABDOMEN_TEAR:     260,
    EYE_GOUGE:        300,
    FATED_BRAND:      120,
    # AoE line — PRIMARY potency (multi-target windows only)
    DEMON_SLICE:      100,
    DEMON_SLAUGHTER:  160,   # combo'd; +1 cart
    FATED_CIRCLE:     300,
    # Ranged filler (not in the uptime rotation; primary only)
    LIGHTNING_SHOT:   150,
}

# --- Splash (free-splash secondary potency) --------------------------------
# ability_id -> potency dealt to EACH additional target beyond the primary, for the
# innately-cleaving casts the SINGLE-TARGET rotation already makes (Double Down, the
# Reign chain, Bow Shock). Credited symmetrically on delivered + ceiling when a pull
# affords multi-target (the WAR/DRG free-splash pattern; the >100% guard holds).
# Falloff baked in. ⚠ verify falloffs live.
SPLASH_POTENCIES: dict[int, int] = {
    DOUBLE_DOWN:     850,   # 1000 x 0.85 (15% falloff)
    REIGN_OF_BEASTS: 320,   # 800 x 0.40 (60% falloff)
    NOBLE_BLOOD:     360,   # 900 x 0.40
    LION_HEART:      400,   # 1000 x 0.40
    BOW_SHOCK:       150,   # full to all nearby
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts at N>=3) --
# Full-to-all (no falloff on the dedicated AoE line). Only consumed when the
# multi-target fork is active; the single-target sim never casts these.
# Fated Brand is the line's forced continuation oGCD ("120 to all nearby
# enemies", wiki-verified 2026-07-16) — listed so both delivered and the
# ceiling credit its cleave alongside Fated Circle's.
AOE_POTENCIES: dict[int, int] = {
    DEMON_SLICE:     100,
    DEMON_SLAUGHTER: 160,
    FATED_CIRCLE:    300,
    FATED_BRAND:     120,
}

# oGCD set — kept job-local and MIRRORED into ability_metadata.BUNDLED (which is what
# the Clipping aspect + GCD-speed inference actually read), so the GCD/oGCD split stays
# hermetic under the test stub. Everything else in POTENCIES is a GCD.
# test_gunbreaker_sim.test_ability_metadata_bundled pins the mirror.
OGCD_IDS: frozenset[int] = frozenset({
    NO_MERCY, BLOODFEST, BLASTING_ZONE, DANGER_ZONE, BOW_SHOCK,
    CONTINUATION, HYPERVELOCITY, JUGULAR_RIP, ABDOMEN_TEAR, EYE_GOUGE, FATED_BRAND,
    CAMOUFLAGE, NEBULA, GREAT_NEBULA, AURORA, SUPERBOLIDE, HEART_OF_LIGHT,
    HEART_OF_STONE, HEART_OF_CORUNDUM, TRAJECTORY, ROYAL_GUARD, RELEASE_ROYAL_GUARD,
})

# The continuation oGCD each "Ready to X" GCD enables (set as a flag in apply_cast,
# emitted by pick_ogcd). Maps the enabling GCD -> its forced continuation.
CONTINUATION_AFTER: dict[int, int] = {
    BURST_STRIKE:  HYPERVELOCITY,
    GNASHING_FANG: JUGULAR_RIP,
    SAVAGE_CLAW:   ABDOMEN_TEAR,
    WICKED_TALON:  EYE_GOUGE,
    FATED_CIRCLE:  FATED_BRAND,
}

# --- Powder Gauge (cartridges) ---------------------------------------------
# Generated by Solid Barrel / Demon Slaughter (+1 each combo finisher) and Bloodfest
# (+3 instant). Spent by Burst Strike / Gnashing Fang / Fated Circle (1) and Double
# Down (2). Cap is normally 3; Bloodfest raises it to 6 for 30s (handled in the
# simulator via `bloodfest_cap_end`). Overcapping a cartridge is wasted potency.
CARTRIDGE_GENERATORS: dict[int, int] = {
    SOLID_BARREL:    1,
    DEMON_SLAUGHTER: 1,
    BLOODFEST:       3,
}
CARTRIDGE_SPENDERS: dict[int, int] = {
    BURST_STRIKE:  1,
    GNASHING_FANG: 1,
    FATED_CIRCLE:  1,
    DOUBLE_DOWN:   2,
}
CARTRIDGE_CAP = 3
CARTRIDGE_CAP_BLOODFEST = 6
BLOODFEST_CAP_DURATION_S: float = 30.0
# Overcap penalty: potency lost per wasted cartridge ~ the lowest-value spender
# (Burst Strike). Used by the OvercapAspect detector (delivered side).
CARTRIDGE_VALUE_P_PER_UNIT: float = 420.0

# --- Cooldowns (recast_s, max_charges) -------------------------------------
# Only RECAST-gated actions live here (drift-detector-watched + sim recast). The
# state-gated GCDs (Sonic Break = Ready to Break; the Reign chain = Ready to Reign;
# the continuations = their Ready procs) are modeled as flags in the simulator, so
# listing them would read as false drift.
COOLDOWNS: dict[int, tuple[float, int]] = {
    NO_MERCY:      (60.0, 1),
    BLOODFEST:     (60.0, 1),   # 60s since 7.4 (fixture-verified: gaps 60.0-63.8s live)
    GNASHING_FANG: (30.0, 2),
    DOUBLE_DOWN:   (60.0, 1),
    BLASTING_ZONE: (30.0, 1),
    BOW_SHOCK:     (60.0, 1),
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    NO_MERCY:      1800,   # +20% over a 20s burst window (priced via enablers)
    BLOODFEST:     2700,   # 3 carts -> spenders + the Reign chain (via enablers)
    GNASHING_FANG: 1200,   # 440 + Savage Claw + Wicked Talon + 3 continuations
    DOUBLE_DOWN:   1000,
    BLASTING_ZONE:  800,
    BOW_SHOCK:      450,   # 150 + 300 DoT
}

# --- Canonical opener (diagnostic only; OpenerAspect is zero-priced) --------
# Standard DT GNB opener GCD sequence (Bloodfest pre-pull -> No Mercy -> dump). The
# oGCDs (No Mercy, Bloodfest, continuations) weave around these. ⚠ refine vs a live probe.
CANONICAL_OPENER: tuple[int, ...] = (
    KEEN_EDGE, BRUTAL_SHELL, SOLID_BARREL, GNASHING_FANG, SAVAGE_CLAW, WICKED_TALON,
    SONIC_BREAK, BURST_STRIKE, DOUBLE_DOWN, REIGN_OF_BEASTS, NOBLE_BLOOD, LION_HEART,
)

# --- Detection exclusions ---------------------------------------------------
CLIP_EXCLUSIONS: frozenset[int] = frozenset()   # no reduced-GCD window (no haste)
DRIFT_EXCLUSIONS: frozenset[int] = frozenset()  # COOLDOWNS lists only recast-gated

# --- DoTs -------------------------------------------------------------------
# Both 15s (NOT SAM's 30s). Snapshot the No Mercy + raid multiplier at cast; scored by
# time-to-next-cast capped at the duration (the SAM Higanbana / DRG Chaotic Spring model).
SONIC_BREAK_DOT_TICK_P: int = 120
SONIC_BREAK_DOT_TICK_S: float = 3.0
SONIC_BREAK_DOT_DURATION_S: float = 15.0
BOW_SHOCK_DOT_TICK_P: int = 60
BOW_SHOCK_DOT_TICK_S: float = 3.0
BOW_SHOCK_DOT_DURATION_S: float = 15.0

# --- Burst-alignment abilities (AlignmentAspect watches these) -------------
BURST_ABILITIES: frozenset[int] = frozenset({
    NO_MERCY, BLOODFEST, GNASHING_FANG, DOUBLE_DOWN, BLASTING_ZONE, BOW_SHOCK,
    SONIC_BREAK, REIGN_OF_BEASTS,
})

# Enablers whose value is throughput/burst, not standalone potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values). No Mercy is the big one
# (the +20% window); Bloodfest fuels the carts + the Reign chain.
ENABLER_IDS: tuple[int, ...] = (NO_MERCY, BLOODFEST, GNASHING_FANG, DOUBLE_DOWN)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Gunbreaker",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            # Name matches the SimState field (`cartridges`) so the shared
            # entry_gauge.seed_entry_gauge (GaugeModel.name == field convention) can
            # seed carried cartridges on phase-continuation pulls (M12S-P2).
            name="cartridges",
            generators=CARTRIDGE_GENERATORS,
            spenders=CARTRIDGE_SPENDERS,
            cap=CARTRIDGE_CAP,
            value_p_per_unit=CARTRIDGE_VALUE_P_PER_UNIT,
            # Bloodfest temporarily raises the cap 3 -> 6 for 30s; the overcap
            # detector measures against 6 during the window and prices any
            # bonus cartridges let to expire (mirrors the simulator's clamp).
            cap_boosts={BLOODFEST: (CARTRIDGE_CAP_BLOODFEST,
                                    BLOODFEST_CAP_DURATION_S)},
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),          # no cross-cooldown reductions
    charge_sharing={},     # no shared-charge pools
    raid_buffs={},         # job-agnostic; modeled via buff_windows
    role_policy=MELEE_TANK,
    # A dropped GCD backfills with a Solid Barrel combo finisher (~460).
    filler_gcd_potency=460,
    # Tincture: tank Strength (party-comp-inclusive, from xivgear); same tier
    # stat/slope as Paladin/Warrior. ⚠ refine per tier via scripts/calibrate_tincture.py.
    tincture_main_stat=6386,
    tincture_role_coeff=190,
    # Pure melee with a gap-closer (Trajectory); forced disconnects are disengages
    # (like PLD/WAR), NOT an RPR-Harpe ranged filler.
    ranged_filler_id=None,
)
