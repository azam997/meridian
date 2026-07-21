"""Summoner data tables (Dawntrail, level 100, patch 7.5) + `JOB_DATA`.

Single source of truth for SMN numbers: potencies, cast/recast tables, the demi
cycle, the gem/attunement economy, Aetherflow, and the opener. Every id below is
PROBE-VERIFIED against real top-pull cast streams (scripts/probe_summoner_ids.py,
2026-07-03: M11S x3, M12S-P1 x2, M12S-P2 x3) and every potency is verified by the
multiplier-deconvolved damage probe (scripts/probe_summoner_potency.py — amount /
FFLogs-multiplier per clean hit, anchored on Ruin III / Topaz Rite / Mountain
Buster) cross-checked against the console wiki 7.x pages.

SMN is the analyzer's **fourth caster** (after RDM / BLM / PCT) and its first
**pet-cycle** job:

  * **The 60s demi cycle** — Summon Solar Bahamut / Bahamut / Phoenix share ONE
    60s recast (probe: consecutive demi gaps 60.2-60.3s everywhere, even through
    M12S-P1's downtime) in the fixed order Solar -> Bahamut -> Solar -> Phoenix.
    Each summon opens a 15s trance window (probe: last impulse at +14.9s) with
    its own instant filler GCD (Umbral/Astral Impulse, Fountain of Fire), one
    Enkindle, one flare oGCD (Sunflare / Deathflare; Phoenix gets the Rekindle
    HEAL instead — no damage flare), and exactly **4 autonomous pet autos**.
  * **Pet damage is folded onto player casts** (the NIN Bunshin / Phantom
    Kamaitachi pattern): the demi autos (4 x Luxwave/Wyrmwave/Scarlet Flame) are
    folded into the SUMMON cast's potency; the Enkindle payoff (Exodus 1500 /
    Akh Morn 1300 / Revelation 1300) is priced on the player's Enkindle id; the
    primal burst (Inferno / Earthen Fury / Aerial Blast 800) on the player's
    Summon Ifrit/Titan/Garuda II id. The pet's own damage ids never appear in
    `norm_casts`, so the fold is symmetric between delivered and ceiling by
    construction. Probe note: each pet hit logs a `calculateddamage` +
    `damage` PAIR (identical amount ~0.8-2.0s apart) — raw event counts are 2x
    the real hit counts. Measured pet-damage coefficient: pets deal ~0.80x the
    player's damage-per-potency (uniform across all 9 pet abilities at tooltip
    potencies); the fold keeps NOMINAL tooltip values — symmetric on both
    sides, and overstating an extra ceiling-side summon can only raise the
    ceiling (guard-safe). Calibration lever if the mix ever diverges.
  * **Gem phases** — each demi summon grants the three arcanum gems; Summon
    {Ifrit,Titan,Garuda} II then attunes for 2 Ruby / 4 Topaz / 4 Emerald Rites
    (probe: exactly 2/4/4 every phase). Rites carry per-ability recasts
    (Emerald 1.5s / Topaz 2.5s / Ruby 3.0s — `gcd_recast_mult`), and favors:
    Summon Ifrit grants Crimson Cyclone (-> Crimson Strike), each Topaz Rite
    grants a Mountain Buster (probe: 40 == 40), Summon Garuda grants Slipstream.
    Primal ORDER within the minute is a real GCD fork (probe: top parses run
    Garuda/Ifrit/Titan, Titan/Garuda/Ifrit, ... varying by cycle).
  * **Aetherflow** — Energy Drain (60s) grants 2 stacks -> 2 x Necrotize, plus
    the Further Ruin proc -> 1 x Ruin IV. Closed cast-visible economy (probe:
    22 Necrotize == 2 x 11 Energy Drain), so it IS a `GaugeModel`.
  * **Searing Light** is the party raid buff (+5%/20s — modeled by the shared
    `raid_buffs.PROVIDER_BUFFS` catalog, NEVER in job scoring). Its self effect
    lives here: Ruby's Glimmer -> **Searing Flash** (700p, probe: casts 1:1
    with Searing Light).
  * **No RNG at all** — procs are deterministic grants (Further Ruin, Ruby's
    Glimmer, favors), so the ceiling needs no proc-budget `sim_context`.
    M12S-P2 continuation logs open COLD (probe: pre-pull Ruin III at t~0.1-0.2
    into the standard opener), so no entry seeding is required either; the
    aetherflow entry gauge is wired anyway (measure returns 0 on cold opens —
    byte-identical).

A scoring-basis note: **Swiftcast is deliberately not modeled as DPS.** Every
swiftable SMN cast is recast-bound (Ruin III max(1.5, 2.5), Ruby Rite
max(2.8, 3.0), Slipstream max(3.0, 3.5)), so an instant cast changes no slot
length — probe: top parses Swiftcast Slipstream for movement (8 of 10
Slipstreams instant), cadence unchanged.
"""
from __future__ import annotations

from jobs._core.job import CASTER_HEALER, GaugeModel, JobData


PATCH_VERSION = "7.5"

# --- Ability IDs (DT 7.5, level 100; probe-verified from live logs) ----------

# Filler + Aetherflow procs.
RUIN_III           = 3579    # 400p, 1.5s cast (probe: begincast delta 0.98+0.5)
RUIN_IV            = 7426    # 520p, instant; needs Further Ruin (Energy Drain)
TRI_DISASTER       = 25826   # 120p AoE filler, 1.5s cast  ⚠️ id XIVAPI-verified at bundling
# Demi-window filler GCDs (instant, 2.5s recast; trance-gated).
ASTRAL_IMPULSE     = 25820   # 500p (Dreadwyrm Trance / Bahamut)
UMBRAL_IMPULSE     = 36994   # 640p (Lightwyrm Trance / Solar Bahamut)
FOUNTAIN_OF_FIRE   = 16514   # 580p (Firebird Trance / Phoenix)
ASTRAL_FLARE       = 25821   # 180p AoE trance filler  ⚠️
UMBRAL_FLARE       = 36995   # 300p AoE trance filler  ⚠️
BRAND_OF_PURGATORY = 16515   # 240p AoE trance filler  ⚠️
# Demi oGCDs (player-cast; demi-window-gated -> state flags, NOT cooldowns).
DEATHFLARE         = 3582    # 500p (Bahamut window)
SUNFLARE           = 36996   # 1000p (Solar Bahamut window)
ENKINDLE_BAHAMUT   = 7429    # -> pet Akh Morn 1300 (priced here; 1 hit/cast)
ENKINDLE_PHOENIX   = 16516   # -> pet Revelation 1300
ENKINDLE_SOLAR_BAHAMUT = 36998  # -> pet Exodus 1500
# Demi summons (GCDs; ONE shared 60s recast, keyed on Solar Bahamut).
# Potency = the 4 autonomous pet autos folded at the summon cast (see docstring).
SUMMON_BAHAMUT     = 7427    # fold 4 x Wyrmwave 150 = 600
SUMMON_PHOENIX     = 25831   # fold 4 x Scarlet Flame 150 = 600
SUMMON_SOLAR_BAHAMUT = 36992  # fold 4 x Luxwave 160 = 640
# Primal summons (GCDs; gem-gated). Potency = the pet's one-shot burst.
SUMMON_IFRIT_II    = 25838   # pet Inferno 800
SUMMON_TITAN_II    = 25839   # pet Earthen Fury 800
SUMMON_GARUDA_II   = 25840   # pet Aerial Blast 800
# Attunement rites (GCDs with per-ability recasts).
RUBY_RITE          = 25823   # 620p, 2.8s cast / 3.0s recast (x2 per Ifrit)
TOPAZ_RITE         = 25824   # 340p, instant / 2.5s recast (x4 per Titan)
EMERALD_RITE       = 25825   # 280p, instant / 1.5s recast (x4 per Garuda)
RUBY_CATASTROPHE   = 25832   # 210p AoE rite, 2.8s cast / 3.0s recast  ⚠️
TOPAZ_CATASTROPHE  = 25833   # 140p AoE rite  ⚠️
EMERALD_CATASTROPHE = 25834  # 100p AoE rite, 1.5s recast  ⚠️
# Favors.
CRIMSON_CYCLONE    = 25835   # 560p instant GCD (melee dash; Ifrit's Favor)
CRIMSON_STRIKE     = 25885   # 560p instant GCD (combo from Cyclone)
MOUNTAIN_BUSTER    = 25836   # 160p oGCD (Titan's Favor, granted per Topaz Rite)
SLIPSTREAM         = 25837   # 520p + windstorm 30p x 5 ticks folded = 670p;
                             # 3.0s cast / 3.5s recast (Garuda's Favor)
# Aetherflow.
ENERGY_DRAIN       = 16508   # 200p oGCD, 60s; grants Aetherflow x2 + Further Ruin
ENERGY_SIPHON      = 16510   # 100p AoE variant (shares the 60s recast)  ⚠️
NECROTIZE          = 36990   # 500p oGCD; spends 1 Aetherflow
PAINFLARE          = 3578    # 220p AoE variant  ⚠️
# Searing.
SEARING_LIGHT      = 25801   # 0p; the party buff (shared catalog) + Ruby's Glimmer
SEARING_FLASH      = 36991   # 700p AoE (full to all); needs Ruby's Glimmer
# Utility / defensives / heals (never simmed).
RADIANT_AEGIS      = 25799   # shield
LUX_SOLARIS        = 36997   # heal (Refulgent Lux, granted by Solar Bahamut)
REKINDLE           = 25830   # heal (the Phoenix window's Astral Flow slot)
PHYSICK            = 190     # heal GCD  ⚠️
RESURRECTION       = 173     # raise  ⚠️

# Pet damage ids — METADATA ONLY. The pet's hits log under these (source = the
# pet actor) and are already rolled into the owner's FFLogs damage; they never
# appear in the player's cast stream, so they are never scored directly — the
# folds above carry their value. Every hit also emits a calculateddamage twin.
WYRMWAVE_PET       = 7428    # Demi-Bahamut auto 150p, x4 per window
AKH_MORN_PET       = 7449    # Enkindle Bahamut payoff 1300p
SCARLET_FLAME_PET  = 16519   # Demi-Phoenix auto 150p, x4 per window
REVELATION_PET     = 16518   # Enkindle Phoenix payoff 1300p
LUXWAVE_PET        = 36993   # Solar Bahamut auto 160p, x4 per window
EXODUS_PET         = 36999   # Enkindle Solar payoff 1500p
INFERNO_PET        = 25852   # Summon Ifrit II burst 800p
EARTHEN_FURY_PET   = 25853   # Summon Titan II burst 800p
AERIAL_BLAST_PET   = 25854   # Summon Garuda II burst 800p

# --- Status ids (probe-verified applybuff ids minus 1_000_000) ---------------
AETHERFLOW_STATUS_ID       = 304
FURTHER_RUIN_STATUS_ID     = 2701    # 60s; grants Ruin IV
RADIANT_AEGIS_STATUS_ID    = 2702
SEARING_LIGHT_STATUS_ID    = 2703    # 20.0s (probe: 20.02)
SLIPSTREAM_STATUS_ID       = 2706    # the 15s windstorm ground effect
IFRITS_FAVOR_STATUS_ID     = 2724    # -> Crimson Cyclone
GARUDAS_FAVOR_STATUS_ID    = 2725    # -> Slipstream
TITANS_FAVOR_STATUS_ID     = 2853    # -> Mountain Buster (granted per Topaz Rite)
RUBYS_GLIMMER_STATUS_ID    = 3873    # -> Searing Flash (granted by Searing Light)
REFULGENT_LUX_STATUS_ID    = 3874    # -> Lux Solaris (heal; not modeled)
CRIMSON_STRIKE_READY_STATUS_ID = 4403

# --- Cast times (s) — feeds the HardcastGCD timing preset --------------------
# Absent ids are instant. Probe-verified via begincast->cast deltas (FFLogs
# snapshots ~0.5s before the bar completes): Ruin III 0.98 -> 1.5s, Ruby Rite
# 2.27 -> 2.8s, Slipstream 2.46 -> 3.0s.
CAST_TIMES: dict[int, float] = {
    RUIN_III:          1.5,
    TRI_DISASTER:      1.5,
    RUBY_RITE:         2.8,
    RUBY_CATASTROPHE:  2.8,
    SLIPSTREAM:        3.0,
}

# Per-ability recast as a multiple of the 2.5s standard (the Viper pattern).
# Scales with the player's Spell Speed like the base recast (probe: Emerald
# 1.52s / Topaz 2.49s / Ruby 2.99s consecutive-cast gaps at a 2.48s player
# GCD). Drives BOTH the sim's `gcd_duration` and the idle/clip detector's
# pacing (JobData field).
RECAST_MULT: dict[int, float] = {
    EMERALD_RITE:        0.6,    # 1.5s
    EMERALD_CATASTROPHE: 0.6,
    RUBY_RITE:           1.2,    # 3.0s
    RUBY_CATASTROPHE:    1.2,
    SLIPSTREAM:          1.4,    # 3.5s (probe: Slipstream -> next GCD 3.48s)
}

# --- Potencies (multiplier-deconvolved probe + wiki 7.x, all within ±2%) -----
POTENCIES: dict[int, int] = {
    RUIN_III:          400,
    RUIN_IV:           520,
    TRI_DISASTER:      120,
    ASTRAL_IMPULSE:    500,
    UMBRAL_IMPULSE:    640,
    FOUNTAIN_OF_FIRE:  580,
    ASTRAL_FLARE:      180,
    UMBRAL_FLARE:      300,
    BRAND_OF_PURGATORY: 240,
    DEATHFLARE:        500,
    SUNFLARE:          1000,
    # Pet folds (see module docstring).
    ENKINDLE_BAHAMUT:  1300,     # Akh Morn
    ENKINDLE_PHOENIX:  1300,     # Revelation
    ENKINDLE_SOLAR_BAHAMUT: 1500,  # Exodus
    SUMMON_BAHAMUT:    600,      # 4 x Wyrmwave 150
    SUMMON_PHOENIX:    600,      # 4 x Scarlet Flame 150
    SUMMON_SOLAR_BAHAMUT: 640,   # 4 x Luxwave 160
    SUMMON_IFRIT_II:   800,      # Inferno
    SUMMON_TITAN_II:   800,      # Earthen Fury
    SUMMON_GARUDA_II:  800,      # Aerial Blast
    RUBY_RITE:         620,
    TOPAZ_RITE:        340,
    EMERALD_RITE:      280,
    RUBY_CATASTROPHE:  210,
    TOPAZ_CATASTROPHE: 140,
    EMERALD_CATASTROPHE: 100,
    CRIMSON_CYCLONE:   560,
    CRIMSON_STRIKE:    560,
    MOUNTAIN_BUSTER:   160,
    SLIPSTREAM:        670,      # 520 + 5 windstorm ticks x 30 (CoS-style fold)
    ENERGY_DRAIN:      200,
    ENERGY_SIPHON:     100,
    NECROTIZE:         500,
    PAINFLARE:         220,
    SEARING_FLASH:     700,
    # Buttons / casts with no direct potency.
    SEARING_LIGHT:     0,
    RADIANT_AEGIS:     0,
    LUX_SOLARIS:       0,
    REKINDLE:          0,
    PHYSICK:           0,
    RESURRECTION:      0,
}

# Dedicated AoE buttons (full potency to all targets — wiki "to all enemies").
# The primary potency stays in POTENCIES; aoe_potency.potency_for prices the
# extra targets in confirmed multi-target windows.
AOE_POTENCIES: dict[int, int] = {
    TRI_DISASTER:       120,
    ASTRAL_FLARE:       180,
    UMBRAL_FLARE:       300,
    BRAND_OF_PURGATORY: 240,
    RUBY_CATASTROPHE:   210,
    TOPAZ_CATASTROPHE:  140,
    EMERALD_CATASTROPHE: 100,
    PAINFLARE:          220,
    ENERGY_SIPHON:      100,
}
# Free-splash: ST-rotation casts that cleave with wiki falloff (per-EXTRA-target
# potency). The demi-summon auto folds are single-target — no splash there.
SPLASH_POTENCIES: dict[int, int] = {
    RUIN_IV:           208,    # 520, -60%
    DEATHFLARE:        250,    # 500, -50%
    SUNFLARE:          500,    # 1000, -50%
    ENKINDLE_BAHAMUT:  520,    # Akh Morn 1300, -60%
    ENKINDLE_PHOENIX:  520,
    ENKINDLE_SOLAR_BAHAMUT: 600,  # Exodus 1500, -60%
    SUMMON_IFRIT_II:   400,    # Inferno 800, -50%
    SUMMON_TITAN_II:   400,
    SUMMON_GARUDA_II:  400,
    CRIMSON_CYCLONE:   224,    # 560, -60%
    CRIMSON_STRIKE:    196,    # 560, -65%
    MOUNTAIN_BUSTER:   64,     # 160, -60%
    SLIPSTREAM:        208,    # 520 initial, -60% (windstorm fold kept primary-only)
    SEARING_FLASH:     700,    # full to all
}

# oGCD set — kept job-local so the scoring GCD/oGCD split stays hermetic under
# the test stub. The demi summons, primal summons, rites, Cyclone/Strike,
# Slipstream and Ruin III/IV are all genuinely GCDs.
OGCD_IDS: frozenset[int] = frozenset({
    DEATHFLARE, SUNFLARE,
    ENKINDLE_BAHAMUT, ENKINDLE_PHOENIX, ENKINDLE_SOLAR_BAHAMUT,
    MOUNTAIN_BUSTER,
    ENERGY_DRAIN, ENERGY_SIPHON, NECROTIZE, PAINFLARE,
    SEARING_LIGHT, SEARING_FLASH,
    RADIANT_AEGIS, LUX_SOLARIS, REKINDLE,
})

# --- Cooldowns + charges ------------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only the genuinely RECAST-gated
# DPS oGCDs live here. The demi pool is keyed on SUMMON_SOLAR_BAHAMUT (the PCT
# Living-Muse pattern): Bahamut/Phoenix spend the shared 60s recast manually in
# the simulator. Enkindle / Deathflare / Sunflare have real 20s recasts but are
# demi-window-gated and never bind at the 60s window cadence -> state flags,
# NOT cooldowns (gotcha #2 — a 20s entry would read as massive false drift).
# Necrotize/Ruin IV/Mountain Buster/Searing Flash/rites/favor spenders/primal
# summons are all resource- or state-gated -> absent.
COOLDOWNS: dict[int, tuple[float, int]] = {
    SUMMON_SOLAR_BAHAMUT: (60.0, 1),
    ENERGY_DRAIN:         (60.0, 1),
    SEARING_LIGHT:        (120.0, 1),
}

# Consumer -> pool-source mapping for the shared-charge drift accounting.
CHARGE_SHARING: dict[int, int] = {
    SUMMON_BAHAMUT: SUMMON_SOLAR_BAHAMUT,
    SUMMON_PHOENIX: SUMMON_SOLAR_BAHAMUT,
    ENERGY_SIPHON:  ENERGY_DRAIN,
}

# Per-cast value used by the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    # A demi window package: summon fold + Enkindle + flare + the impulse
    # upgrade over Ruin III filler (~6 x 190), averaged across the cycle.
    SUMMON_SOLAR_BAHAMUT: 3800,
    # 200 + 2 x Necrotize 500 + the Ruin IV upgrade (520 - 400).
    ENERGY_DRAIN:         1300,
    # Searing Flash (the +5% party buff is the shared overlay's job).
    SEARING_LIGHT:        700,
}

# --- Demi cycle / gem economy ---------------------------------------------------
DEMI_CYCLE: tuple[int, ...] = (
    SUMMON_SOLAR_BAHAMUT, SUMMON_BAHAMUT, SUMMON_SOLAR_BAHAMUT, SUMMON_PHOENIX,
)
DEMI_WINDOW_S: float = 15.2          # probe: last impulse at +14.9..15.0s every
                                     # window — a real player lands the 6th
                                     # impulse ON the +15.0 boundary, so the
                                     # modeled window includes it (guard-safe)
PET_AUTOS_PER_WINDOW: int = 4        # probe: 4 real hits (8 raw calc+dmg events)
# Per-demi window filler GCD + damage flare (Phoenix's Astral Flow is the
# Rekindle heal -> no flare).
DEMI_IMPULSE: dict[int, int] = {
    SUMMON_BAHAMUT:       ASTRAL_IMPULSE,
    SUMMON_PHOENIX:       FOUNTAIN_OF_FIRE,
    SUMMON_SOLAR_BAHAMUT: UMBRAL_IMPULSE,
}
DEMI_FLARE: dict[int, int] = {
    SUMMON_BAHAMUT:       DEATHFLARE,
    SUMMON_SOLAR_BAHAMUT: SUNFLARE,
}
DEMI_ENKINDLE: dict[int, int] = {
    SUMMON_BAHAMUT:       ENKINDLE_BAHAMUT,
    SUMMON_PHOENIX:       ENKINDLE_PHOENIX,
    SUMMON_SOLAR_BAHAMUT: ENKINDLE_SOLAR_BAHAMUT,
}
# Primal attunement: summon -> (rite, count). Favors: Ifrit grants Crimson
# Cyclone (-> Strike combo), Garuda grants Slipstream, each Topaz Rite grants a
# Mountain Buster (probe: 40 == 40).
PRIMAL_RITES: dict[int, tuple[int, int]] = {
    SUMMON_IFRIT_II:  (RUBY_RITE, 2),
    SUMMON_TITAN_II:  (TOPAZ_RITE, 4),
    SUMMON_GARUDA_II: (EMERALD_RITE, 4),
}
PRIMAL_SUMMONS: tuple[int, ...] = (SUMMON_GARUDA_II, SUMMON_IFRIT_II, SUMMON_TITAN_II)

# Aetherflow gauge (drift/overcap detector + entry gauge). Closed cast-visible
# economy: Energy Drain/Siphon is the only generator and both are in the cast
# stream, so the deepest-deficit entry measurement needs no window cap.
AETHERFLOW_CAP = 2
AETHERFLOW_GAUGE = GaugeModel(
    name="aetherflow",
    generators={ENERGY_DRAIN: 2, ENERGY_SIPHON: 2},
    spenders={NECROTIZE: 1, PAINFLARE: 1},
    cap=AETHERFLOW_CAP,
    value_p_per_unit=500.0,   # a Necrotize
)

# --- Canonical opener -----------------------------------------------------------
# The MEASURED consensus opener (probe 2026-07-03: M11S top pulls + M12S-P2 cold
# opens agree): pre-pull Ruin III (lands t~0.3), Summon Solar Bahamut, then the
# Lightwyrm window with Searing Light + pot + Energy Drain + Enkindle + Sunflare
# + Searing Flash + Necrotize weaved between Umbral Impulses, then Garuda ->
# Ifrit -> Titan gem phases.
CANONICAL_OPENER: tuple[int, ...] = (
    RUIN_III,
    SUMMON_SOLAR_BAHAMUT,
    UMBRAL_IMPULSE,
    SEARING_LIGHT,
    UMBRAL_IMPULSE,
    ENERGY_DRAIN,
    UMBRAL_IMPULSE,
    ENKINDLE_SOLAR_BAHAMUT,
    UMBRAL_IMPULSE,
    SUNFLARE,
    NECROTIZE,
    UMBRAL_IMPULSE,
    SEARING_FLASH,
    UMBRAL_IMPULSE,
    NECROTIZE,
    SUMMON_GARUDA_II,
    EMERALD_RITE,
    EMERALD_RITE,
    SLIPSTREAM,
    EMERALD_RITE,
    EMERALD_RITE,
    SUMMON_IFRIT_II,
    CRIMSON_CYCLONE,
    RUBY_RITE,
    RUBY_RITE,
    CRIMSON_STRIKE,
    SUMMON_TITAN_II,
    TOPAZ_RITE,
    MOUNTAIN_BUSTER,
    TOPAZ_RITE,
    MOUNTAIN_BUSTER,
    TOPAZ_RITE,
    MOUNTAIN_BUSTER,
    TOPAZ_RITE,
    MOUNTAIN_BUSTER,
)

# --- Burst-alignment abilities ---------------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these).
BURST_ABILITIES: frozenset[int] = frozenset({
    ENKINDLE_BAHAMUT, ENKINDLE_PHOENIX, ENKINDLE_SOLAR_BAHAMUT,
    DEATHFLARE, SUNFLARE, SEARING_FLASH,
})

# Enablers whose value is throughput, not standalone table potency — priced by
# the sim's marginal contribution (scoring.enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (SEARING_LIGHT, SUMMON_SOLAR_BAHAMUT, ENERGY_DRAIN)

# Interchangeable filler GCDs (the diffuse "filler quality" card).
FILLER_QUALITY_GCDS: frozenset[int] = frozenset({RUIN_III})

# Defensive / utility casts the simulator never fires (excluded from the DPS
# timeline + cast-diff). Swiftcast/Addle/Lucid/Surecast are shared role actions
# (role_actions.ROLE_ACTION_IDS).
DEFENSIVE_IDS: frozenset[int] = frozenset({
    RADIANT_AEGIS, LUX_SOLARIS, REKINDLE, PHYSICK, RESURRECTION,
})

# Id families (used by the simulator + tests).
DEMI_SUMMON_IDS: frozenset[int] = frozenset(DEMI_CYCLE)
PRIMAL_SUMMON_IDS: frozenset[int] = frozenset(PRIMAL_RITES)
RITE_IDS: frozenset[int] = frozenset({RUBY_RITE, TOPAZ_RITE, EMERALD_RITE})
IMPULSE_IDS: frozenset[int] = frozenset(DEMI_IMPULSE.values())
PET_IDS: frozenset[int] = frozenset({
    WYRMWAVE_PET, AKH_MORN_PET, SCARLET_FLAME_PET, REVELATION_PET,
    LUXWAVE_PET, EXODUS_PET, INFERNO_PET, EARTHEN_FURY_PET, AERIAL_BLAST_PET,
})


# --- JOB_DATA bundle -----------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Summoner",
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    aoe_potencies=AOE_POTENCIES,
    splash_potencies=SPLASH_POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(AETHERFLOW_GAUGE,),
    canonical_opener=CANONICAL_OPENER,
    defensive_ids=DEFENSIVE_IDS,
    gcd_recast_mult=RECAST_MULT,
    drift_exclusions=frozenset(),
    charge_sharing=CHARGE_SHARING,
    filler_quality_gcds=FILLER_QUALITY_GCDS,
    burst_abilities=BURST_ABILITIES,
    raid_buffs={},                      # Searing Light rides the shared PROVIDER_BUFFS catalog
    role_policy=CASTER_HEALER,
    # A missed cast backfills with a Ruin III (~400); price a miss above that.
    filler_gcd_potency=400,
    # Tincture: effective BiS Intelligence incl. party-comp bonus + food (the
    # xivgear party-bonus-inclusive convention; same Casting set as RDM/BLM/PCT).
    tincture_main_stat=6838,
)
