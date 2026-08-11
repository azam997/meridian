"""Viper data tables (Dawntrail 7.x, level 100) + the `JOB_DATA` JobData instance.

Single source of truth for VPR numbers: potencies, cooldowns, the three gauges
(Serpent Offering -> Reawaken, Rattling Coil -> Uncoiled Fury, Anguine Tribute ->
the Reawakened combo), the maintained self-buffs, and the cleave (free-splash) set.

Viper is a fast, deterministic instant-melee job — a fixed priority loop with NO
RNG procs and essentially no GCD forks (the user's "relatively straightforward"),
so it follows the RPR structure (InstantGCD + greedy/perfect, no beam/DP). Two
SAM-style touches set it apart from RPR:

  * the two combo buffs are SELF-buffs (like SAM Fugetsu/Fuka), not an enemy
    debuff like RPR Death's Design — **Swiftscaled** (-15% recast) is baked into
    the hasted GCD constant (simulator.VPR_GCD_S), and **Hunter's Instinct**
    (+10% damage) is the `coverage_intervals` overlay measured on the PLAYER'S
    OWN aura (jobs/viper/buffs.py), full on the idealized side;
  * the Reawakened combo's GCDs run at their own recasts — the Generations at a
    reduced ~1.7s hasted (RPR Enshroud-Reaping analog), Ouroboros at the slow 3.0s
    Vicewinder-line recast — all via the per-ability GCD_RECAST_MULT below.

⚠️ ACTION IDS — VERIFIED 2026-06-23 against a live top-Viper M9S log's
masterData.abilities (scripts/probe_viper_ids.py). Potencies cross-checked on
ffxiv.consolegameswiki.com per-action pages (level 100, current patch). Positional
abilities carry the positional-HIT value (assume-always-hit, RPR convention); the
finishers also carry the +100 alternating-venom bonus the steady-state rotation
always has up.
"""
from __future__ import annotations

from jobs._core.job import MELEE_DPS, GaugeModel, JobData


PATCH_VERSION = "7.x"

# --- Ability IDs (DT 7.x, level 100) — VERIFIED from live log ---------------

# Single-target combo
STEEL_FANGS        = 34606   # starter, buffed by Honed Steel
REAVING_FANGS      = 34607   # starter, buffed by Honed Reavers
HUNTERS_STING      = 34608   # 2nd step -> Hunter's Instinct (10% dmg self-buff)
SWIFTSKINS_STING   = 34609   # 2nd step -> Swiftscaled (15% haste self-buff)
FLANKSTING_STRIKE  = 34610   # finisher flank  (from Hunter's Sting)
FLANKSBANE_FANG    = 34611   # finisher flank  (from Swiftskin's Sting)
HINDSTING_STRIKE   = 34612   # finisher rear   (from Hunter's Sting)
HINDSBANE_FANG     = 34613   # finisher rear   (from Swiftskin's Sting)
DEATH_RATTLE       = 34634   # oGCD granted by every ST finisher

# Vicewinder line (Rattling Coil + Serpent Offering generator)
VICEWINDER         = 34620   # GCD, +1 Rattling Coil, 2 charges / 40s
HUNTERS_COIL       = 34621   # flank follow-up, +5 offering, -> 2 Twin Bites
SWIFTSKINS_COIL    = 34622   # rear  follow-up, +5 offering, -> 2 Twin Bites
TWINFANG_BITE      = 34636   # oGCD (Hunter's Venom -> 170)
TWINBLOOD_BITE     = 34637   # oGCD (Swiftskin's Venom -> 170)

# Uncoiled (the DPS-neutral ranged GCD — Rattling Coil dump / seamless disengage)
UNCOILED_FURY      = 34633   # ranged GCD (20y), -1 Rattling Coil
UNCOILED_TWINFANG  = 34644   # oGCD (Poised for Twinfang -> 170)
UNCOILED_TWINBLOOD = 34645   # oGCD (Poised for Twinblood -> 170)

# Reawaken line (Serpent Offering spender / Anguine Tribute combo)
REAWAKEN           = 34626   # GCD, -50 Serpent Offering (or free via Ready to
                             # Reawaken from Serpent's Ire), +5 Anguine Tribute
FIRST_GENERATION   = 34627   # Reawakened GCD (reduced recast), -1 Anguine
SECOND_GENERATION  = 34628
THIRD_GENERATION   = 34629
FOURTH_GENERATION  = 34630
OUROBOROS          = 34631   # Reawakened finisher GCD, -1 Anguine
FIRST_LEGACY       = 34640   # oGCD granted by First Generation
SECOND_LEGACY      = 34641   # oGCD granted by Second Generation
THIRD_LEGACY       = 34642   # oGCD granted by Third Generation
FOURTH_LEGACY      = 34643   # oGCD granted by Fourth Generation

# Burst / utility
SERPENTS_IRE       = 34647   # oGCD, 120s: +1 Rattling Coil + Ready to Reawaken
SLITHER            = 34646   # oGCD dash (no damage)

# --- Maintained self-buff status ids (measured on the PLAYER) ---------------
HUNTERS_INSTINCT_STATUS_ID = 1003668   # +10% damage (the coverage overlay)
SWIFTSCALED_STATUS_ID      = 1003669   # -15% recast (baked into the GCD constant)
HUNTERS_INSTINCT_MULT: float = 1.10

# --- Potencies --------------------------------------------------------------
# ability_id -> base potency (no crit modeling, no Hunter's Instinct — that's the
# coverage overlay). Positional + the alternating venom are folded into the
# steady-state hit value the idealized always reaches (RPR assume-hit convention).

POTENCIES: dict[int, int] = {
    # ST combo (starters always cast under their Honed buff -> 300)
    STEEL_FANGS:        300,
    REAVING_FANGS:      300,
    HUNTERS_STING:      300,
    SWIFTSKINS_STING:   300,
    # Finishers: 400 positional + 100 alternating venom = 500 steady-state.
    FLANKSTING_STRIKE:  500,
    FLANKSBANE_FANG:    500,
    HINDSTING_STRIKE:   500,
    HINDSBANE_FANG:     500,
    DEATH_RATTLE:       280,
    # Vicewinder line
    VICEWINDER:         540,
    HUNTERS_COIL:       680,   # flank-hit value
    SWIFTSKINS_COIL:    680,   # rear-hit value
    TWINFANG_BITE:      170,   # with Hunter's Venom (always up in rotation)
    TWINBLOOD_BITE:     170,   # with Swiftskin's Venom
    # Uncoiled (ranged)
    UNCOILED_FURY:      680,
    UNCOILED_TWINFANG:  170,   # with Poised for Twinfang
    UNCOILED_TWINBLOOD: 170,   # with Poised for Twinblood
    # Reawaken line (Generations enhanced -> 680 each; always cast in-sequence)
    REAWAKEN:           750,
    FIRST_GENERATION:   680,
    SECOND_GENERATION:  680,
    THIRD_GENERATION:   680,
    FOURTH_GENERATION:  680,
    OUROBOROS:         1150,
    FIRST_LEGACY:       320,
    SECOND_LEGACY:      320,
    THIRD_LEGACY:       320,
    FOURTH_LEGACY:      320,
    # Utility (no damage)
    SERPENTS_IRE:         0,
    SLITHER:              0,
}

# --- Cleave / splash (free-splash secondary-target potency) -----------------
# ability_id -> potency to EACH additional enemy. Viper's burst buttons cleave at
# 25% (the wiki "75% less for all remaining enemies"); they're cast in the normal
# single-target rotation, so crediting their splash symmetrically on delivered +
# ceiling holds the >100% guard (RPR Communio / Plentiful Harvest pattern). The
# Generations, Coils, Twin Bites and Death Rattle are single-target (no falloff).
# Dedicated AoE buttons (Maws / Bites / Vicepit / Threshes / Dens) are deferred —
# at the current tier top Vipers run single-target + cleave (the RPR finding).
SPLASH_POTENCIES: dict[int, int] = {
    UNCOILED_FURY:  170,   # 680 x 0.25
    REAWAKEN:       188,   # 750 x 0.25
    OUROBOROS:      288,   # 1150 x 0.25
    FIRST_LEGACY:    80,   # 320 x 0.25
    SECOND_LEGACY:   80,
    THIRD_LEGACY:    80,
    FOURTH_LEGACY:   80,
}

# Non-positional ("missed") potency for the positional abilities. The delta vs
# POTENCIES is what a missed positional costs (priced only when the bonus-byte
# detector is wired). Finisher miss keeps the +100 venom (340 -> 440); coil miss
# drops to 630. Idealized always uses the hit value above.
POSITIONAL_MISS_POTENCY: dict[int, int] = {
    FLANKSTING_STRIKE: 440,
    FLANKSBANE_FANG:   440,
    HINDSTING_STRIKE:  440,
    HINDSBANE_FANG:    440,
    HUNTERS_COIL:      630,
    SWIFTSKINS_COIL:   630,
}
POSITIONAL_IDS: frozenset[int] = frozenset(POSITIONAL_MISS_POTENCY)

# oGCD set — kept job-local so scoring's GCD/oGCD split stays hermetic under the
# test stub. Everything else in POTENCIES is a GCD.
OGCD_IDS: frozenset[int] = frozenset({
    DEATH_RATTLE, TWINFANG_BITE, TWINBLOOD_BITE,
    UNCOILED_TWINFANG, UNCOILED_TWINBLOOD,
    FIRST_LEGACY, SECOND_LEGACY, THIRD_LEGACY, FOURTH_LEGACY,
    SERPENTS_IRE, SLITHER,
})

# --- Cooldowns + charges ----------------------------------------------------
# action_id -> (recast_seconds, max_charges). Only genuinely RECAST-gated actions.
# Reawaken / Uncoiled Fury are GAUGE-gated (Serpent Offering / Rattling Coil), so
# listing them would read as false drift — they live in the gauges below instead.
COOLDOWNS: dict[int, tuple[float, int]] = {
    VICEWINDER:    (40.0, 2),   # shares its timer with Vicepit (AoE, not modeled)
    SERPENTS_IRE: (120.0, 1),
}

# Per-cast value for the cooldown-drift detector (lost potential if skipped).
COOLDOWN_VALUE_P: dict[int, int] = {
    VICEWINDER:   1360,   # the two 680 coils it enables
    SERPENTS_IRE: 2000,   # +1 coil (-> Uncoiled Fury) + a free Reawaken cycle
}

# --- Serpent Offering gauge (0-100) -> Reawaken -----------------------------
# Built +10 by every ST combo finisher and +5 by each Vicewinder coil; spent 50
# by Reawaken. The +10 finishers are the dominant source (they fund the cadence).
OFFERING_GENERATORS: dict[int, int] = {
    FLANKSTING_STRIKE: 10,
    FLANKSBANE_FANG:   10,
    HINDSTING_STRIKE:  10,
    HINDSBANE_FANG:    10,
    HUNTERS_COIL:       5,
    SWIFTSKINS_COIL:    5,
}
OFFERING_SPENDERS: dict[int, int] = {REAWAKEN: 50}
OFFERING_CAP = 100
# 50 offering -> one Reawaken cycle (~5900p over 6 GCDs + 4 Legacy oGCDs, replacing
# ~3100p of filler) ~ +2800 net / 50 ~ 56/unit. Held conservative (the sim never
# overcaps; this only prices Overcap-panel waste). Refine post-calibration.
OFFERING_VALUE_P_PER_UNIT: float = 40.0

# --- Rattling Coil gauge (0-3) -> Uncoiled Fury -----------------------------
# +1 per Vicewinder and +1 from Serpent's Ire; spent 1 by Uncoiled Fury.
RATTLING_GENERATORS: dict[int, int] = {VICEWINDER: 1, SERPENTS_IRE: 1}
RATTLING_SPENDERS: dict[int, int] = {UNCOILED_FURY: 1}
RATTLING_CAP = 3
# 1 coil -> Uncoiled Fury (680) + its two 170 oGCDs ~ 1020, replacing a ~500 melee
# GCD ~ +500 net per coil.
RATTLING_VALUE_P_PER_UNIT: float = 500.0

# --- Anguine Tribute (0-5) — tracked in the simulator, not an overcap gauge --
# Reawaken grants 5; each Generation + Ouroboros spends 1. Consumed immediately in
# the Reawakened window, so it never overcaps -> no GaugeModel (state flag only).
ANGUINE_ON_REAWAKEN = 5

# --- Clip / drift exclusions ------------------------------------------------
# The Reawakened GCDs run at non-standard recasts (Generations ~1.7s, Ouroboros at
# the 3.0s Vicewinder-line recast) modeled in GCD_RECAST_MULT — not clips. The keyed
# clip_skip_window opens on Reawaken to cover the whole burst (see clip_skip_windows).
CLIP_EXCLUSIONS: frozenset[int] = frozenset({
    FIRST_GENERATION, SECOND_GENERATION, THIRD_GENERATION, FOURTH_GENERATION,
    OUROBOROS,
})
# Vicewinder is recast-gated for the sim's charge tracking but paced by the
# Rattling Coil cap in practice (held to avoid overcapping coil), so a held
# Vicewinder is not pilot drift — exclude it from the drift detector.
DRIFT_EXCLUSIONS: frozenset[int] = frozenset({VICEWINDER})

# --- Per-ability GCD recast (multiple of the standard 2.5s GCD) -------------
# Viper mixes GCD speeds: the Reawakened Generations are 2.0s (fast — single-weave
# only), the standard ST combo is 2.5s, the Vicewinder line + Ouroboros are 3.0s,
# and Uncoiled Fury is 3.5s. The idle/clip detector scales the run's standard
# effective GCD by these so a slow GCD's natural gap isn't read as idle (and a fast
# one's tight gap isn't read as clipping). Recasts verified on the console wiki;
# they match the measured Swiftscaled-hasted cadences (1.70 / 1.87 / 2.13 / 2.55 /
# 2.97). NOT used by the idealized sim — the sim's blended VPR_GCD_S is the
# separately-calibrated achievable ceiling cadence.
GCD_RECAST_MULT: dict[int, float] = {
    FIRST_GENERATION:  0.8, SECOND_GENERATION: 0.8,    # 2.0 / 2.5
    THIRD_GENERATION:  0.8, FOURTH_GENERATION: 0.8,
    REAWAKEN:          0.88,                            # 2.2 / 2.5
    VICEWINDER:        1.2, HUNTERS_COIL:      1.2,     # 3.0 / 2.5
    SWIFTSKINS_COIL:   1.2, OUROBOROS:         1.2,
    UNCOILED_FURY:     1.4,                             # 3.5 / 2.5
}

# --- Burst-alignment abilities ---------------------------------------------
# Worth shifting into raid-buff windows (AlignmentAspect watches these): the 2-min
# Serpent's Ire and the Reawaken burst it feeds.
BURST_ABILITIES: frozenset[int] = frozenset({SERPENTS_IRE, REAWAKEN})

# Enablers whose value is throughput, not standalone table potency — priced by the
# sim's marginal contribution (scoring._enabler_net_values).
ENABLER_IDS: tuple[int, ...] = (SERPENTS_IRE,)

# --- Canonical opener -------------------------------------------------------
# First ~12 in-fight GCDs (MEASURED from the live M9S top parse, 2026-06-23:
# Slither run-in -> Reaving Fangs -> Serpent's Ire weave -> Swiftskin's Sting ->
# Vicewinder -> the Coil pair -> a double-Reawaken burst -> Uncoiled dumps). The
# Reawakened GCDs are listed as the modal line; OpenerAspect is a zero-priced
# diagnostic, so the exact order is refined during calibration.
CANONICAL_OPENER: tuple[int, ...] = (
    REAVING_FANGS,
    SWIFTSKINS_STING,
    VICEWINDER,
    HUNTERS_COIL,
    SWIFTSKINS_COIL,
    REAWAKEN,
    FIRST_GENERATION,
    SECOND_GENERATION,
    THIRD_GENERATION,
    FOURTH_GENERATION,
    OUROBOROS,
    UNCOILED_FURY,
)


# --- JOB_DATA bundle --------------------------------------------------------

JOB_DATA: JobData = JobData(
    job_name="Viper",
    # Hunter's Instinct coverage is reconstructed from the player's own aura every
    # pull (SAM Fugetsu pattern) — bundle the DamageDone / buff streams.
    prebundle_damage_done=True,
    patch_version=PATCH_VERSION,
    potencies=POTENCIES,
    cooldowns=COOLDOWNS,
    cooldown_value_p=COOLDOWN_VALUE_P,
    gauges=(
        GaugeModel(
            name="offering",
            generators=OFFERING_GENERATORS,
            spenders=OFFERING_SPENDERS,
            cap=OFFERING_CAP,
            value_p_per_unit=OFFERING_VALUE_P_PER_UNIT,
        ),
        GaugeModel(
            name="rattling",
            generators=RATTLING_GENERATORS,
            spenders=RATTLING_SPENDERS,
            cap=RATTLING_CAP,
            value_p_per_unit=RATTLING_VALUE_P_PER_UNIT,
        ),
    ),
    canonical_opener=CANONICAL_OPENER,
    clip_exclusions=CLIP_EXCLUSIONS,
    # The Reawaken window spawns ~5 reduced-recast GCDs; without this skip the
    # ClippingAspect reads the pair as clipping (RPR Enshroud analog). Keyed on the
    # Reawaken GCD that opens the window (~5 x 1.7s + a resolution buffer).
    clip_skip_windows={REAWAKEN: 10.0},
    drift_exclusions=DRIFT_EXCLUSIONS,
    gcd_recast_mult=GCD_RECAST_MULT,
    burst_abilities=BURST_ABILITIES,
    cdr_rules=(),
    charge_sharing={},
    splash_potencies=SPLASH_POTENCIES,
    raid_buffs={},          # modeled via the job-agnostic buff_windows pass
    # Slither (the dash) carries no damage and the sim never fires it — tag it so
    # the frontend filters it off the DPS timeline / cast-diff via `isDefensive`.
    defensive_ids=frozenset({SLITHER}),
    role_policy=MELEE_DPS,
    # A dropped high-potency GCD backfills with a main-combo hit (~300-500); use the
    # mid value so a missed tool is priced at opportunity cost.
    filler_gcd_potency=400,
    # Tincture: effective BiS Dexterity (party-comp + food inclusive) — the live top
    # parse used a Gemdraught of Dexterity. Same DEX value MCH/DNC use.
    tincture_main_stat=6838,
    # No ranged_filler_id: Viper's forced-disconnect tool is Uncoiled Fury, a
    # DPS-NEUTRAL rotational GCD it would press anyway, so disengages are seamless
    # (like Paladin — excluded from the melee-downtime credit) rather than the
    # genuine loss RPR's Harpe is. Revisit only if calibration shows M10S/M11S
    # forced-disconnect looseness.
)
