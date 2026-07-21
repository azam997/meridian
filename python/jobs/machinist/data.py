"""Machinist data tables (Dawntrail 7.x) + the `JOB_DATA` JobData instance.

Single source of truth for MCH numbers: potencies, cooldowns, gauge rules,
Queen ability potencies, conversion rates, procs. Values cross-checked
against ffxiv.consolegameswiki.com/wiki/Machinist and the MCH-expert
review captured in PLAN_MCH_EXECUTION.md.

Pinned to FFXIV patch series 7.x. If the analyzer is ever run against a
log from a different patch, `PATCH_VERSION` is the place to gate.
"""
from __future__ import annotations

from jobs._core.job import PHYSICAL_RANGED, CDRRule, GaugeModel, JobData


PATCH_VERSION = "7.x"

# Reassemble (action 2876) applies the "Reassembled" buff. Used to reconstruct a
# clipped pre-pull Reassemble for the Timeline (the instant precast during the
# countdown is dropped by FFLogs, but this buff survives — pre-applied — into the
# fight). ⚠️ verify the status id against a live log.
REASSEMBLED_STATUS_ID = 851

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no buffs / combo / crit modeling).

POTENCIES: dict[int, int] = {
    # Heated combo
    7411:  220,   # Heated Split Shot
    7412:  320,   # Heated Slug Shot   (combo'd)
    7413:  420,   # Heated Clean Shot  (combo'd)
    # Cooldown tools — all 660p in 7.x
    16498: 660,   # Drill
    16500: 660,   # Air Anchor
    25788: 660,   # Chain Saw
    # Proc-gated GCDs
    36981: 660,   # Excavator (granted by Chain Saw)
    36982: 900,   # Full Metal Field (granted by Barrel Stabilizer; guaranteed crit-DH)
    # Flamethrower — channeled GCD, 120p per tick (7.3+). NOT part of the
    # rotation: a real GCD always out-potencies it in uptime. We only ever
    # credit ONE tick, for the niche downtime-edge squeeze (cast as the boss
    # goes untargetable so a tick lands in the retarget gap). The sim emits it
    # solely at downtime boundaries; see simulator._in_downtime handling.
    7418:  120,   # Flamethrower (single squeezed tick)
    # Hypercharge sub-rotation
    36978: 240,   # Blazing Shot (220 base + 20 from "Overheated"; combined here)
    # Ranged oGCDs
    36979: 180,   # Double Check
    36980: 180,   # Checkmate
    # AoE line (the AoE-aware sim casts these in multi-target windows; the ST
    # rotation never does, so they scored 0 until now). ⚠️ DT 7.x best-effort,
    # verify-live (Phase 5).
    25786: 130,   # Scattergun     (AoE combo filler; +10 heat; replaces the Heated combo)
    16497: 180,   # Auto Crossbow  (Overheated AoE spender; replaces Blazing Shot)
    16499: 100,   # Bioblaster     (AoE tool, shares Drill charges; 50 direct + 50 DoT/15s)
    # Pet finisher — modeled separately via pet damage events on the Queen aspect.
    25787: 0,     # Crowned Collider (pet ability; do not double-count here)
    # Wildfire payload — total damage = hits × 240, capped at 6 hits (max 1440).
    # VERIFIED (scripts/probe_mch_ids.py, 25 top parses): the detonation damage
    # event carries abilityGameID 1000861 (FFLogs' status-damage encoding of the
    # Wildfire debuff, status 861) — exactly one per Wildfire cast; the old
    # best-guess action 11638 never occurs. Neither ID ever appears in a CASTS
    # stream, so the scorer prices the payload off the Wildfire cast (2878)
    # itself and no potency entry is needed here.
}

# --- Splash (multi-target secondary potency) -------------------------------
# ability_id -> potency dealt to EACH additional target beyond the primary.
# MCH stays on its single-target rotation even with 2-3 targets, but several
# of its tools cleave, so it IS slightly undercounted on fights like M10S.
# All FREE-SPLASH: the idealized ST sim already casts them, so crediting their
# splash on both delivered and the ceiling is symmetric (the >100% guard holds).
#
# CALIBRATED from a live M10S top-parse probe (2026-05-30): the dominant
# cleavers are Double Check (~26-32 casts/pull) and Checkmate (~27-32) — the
# spammed ranged oGCDs — NOT the tools. Double Check splashes after all
# (corrected: it's NOT single-target). Secondary potency = primary × measured
# secondary/primary unmitigated ratio (~0.7). maxN observed = 2. See
# [[multitarget-live-findings]].
SPLASH_POTENCIES: dict[int, int] = {
    36979: 126,   # Double Check (180 × ~0.70)
    36980: 126,   # Checkmate    (180 × ~0.70)
    25788: 495,   # Chain Saw    (660 × 0.75, 25% falloff — wiki-verified)
    36981: 495,   # Excavator    (660 × 0.75, 25% falloff — wiki-verified)
    36982: 675,   # Full Metal Field (900 × 0.75, 25% falloff)
    7418:  90,    # Flamethrower (120 × ~0.75, cone per squeezed tick)
}

# --- AoE potencies (dedicated AoE buttons the AoE-aware sim casts) ----------
# ability_id -> per-extra-target potency. MCH's AoE line is full-to-all (no
# falloff): secondary == primary (== POTENCIES[id]). Chain Saw / Excavator /
# FMF already cleave and live in SPLASH_POTENCIES (free-splash the ST sim casts);
# these are the dedicated AoE buttons the ST sim does NOT cast. ⚠️ verify-live.
AOE_POTENCIES: dict[int, int] = {
    25786: 130,   # Scattergun     (wiki 7.2: 130, full-to-all)
    16497: 180,   # Auto Crossbow  (wiki: 180, full-to-all)
    16499: 100,   # Bioblaster     (50 direct + 50 DoT/15s, full-to-all)
}

# --- AoE reach overrides (yalms) for the advisory cleave-geometry verdicts ---
# Only abilities whose reach exceeds the standard 5y splash circle. Lines/cones
# stored as circles of their length — generous on purpose (the advisory only
# auto-denies a window when NOTHING in the kit could ever reach; see
# jobs._core.cleave_geometry). No potency/ceiling math reads these.
AOE_RADII_YALM: dict[int, float] = {
    25788: 25.0,  # Chain Saw     (25y line)
    36981: 25.0,  # Excavator     (25y line)
    25786: 12.0,  # Scattergun    (12y cone)
    16497: 12.0,  # Auto Crossbow (12y cone)
    16499: 12.0,  # Bioblaster    (12y cone)
    7418:   8.0,  # Flamethrower  (8y cone)
}

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges)

COOLDOWNS: dict[int, tuple[float, int]] = {
    2876:  (55.0, 2),   # Reassemble
    16498: (20.0, 2),   # Drill — charges SHARED with Bioblaster (see quirk #11)
    16500: (40.0, 1),   # Air Anchor
    25788: (60.0, 1),   # Chain Saw
    36979: (30.0, 3),   # Double Check  — recast reduced 15s per Blazing Shot
    36980: (30.0, 3),   # Checkmate     — recast reduced 15s per Blazing Shot
    7414:  (120.0, 1),  # Barrel Stabilizer
    2878:  (120.0, 1),  # Wildfire
    17209: (10.0, 1),   # Hypercharge — gated by 50 heat OR Hypercharged buff
    16889: (90.0, 1),   # Tactician (utility; excluded from drift — see DRIFT_EXCLUSIONS)
}

# Per-cast cooldown reduction that Blazing Shot applies to Double Check
# and Checkmate. Source: in-game tooltip (wiki-confirmed).
BLAZING_SHOT_CDR_S: float = 15.0

# Bioblaster (AoE GCD) shares Drill's charge pool. Drift detection for Drill
# must count Bioblaster casts as charge consumptions or AoE-heavy phases
# read as fake Drill drift. See quirk #11 in PLAN_MCH_EXECUTION.md.
BIOBLASTER_ABILITY_ID: int = 16499
# AoE-line action IDs the AoE-aware sim swaps in (multi-target windows).
SCATTERGUN_ABILITY_ID: int = 25786      # AoE combo filler (Spread Shot's Lv82 upgrade); XIVAPI-verified (36977 was Radiant Encore, a DNC action)
AUTO_CROSSBOW_ABILITY_ID: int = 16497   # AoE Overheated spender (AoE Blazing Shot)

# Per-cast value used by the cooldown-drift detector. Distinct from POTENCIES
# (damage-event scoring) because cooldown abilities like Reassemble or
# Hypercharge don't directly damage — their per-cast value is the lost
# potential when the cast is skipped. Approximate; refine alongside Phase D
# alignment math (M5).
COOLDOWN_VALUE_P: dict[int, int] = {
    # Direct-damage cooldown tools — equal to their potency
    16498: 660,    # Drill
    16500: 660,    # Air Anchor
    25788: 660,    # Chain Saw (proc value tracked separately via Excavator)
    36979: 180,    # Double Check
    36980: 180,    # Checkmate
    # Cooldown abilities — derived per-cast value
    2876:  200,    # Reassemble — ~30% crit-DH bonus on a 660p tool
    2878:  1440,   # Wildfire — full 6 × 240 weaponskill payload
    7414:  2100,   # Barrel Stabilizer — grants FMF (900) + free Hypercharge (1200)
    17209: 1200,   # Hypercharge — full 5 × 240 Blazing Shot chain
    16889: 0,      # Tactician — utility, no DPS value
}

# --- Heat gauge -------------------------------------------------------------

HEAT_GENERATORS: dict[int, int] = {
    7411: 5,  # Heated Split Shot
    7412: 5,  # Heated Slug Shot
    7413: 5,  # Heated Clean Shot
    # Scattergun builds MORE heat per GCD than the ST combo — the gauge
    # divergence that makes the AoE-vs-ST combo a genuine beam fork (extra heat
    # -> extra Hypercharge -> extra Auto Crossbow). ⚠️ verify-live (Phase 5).
    25786: 10,  # Scattergun
}
HEAT_SPENDERS: dict[int, int] = {17209: 50}  # Hypercharge
HEAT_CAP = 100
HYPERCHARGE_MIN_HEAT = 50

# --- Battery gauge ----------------------------------------------------------

BATTERY_GENERATORS: dict[int, int] = {
    16500: 20,  # Air Anchor
    25788: 20,  # Chain Saw
    36981: 20,  # Excavator
    7413:  10,  # Heated Clean Shot
}
# Queen consumes all battery, minimum 50.
BATTERY_SPENDERS: dict[int, str] = {16501: "all"}
BATTERY_CAP = 100
QUEEN_MIN_BATTERY = 50

# --- Queen autonomous sequence ---------------------------------------------
# Used to derive the battery → potency conversion rate.
# At 50 battery: 5×120 + 240 + 680 + 780 = 2,300p   → 46.0p / battery
# At 100 battery: 5×240 + 480 + 1360 + 1560 = 4,600p → 46.0p / battery
# Linear; constant rate regardless of battery held at cast.

QUEEN_POTENCY_BASE: dict[str, int] = {
    # Base potency at 50 battery. Max potency at 100 battery is 2× (linear).
    # Action IDs TBD for arm_punch / roller_dash / pile_bunker — only
    # crowned_collider (25787) is currently confirmed via the Queen aspect.
    "arm_punch":        120,   # max 240
    "roller_dash":      240,   # max 480
    "pile_bunker":      680,   # max ~1360 (inferred 2× scaling)
    "crowned_collider": 780,   # max ~1560 (inferred 2× scaling)
}

QUEEN_SEQUENCE_COUNTS: dict[str, int] = {
    "arm_punch":        5,
    "roller_dash":      1,
    "pile_bunker":      1,
    "crowned_collider": 1,
}

# --- Resource economy conversion rates -------------------------------------
# Both rates are deterministic, derived per PLAN_MCH_EXECUTION.md.

# 50 heat → 1 Hypercharge → 5 × Blazing Shot @ 240p = 1,200p
HEAT_VALUE_P_PER_UNIT: float = 24.0

# Derived from QUEEN_POTENCY_BASE × QUEEN_SEQUENCE_COUNTS (see plan).
# Constant whether battery is held at 50 or 100 at cast.
BATTERY_VALUE_P_PER_UNIT: float = 46.0

# --- Guaranteed crit-direct-hit multiplier ---------------------------------
# Scoring is in raw potency precisely so ordinary crit/DH variance averages
# out in the ideal-vs-delivered comparison. The exception is abilities that
# *guarantee* a critical direct hit — Reassemble's buffed weaponskill and Full
# Metal Field's innate guarantee — which deterministically beat the
# non-crit/non-DH floor and so need a concrete multiplier.
#
# Empirically derived from real top-parse tool damage (Drill / Air Anchor /
# Chain Saw / Excavator), buff-normalized via each damage event's `multiplier`:
#   crit_mult ≈ 1.62 at current gear; DH is a fixed ×1.25  →  ≈ 2.03.
# Recompute on a new gear tier with:  python scripts/calibrate_crit_dh.py
# (crit scales slowly, so a re-run per major tier is plenty).
GUARANTEED_CRIT_DH_MULT: float = 2.03

# --- Procs ------------------------------------------------------------------
# action_id that grants -> status_id of the granted "ready" buff.
# Correction over earlier draft: Full Metal Field is granted by Barrel
# Stabilizer, NOT by Excavator. The two procs are parallel, not sequential.

PROC_GRANTED_BY: dict[int, int] = {
    25788: 36981,   # Chain Saw          → Excavator Ready
    7414:  36982,   # Barrel Stabilizer  → Full Metal Machinist (makes FMF castable)
}
PROC_DURATION_S: float = 30.0

# Barrel Stabilizer additionally grants Hypercharged (a free Hypercharge,
# bypassing the 50-heat cost). Tracked separately because it modifies
# Hypercharge's resource cost rather than its cooldown.
BARREL_STABILIZER_GRANTS_FREE_HYPERCHARGE: bool = True

# --- Raid buff statuses (Phase D / M5) -------------------------------------
# Filled in at M5 when raid-buff alignment lands. Conservative set; numbers
# will be approximations applied uniformly to all runs (idealized / refs /
# user) so deltas remain meaningful even if absolute multipliers drift.
# status_id -> (name, base_duration_s, dmg_multiplier)

RAID_BUFFS: dict[int, tuple[str, float, float]] = {
    # populated at M5
}


# --- Canonical opener -------------------------------------------------------
# First 12 in-fight GCDs in their expected order. Air-Anchor-first per
# MCH-expert input (CDs rolling > buff alignment, quirk #13). Moved from
# jobs/execution.py during the Phase 2 refactor.
CANONICAL_OPENER: tuple[int, ...] = (
    16500,   # Air Anchor (buffed by pre-pull Reassemble)
    7411,    # Heated Split Shot
    16498,   # Drill (1st charge)
    7412,    # Heated Slug Shot
    25788,   # Chain Saw
    7413,    # Heated Clean Shot
    36981,   # Excavator (proc from Chain Saw)
    36982,   # Full Metal Field (from pre-Hypercharge Barrel Stabilizer)
    16498,   # Drill (2nd charge)
    7411,    # Heated Split Shot
    16500,   # Air Anchor (back off CD)
    7412,    # Heated Slug Shot
)

# --- Clip-detection exclusions ---------------------------------------------
# Blazing Shot's 1.5s Overheated recast is not a clip — exclude it from the
# clipping detector's gap heuristic.
CLIP_EXCLUSIONS: frozenset[int] = frozenset({36978})

# --- Burst-alignment abilities ---------------------------------------------
# Tools worth shifting INTO a raid-buff window (the alignment detector
# flags casts that fall just BEFORE one). MCH: the three 660p Tools.
BURST_ABILITIES: frozenset[int] = frozenset({16498, 16500, 25788})

# --- Drift-detection exclusions --------------------------------------------
# - Hypercharge (17209) is heat-gated (50 heat OR Hypercharged buff). Its 10s
#   recast is almost never the binding constraint — the real waste signal is
#   heat overcap (Phase C). Including it would report ~475s of "drift" for a
#   10-minute fight, which is mostly heat-building time.
# - Tactician (16889) is a pure mitigation/utility oGCD (COOLDOWN_VALUE_P = 0):
#   drifting it costs zero DPS, so it's not a "drifted ability" worth a row in
#   the Drift detail table — exclude it rather than list a perpetual 0p finding.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset({17209, 16889})


# --- JOB_DATA bundle --------------------------------------------------------
# The single instance consumed by the shared aspects in jobs._aspects/.

JOB_DATA: JobData = JobData(
    job_name="Machinist",
    # Queen scan + multi-target grouping read the player's DamageDone — bundle it.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="heat",
            generators=HEAT_GENERATORS,
            spenders=HEAT_SPENDERS,
            cap=HEAT_CAP,
            value_p_per_unit=HEAT_VALUE_P_PER_UNIT,
        ),
        GaugeModel(
            name="battery",
            generators=BATTERY_GENERATORS,
            spenders=BATTERY_SPENDERS,
            cap=BATTERY_CAP,
            value_p_per_unit=BATTERY_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    # Defensive / utility oGCDs the picker never fires and the timeline drops
    # (Tactician, Dismantle) — emitted as isDefensive on abilityMeta. Mirrors the
    # simulator's ignored-action set (see machinist/simulator.py).
    defensive_ids=frozenset({16889, 2887}),
    clip_exclusions=CLIP_EXCLUSIONS,
    # Each Hypercharge cast spawns an 8s window of 1.5s-recast Blazing Shots.
    # Without this skip, ClippingAspect reads those pairs as clipping. The
    # post-Hypercharge resync is also hard to score without mechanic-level
    # modeling, so silent > wrong inside the window.
    clip_skip_windows={17209: 8.0},
    drift_exclusions=DRIFT_EXCLUSIONS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(
        # Blazing Shot reduces Double Check + Checkmate recasts by 15s.
        CDRRule(
            source=36978,
            targets=frozenset({36979, 36980}),
            reduction_s=BLAZING_SHOT_CDR_S,
        ),
    ),
    charge_sharing={
        # Bioblaster consumes Drill charges.
        BIOBLASTER_ABILITY_ID: 16498,
    },
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    aoe_radii_yalm=AOE_RADII_YALM,
    raid_buffs={},  # filled in at M5
    role_policy=PHYSICAL_RANGED,
    # Reassemble is precast during the countdown (instant → clipped by FFLogs);
    # reconstruct it on the Timeline from the pre-applied Reassembled buff.
    prepull_buff_ids={2876: REASSEMBLED_STATUS_ID},
    # A missed tool slot backfills with a Heated combo GCD (~220-420p);
    # use the mid value so a dropped 660p tool is priced at its opportunity
    # cost (~660-320) rather than full potency.
    filler_gcd_potency=320,
    # Tincture: effective BiS Dexterity from xivgear — INCLUDES the party-comp
    # bonus + food (the in-raid value the damage formula uses; the solo char
    # sheet undercounts at ~6516). Base for f(base+Δ)/f(base), Δ541 ⇒ ≈ +8.21%.
    tincture_main_stat=6838,
)
