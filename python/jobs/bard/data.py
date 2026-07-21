"""Bard data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData.

Single source of truth for BRD numbers: potencies, cooldowns, the song cycle, the
DoTs, and the Coda/Barrage machinery. BRD is the analyzer's first **song-cycle
job** — it combines:

  * the DNC archetype (physical ranged, all-instant GCDs, RNG resources modeled
    as measured *budgets*, `PHYSICAL_RANGED` downtime policy),
  * the SAM/WHM DoT-refresh handling (Caustic Bite + Stormbite scored per
    application by time-to-next-refresh, snapshotting buffs at cast), and
  * a BLM-style **self-haste window** (Army's Paeon repertoire haste + Army's
    Muse) modeled in `gcd_duration` and excluded from the gear-GCD inference.

**Resource model — every Repertoire output is a budget, not a gauge.** Repertoire
procs at 80% per 3s song tick (RNG; live-probed 2026-07-02 — the Dawntrail change
only decoupled it from DoT ticks, it did NOT make it deterministic), and it feeds
everything: Pitch Perfect stacks (Wanderer's), Heartbreak Shot recast reduction
(Mage's, −7.5s/proc), haste stacks (Army's) and +5 Soul Voice (→ Apex/Blast
Arrow). Hawk's Eye (→ Refulgent Arrow / Shadowbite) is an independent 35% proc on
Burst Shot / Ladonsbite. Modeling any of these as a `GaugeModel` would emit false
overcap, so — exactly like DNC — the ceiling spends *the same counts* the player
did: the Scoring aspect measures the player's Refulgent/PP/Apex/Blast/Heartbreak
casts and threads them in as `sim_context`. Below-average proc luck never costs
efficiency; only misuse does. `gauges=()`. (This also makes M12S-P2 entry-gauge
machinery unnecessary: carried Soul Voice / Heartbreak charges just surface as
higher measured budgets, which the ceiling spends symmetrically.)

ACTION IDS + POTENCIES are **live-verified** (scripts/probe_bard_ids.py /
probe_bard_rates.py / probe_bard_encore.py against M11S top parses, 2026-07-02,
buff-normalized per-potency table anchored on Burst Shot / Iron Jaws) and
cross-checked on ffxiv.consolegameswiki.com. Highlights:

  * Barraged Refulgent Arrow logs THREE separate ~280p hits (probe: "dmg events
    per cast {1: 57, 3: 6}") → modeled as ×3 on the armed cast.
  * Radiant Encore potency follows the Coda count consumed by the granting
    Radiant Finale — the opener 1-Coda Encore reads ~700p, later 3-Coda ones
    ~1100p (probe_bard_encore.py) → scored per cast from the song/Finale history.
  * Army's Paeon GCD: 2.49s → 2.10s at 4 stacks (16%); Army's Muse 12% for 10s
    (measured 2.18-2.22s medians in the 10s after AP ends).
  * DoT ticks: FFLogs flags every tick hitType=1, so tick amounts BLEND crit/DH
    RNG (smooth 1.5× p10→p90 spread, no flags); the p10 floor matches the wiki
    tick potencies (Caustic 20 / Stormbite 25). Efficiency is crit-neutral
    potency, so the wiki values are the right constants.

⚠️ marks the few ids not observable in a single-target log (AoE buttons +
utility), taken from XIVAPI and verified in the metadata bundling pass.
"""
from __future__ import annotations

from jobs._core.job import PHYSICAL_RANGED, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (live-verified via probe_bard_ids.py unless marked ⚠️) ------

# GCDs (weaponskills; all instant — no begincast events in any probed log)
BURST_SHOT       = 16495   # 220; 35% Hawk's Eye
REFULGENT_ARROW  = 7409    # 280; needs Hawk's Eye (or Barrage → 3 hits)
CAUSTIC_BITE     = 7406    # 150 + DoT 20/3s, 45s; 35% Hawk's Eye
STORMBITE        = 7407    # 100 + DoT 25/3s, 45s; 35% Hawk's Eye
IRON_JAWS        = 3560    # 100; refreshes both DoTs (re-snapshots buffs); 35% HE
APEX_ARROW       = 16496   # ≤700 (scales with Soul Voice; ≥80 gauge grants Blast Arrow Ready)
BLAST_ARROW      = 25784   # 700; needs Blast Arrow Ready
RESONANT_ARROW   = 36976   # 640; needs Resonant Arrow Ready (Barrage, 30s)
RADIANT_ENCORE   = 36977   # 1100/800/700 by Coda (see ENCORE_POTENCY_BY_CODA)
# AoE line (not present in single-target logs; ⚠️ XIVAPI ids)
LADONSBITE       = 25783   # ⚠️ 140 full-to-all; AoE Burst Shot (35% Hawk's Eye)
SHADOWBITE       = 16494   # ⚠️ 200 full-to-all (270 under Barrage); needs Hawk's Eye
WIDE_VOLLEY      = 36974   # ⚠️ pre-82 Ladonsbite (kept for id completeness)

# oGCDs
HEARTBREAK_SHOT  = 36975   # 180; 3 charges / 15s; Mage's Ballad repertoire −7.5s each
RAIN_OF_DEATH    = 117     # ⚠️ 100 full-to-all; shares Heartbreak Shot charges
EMPYREAL_ARROW   = 3558    # 260; 15s; triggers the active song's repertoire effect
SIDEWINDER       = 3562    # 400; 60s
PITCH_PERFECT    = 7404    # 100/220/360 by repertoire stacks; Wanderer's only
BARRAGE          = 107     # 120s; arms 3× next Refulgent + grants Resonant Arrow Ready
RAGING_STRIKES   = 101     # 120s; +15% self-damage, 20s (the job-owned window)
BATTLE_VOICE     = 118     # 120s; party +20% DH 20s (raid_buffs.py models it)
RADIANT_FINALE   = 25785   # 110s; party +2/4/6% by Coda 20s (raid_buffs.py); grants Encore Ready
WANDERERS_MINUET = 3559    # song, 120s / 45s; repertoire → Pitch Perfect stacks
MAGES_BALLAD     = 114     # song, 120s / 45s; repertoire → Heartbreak recast −7.5s
ARMYS_PAEON      = 116     # song, 120s / 45s; repertoire → 4% haste/stack (max 4)
# Defensive / utility (no DPS value; the simulator never fires these)
TROUBADOUR       = 7405    # party mitigation
NATURES_MINNE    = 7408    # healing-up
WARDENS_PAEAN    = 3561    # ⚠️ esuna-over-time
REPELLING_SHOT   = 112     # ⚠️ backstep (movement)

# --- Status (buff) ids (live-verified; for aspects/diagnostics) --------------
HAWKS_EYE_STATUS_ID       = 1003861
RAGING_STRIKES_STATUS_ID  = 1000125
BARRAGE_STATUS_ID         = 1000128
RESONANT_READY_STATUS_ID  = 1003862
ENCORE_READY_STATUS_ID    = 1003863
BLAST_READY_STATUS_ID     = 1002692
ARMYS_MUSE_STATUS_ID      = 1001932
BATTLE_VOICE_STATUS_ID    = 1000141
RADIANT_FINALE_STATUS_ID  = 1002964
SONG_STATUS_IDS = {WANDERERS_MINUET: 1002216, MAGES_BALLAD: 1002217,
                   ARMYS_PAEON: 1002218}
CAUSTIC_DOT_STATUS_ID     = 1001200
STORMBITE_DOT_STATUS_ID   = 1001201

# --- Potencies (probe-verified; buff-normalized p50 within ±1% of each) ------
POTENCIES: dict[int, int] = {
    BURST_SHOT:       220,
    REFULGENT_ARROW:  280,    # ×3 hits under Barrage (handled in scoring/sim)
    CAUSTIC_BITE:     150,    # initial; DoT scored separately
    STORMBITE:        100,    # initial; DoT scored separately
    IRON_JAWS:        100,    # refresh; DoT value flows through the snapshot scoring
    APEX_ARROW:       700,    # at full Soul Voice (budget model spends full-gauge casts)
    BLAST_ARROW:      700,
    RESONANT_ARROW:   640,
    RADIANT_ENCORE:   1100,   # 3-Coda value; per-cast tier via ENCORE_POTENCY_BY_CODA
    PITCH_PERFECT:    360,    # 3-stack value; budget model counts spends (see scoring)
    EMPYREAL_ARROW:   260,
    SIDEWINDER:       400,
    HEARTBREAK_SHOT:  180,
    RAIN_OF_DEATH:    100,
    LADONSBITE:       140,
    SHADOWBITE:       200,
    WIDE_VOLLEY:      140,
    # 0-potency enablers/realism casts
    BARRAGE:          0,
    RAGING_STRIKES:   0,
    BATTLE_VOICE:     0,
    RADIANT_FINALE:   0,
    WANDERERS_MINUET: 0,
    MAGES_BALLAD:     0,
    ARMYS_PAEON:      0,
}

# Radiant Encore potency by the Coda count the granting Radiant Finale consumed
# (live-verified: opener 1-Coda ≈ 700p, 2-min 3-Coda ≈ 1100p).
ENCORE_POTENCY_BY_CODA: dict[int, int] = {1: 700, 2: 800, 3: 1100}
# Barrage: the armed Refulgent Arrow lands 3 separate full-potency hits.
BARRAGE_REFULGENT_HITS: int = 3
BARRAGE_SHADOWBITE_POTENCY: int = 270
BARRAGE_READY_DURATION_S: float = 30.0   # Resonant Arrow Ready
ENCORE_READY_DURATION_S: float = 30.0    # Radiant Encore Ready

# --- DoTs (snapshot buffs at application; scored by time-to-next-refresh) ----
CAUSTIC_DOT_TICK_P: int = 20
STORMBITE_DOT_TICK_P: int = 25
DOT_TICK_S: float = 3.0
DOT_DURATION_S: float = 45.0

# --- Raging Strikes (the job-owned windowed self-buff; PLD FoF pattern) ------
RAGING_STRIKES_MULT: float = 1.15
RAGING_STRIKES_DURATION_S: float = 20.0

# --- The song cycle (live-measured: WM ~43.5s → MB ~40s → AP ~36.5s = 120s) --
SONG_ORDER: tuple[int, ...] = (WANDERERS_MINUET, MAGES_BALLAD, ARMYS_PAEON)
SONG_SPLITS: dict[int, float] = {WANDERERS_MINUET: 43.5, MAGES_BALLAD: 40.0,
                                 ARMYS_PAEON: 36.5}
SONG_DURATION_S: float = 45.0
SONG_RECAST_S: float = 120.0

# --- Army's Paeon haste + Army's Muse (live-measured) ------------------------
# 4%/stack, max 4 (GCD 2.49 → 2.10 measured late-AP). Stacks modeled on the
# optimistic deterministic 3s tick (the real 80% RNG ramp is ~3.75s/stack; the
# faster ramp only RAISES the ceiling by a fraction of a GCD — guard-safe).
AP_HASTE_PER_STACK: float = 0.04
AP_MAX_STACKS: int = 4
AP_STACK_INTERVAL_S: float = 3.0
# Army's Muse: 12% for 10s after Army's Paeon ends (measured 2.18-2.22s GCDs).
MUSE_MULT: float = 0.88
MUSE_DURATION_S: float = 10.0

# oGCD set — kept job-local so the sim's weave logic and scoring's GCD/oGCD split
# stay hermetic under the test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    HEARTBREAK_SHOT, RAIN_OF_DEATH, EMPYREAL_ARROW, SIDEWINDER, PITCH_PERFECT,
    BARRAGE, RAGING_STRIKES, BATTLE_VOICE, RADIANT_FINALE,
    WANDERERS_MINUET, MAGES_BALLAD, ARMYS_PAEON,
    TROUBADOUR, NATURES_MINNE, WARDENS_PAEAN, REPELLING_SHOT,
})

# --- Cooldowns + charges ------------------------------------------------------
# Only genuinely RECAST-gated actions. The proc/budget-gated buttons (Refulgent,
# Pitch Perfect, Apex/Blast, Resonant, Encore) are simulator state flags — listing
# them would read as false drift.
COOLDOWNS: dict[int, tuple[float, int]] = {
    WANDERERS_MINUET: (SONG_RECAST_S, 1),
    MAGES_BALLAD:     (SONG_RECAST_S, 1),
    ARMYS_PAEON:      (SONG_RECAST_S, 1),
    BARRAGE:          (120.0, 1),
    RAGING_STRIKES:   (120.0, 1),
    BATTLE_VOICE:     (120.0, 1),
    RADIANT_FINALE:   (110.0, 1),
    EMPYREAL_ARROW:   (15.0, 1),
    SIDEWINDER:       (60.0, 1),
    HEARTBREAK_SHOT:  (15.0, 3),
}

# Per-cast value for the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    WANDERERS_MINUET: 700,    # the Pitch Perfect economy + crit song
    MAGES_BALLAD:     400,    # the Heartbreak recast economy
    ARMYS_PAEON:      400,    # the haste window (~1 extra GCD)
    BARRAGE:          1200,   # Resonant 640 + the tripled Refulgent (+560)
    RAGING_STRIKES:   900,    # +15% over 20s of burst
    BATTLE_VOICE:     100,    # party value (own-potency ~0); kept as a "press it" nudge
    RADIANT_FINALE:   1100,   # the Radiant Encore it enables
    EMPYREAL_ARROW:   260,
    SIDEWINDER:       400,
    HEARTBREAK_SHOT:  180,
}

# --- Canonical opener (measured consensus, top-3 M11S parses 2026-07-02) -----
# GCD sequence only (OpenerAspect is a zero-priced diagnostic). The oGCD weave
# order observed live: Wanderer's Minuet after GCD 1, pot + Battle Voice /
# Radiant Finale / Raging Strikes across GCDs 2-4, Barrage ~GCD 5.
CANONICAL_OPENER: tuple[int, ...] = (
    STORMBITE, CAUSTIC_BITE, BURST_SHOT, REFULGENT_ARROW, BURST_SHOT,
    REFULGENT_ARROW, REFULGENT_ARROW, RESONANT_ARROW, RADIANT_ENCORE,
    IRON_JAWS, BURST_SHOT, REFULGENT_ARROW,
)

# --- Detection exclusions -----------------------------------------------------
CLIP_EXCLUSIONS: frozenset[int] = frozenset()
# Heartbreak Shot's real cadence is dominated by the Mage's Ballad repertoire
# recast reduction (RNG), so the 15s recast would read as meaningless "drift".
DRIFT_EXCLUSIONS: frozenset[int] = frozenset({HEARTBREAK_SHOT})

# --- Burst-alignment abilities (AlignmentAspect watches these) ---------------
BURST_ABILITIES: frozenset[int] = frozenset({
    RAGING_STRIKES, BATTLE_VOICE, RADIANT_FINALE, BARRAGE,
    RADIANT_ENCORE, RESONANT_ARROW, SIDEWINDER,
})

# Enablers whose value is throughput/burst, not standalone table potency —
# priced by the sim's marginal contribution (scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (BARRAGE, RAGING_STRIKES, RADIANT_FINALE)

# RNG / budget-gated casts — the player can't summon these on demand, so a
# sim/player mismatch on them is NOT a missed cast. Excluded from the diff.
RNG_PROC_IDS: frozenset[int] = frozenset({
    REFULGENT_ARROW, SHADOWBITE, PITCH_PERFECT, APEX_ARROW, BLAST_ARROW,
    HEARTBREAK_SHOT, RAIN_OF_DEATH,
})

DEFENSIVE_IDS: frozenset[int] = frozenset({
    TROUBADOUR, NATURES_MINNE, WARDENS_PAEAN, REPELLING_SHOT,
})

# --- Multi-target -------------------------------------------------------------
# Free-splash: ST-rotation casts that cleave with 50% falloff (wiki-verified).
# The ST sim already casts them, so their splash credits symmetrically.
SPLASH_POTENCIES: dict[int, int] = {
    APEX_ARROW:     350,
    BLAST_ARROW:    350,
    RESONANT_ARROW: 320,
    RADIANT_ENCORE: 550,   # 3-Coda half; per-cast tier stays on the primary
    PITCH_PERFECT:  180,   # 3-stack half
}
# Dedicated AoE buttons (full-to-all) the AoE-aware sim swaps to in multi-target
# windows: Burst Shot → Ladonsbite, Refulgent → Shadowbite, Heartbreak → Rain of
# Death.
AOE_POTENCIES: dict[int, int] = {
    LADONSBITE:    140,
    SHADOWBITE:    200,
    WIDE_VOLLEY:   140,
    RAIN_OF_DEATH: 100,
}


# --- JOB_DATA bundle ----------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Bard",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(),                          # every resource is a measured budget (see module docstring)
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    clip_exclusions=CLIP_EXCLUSIONS,
    drift_exclusions=DRIFT_EXCLUSIONS,
    rng_proc_ids=RNG_PROC_IDS,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),                       # the MB Heartbreak CDR is RNG → budget, not a CDRRule
    charge_sharing={RAIN_OF_DEATH: HEARTBREAK_SHOT},
    raid_buffs={},                      # Battle Voice × Radiant Finale live in raid_buffs.py
    role_policy=PHYSICAL_RANGED,
    splash_potencies=SPLASH_POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    # A dropped GCD backfills with a Burst Shot.
    filler_gcd_potency=220,
    # Tincture: effective BiS Dexterity (party-bonus-inclusive) — same stat as the
    # other physical ranged (MCH/DNC), live-verified there.
    tincture_main_stat=6838,
)
